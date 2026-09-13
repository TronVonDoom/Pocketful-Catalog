"""
Apply supabase/migrations to a throwaway PostgreSQL and run supabase/tests against it.

Nothing here touches Supabase or any database already on this machine. It creates a fresh
cluster in a temporary folder, starts it on a private port bound to localhost, and deletes
it afterwards, so it can run as often as the migrations change.

Supabase provides a few things a bare PostgreSQL does not: the anon, authenticated and
service_role roles, the default grants that hand every new table to them, and the storage
schema. `SUPABASE_STUB` recreates just enough of those for the migrations to meet the
same conditions they will meet there.

Needs PostgreSQL's command-line programs (initdb, pg_ctl, psql), found in PG_BIN, on PATH,
or in the standard Windows install folder. Stdlib only, like the rest of the tools.

    python tools/test_database.py
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "supabase" / "migrations"
TESTS = ROOT / "supabase" / "tests"
PORT = 55439

SUPABASE_STUB = """
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
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


def psql(bin_dir: Path, *args) -> subprocess.CompletedProcess:
    return run([bin_dir / exe("psql"), "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", "localhost",
                "-p", PORT, "-U", "postgres", "-d", "postgres", *args],
               env={**os.environ, "PGCLIENTENCODING": "UTF8"})


def step(label: str, result: subprocess.CompletedProcess) -> None:
    if result.returncode != 0:
        print(f"FAIL  {label}")
        print((result.stderr or result.stdout).strip())
        raise SystemExit(1)
    print(f"ok    {label}", flush=True)


def main() -> None:
    bin_dir = find_bin()
    work = Path(tempfile.mkdtemp(prefix="pocketful-db-"))
    data = work / "data"
    started = False
    try:
        step("create a throwaway cluster",
             run([bin_dir / exe("initdb"), "-D", data, "-U", "postgres", "-A", "trust",
                  "-E", "UTF8", "--locale=C"]))
        # Not captured: the server pg_ctl leaves running inherits its output handles, so
        # waiting to read them to the end would wait for the server to exit. Its log goes
        # to a file instead.
        started = subprocess.run(
            [str(bin_dir / exe("pg_ctl")), "-D", str(data), "-l", str(work / "server.log"), "-w",
             "-o", f"-p {PORT} -c listen_addresses=localhost", "start"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not started:
            log = work / "server.log"
            print(f"FAIL  start it on localhost:{PORT}")
            print(log.read_text(errors="replace") if log.exists() else "")
            raise SystemExit(1)
        print(f"ok    start it on localhost:{PORT}", flush=True)

        step("stand in for Supabase's roles and storage schema", psql(bin_dir, "-c", SUPABASE_STUB))
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            step(f"migration {migration.name}", psql(bin_dir, "--single-transaction", "-f", migration))
        for test in sorted(TESTS.glob("*.sql")):
            step(f"test {test.name}", psql(bin_dir, "-f", test))
        print("\nAll migrations applied and every test passed.")
    finally:
        if started:
            subprocess.run([str(bin_dir / exe("pg_ctl")), "-D", str(data), "-w", "-m", "immediate", "stop"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
