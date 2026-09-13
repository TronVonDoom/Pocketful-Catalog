"""
Apply supabase/migrations to Pocketful's Supabase project.

    python tools/migrate.py            # what has been applied, and what has not
    python tools/migrate.py --apply    # apply what has not

Each migration runs in one transaction together with the row recording it, so a migration
that fails leaves nothing behind and is simply tried again next time. The record is kept
in supabase_migrations.schema_migrations, the table the Supabase CLI uses, so the CLI and
this script agree about what has run if the CLI is ever used instead.

Run `python tools/test_database.py` first: that is where a migration should fail.

Stdlib only; talks to the database through psql, the same way the tests do.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supabase_config  # noqa: E402
from test_database import MIGRATIONS, exe, find_bin  # noqa: E402

RECORD_TABLE = """
create schema if not exists supabase_migrations;
create table if not exists supabase_migrations.schema_migrations (
  version text primary key,
  statements text[],
  name text
);
"""

QUOTE = "$pocketful_migration$"


def psql(config: supabase_config.SupabaseConfig, *args: str) -> subprocess.CompletedProcess:
    connection, env = config.psql_connection()
    return subprocess.run(
        [str(find_bin() / exe("psql")), "-X", "-q", "-v", "ON_ERROR_STOP=1", *connection, *args],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PGCLIENTENCODING": "UTF8", **env},
    )


def applied(config: supabase_config.SupabaseConfig) -> set[str]:
    result = psql(config, "-c", RECORD_TABLE, "-tA", "-c", "select version from supabase_migrations.schema_migrations")
    if result.returncode != 0:
        raise SystemExit(f"Could not reach the database:\n{result.stderr.strip()}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def apply(config: supabase_config.SupabaseConfig, migration: Path) -> None:
    version, _, name = migration.stem.partition("_")
    body = migration.read_text(encoding="utf-8")
    if QUOTE in body:
        raise SystemExit(f"{migration.name} contains {QUOTE}, which this script uses to quote it.")
    script = (
        "begin;\n"
        f"\\i '{migration.as_posix()}'\n"
        "insert into supabase_migrations.schema_migrations (version, name, statements)\n"
        f"values ('{version}', '{name}', array[{QUOTE}{body}{QUOTE}]);\n"
        "commit;\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as f:
        f.write(script)
    try:
        result = psql(config, "-f", f.name)
    finally:
        os.unlink(f.name)
    if result.returncode != 0:
        print(f"FAIL  {migration.name}")
        print(result.stderr.strip())
        raise SystemExit(1)
    print(f"ok    {migration.name}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="apply every migration not yet applied")
    args = parser.parse_args()

    config = supabase_config.load()
    done = applied(config)
    migrations = sorted(MIGRATIONS.glob("*.sql"))
    pending = [m for m in migrations if m.stem.partition("_")[0] not in done]

    print(f"Project {config.ref}: {len(migrations) - len(pending)} applied, {len(pending)} pending")
    for m in pending:
        print(f"      pending  {m.name}")
    if pending and args.apply:
        for m in pending:
            apply(config, m)
    elif pending:
        print("\nRun with --apply to apply them.")


if __name__ == "__main__":
    main()
