"""
Apply supabase/migrations to a throwaway PostgreSQL and run supabase/tests against it.

Nothing here touches Supabase or any database already on this machine. It creates a fresh
cluster in a temporary folder, starts it on a private port bound to localhost, and deletes
it afterwards, so it can run as often as the migrations change.

Supabase provides a few things a bare PostgreSQL does not: the anon, authenticated and
service_role roles, the authenticator role its REST API logs in as, the default grants that
hand every new table to them, and the storage schema with its guard against deletes.
`SUPABASE_STUB` recreates just enough of those for the migrations to meet the same
conditions they will meet there.

`throwaway_database()` is what tools/test_editor.py builds on, so the editor is tested
against exactly the schema these tests check.

Needs PostgreSQL's command-line programs (initdb, pg_ctl, psql), found in PG_BIN, on PATH,
or in the standard Windows install folder. Stdlib only, like the rest of the tools.

    python tools/test_database.py
"""

from __future__ import annotations

import contextlib
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"
TESTS = ROOT / "supabase" / "tests"
PORT = 55439

SUPABASE_STUB = """
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role authenticator login noinherit;
grant anon, authenticated, service_role to authenticator;
grant usage on schema public to anon, authenticated, service_role;
alter default privileges in schema public grant all on tables to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
alter default privileges in schema public grant all on functions to anon, authenticated, service_role;
create schema storage;
create table storage.buckets (
  id text primary key,
  name text not null,
  public boolean default false,
  file_size_limit bigint,
  allowed_mime_types text[]
);
create table storage.objects (
  id uuid primary key default gen_random_uuid(),
  bucket_id text references storage.buckets (id),
  name text
);
-- Supabase's own guard, copied: no direct delete from storage tables unless asked for.
create function storage.protect_delete() returns trigger language plpgsql as $$
begin
  if coalesce(current_setting('storage.allow_delete_query', true), 'false') != 'true' then
    raise exception 'Direct deletion from storage tables is not allowed. Use the Storage API instead.'
      using errcode = '42501';
  end if;
  return null;
end $$;
create trigger protect_buckets_delete before delete on storage.buckets
  for each statement execute function storage.protect_delete();
"""


def find_bin() -> Path:
    candidates = [os.environ.get("PG_BIN")]
    if which := shutil.which("pg_ctl"):
        candidates.append(str(Path(which).parent))
    candidates += sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin"), reverse=True)
    for c in candidates:
        if c and (Path(c) / exe("pg_ctl")).exists():
            return Path(c)
    sys.exit("PostgreSQL's command-line programs were not found. Set PG_BIN to their folder.")


def exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def run(args: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding="utf-8", **kw)


def quiet(args: list) -> int:
    """
    Runs a command without capturing its output.

    Needed for pg_ctl start and anything else that leaves a server running: the server
    inherits the output handles, so reading them to the end would wait for it to exit.
    """
    return subprocess.run([str(a) for a in args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode


def step(label: str, result: subprocess.CompletedProcess) -> None:
    if result.returncode != 0:
        print(f"FAIL  {label}")
        print((result.stderr or result.stdout).strip())
        raise SystemExit(1)
    print(f"ok    {label}", flush=True)


@contextlib.contextmanager
def throwaway_database(port: int = PORT) -> Iterator[Callable[..., subprocess.CompletedProcess]]:
    """
    A fresh cluster with the Supabase stand-in and every migration applied.

    Yields a psql runner for it, and removes the whole cluster on the way out, whether or not
    whatever ran inside succeeded.
    """
    bin_dir = find_bin()
    work = Path(tempfile.mkdtemp(prefix="pocketful-db-"))
    data = work / "data"

    def psql(*args) -> subprocess.CompletedProcess:
        return run([bin_dir / exe("psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", "localhost",
                    "-p", port, "-U", "postgres", "-d", "postgres", *args],
                   env={**os.environ, "PGCLIENTENCODING": "UTF8"})

    started = False
    try:
        step("create a throwaway cluster",
             run([bin_dir / exe("initdb"), "-D", data, "-U", "postgres", "-A", "trust",
                  "-E", "UTF8", "--locale=C"]))
        started = quiet([bin_dir / exe("pg_ctl"), "-D", data, "-l", work / "server.log", "-w",
                         "-o", f"-p {port} -c listen_addresses=localhost", "start"]) == 0
        if not started:
            log = work / "server.log"
            print(f"FAIL  start it on localhost:{port}")
            print(log.read_text(errors="replace") if log.exists() else "")
            raise SystemExit(1)
        print(f"ok    start it on localhost:{port}", flush=True)

        step("stand in for Supabase's roles and storage schema", psql("-c", SUPABASE_STUB))
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            step(f"migration {migration.name}", psql("--single-transaction", "-f", migration))
        yield psql
    finally:
        if started:
            quiet([bin_dir / exe("pg_ctl"), "-D", data, "-w", "-m", "immediate", "stop"])
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    with throwaway_database() as psql:
        for test in sorted(TESTS.glob("*.sql")):
            step(f"test {test.name}", psql("-f", test))
    print("\nAll migrations applied and every test passed.")


if __name__ == "__main__":
    main()
