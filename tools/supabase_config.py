"""
Where Pocketful's Supabase project is, and the admin credentials for it.

They live in a file outside every repository -- by default `~/keystores/pocketful-supabase.json`,
beside the app's release signing key, or wherever POCKETFUL_SUPABASE points:

    {
      "url": "https://<project ref>.supabase.co",
      "secret_key": "<the secret / service_role key>",
      "database_password": "<the password chosen when the project was created>"
    }

The secret key is for the REST API, which is all the editor and the nightly jobs need from
Supabase. Files the app downloads are not here but on Cloudflare R2; see tools/r2.py. The database password is only for applying migrations, which talk to PostgreSQL
directly. That direct address is IPv6-only on Supabase; a network without IPv6 can instead
put the session pooler's full connection string in "database_url", which wins when present.

Stdlib only, because the editor imports it too.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

KEY_FILE = Path(os.environ.get("POCKETFUL_SUPABASE") or Path.home() / "keystores" / "pocketful-supabase.json")


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    secret_key: str
    database_password: str | None
    database_url: str | None

    @property
    def ref(self) -> str:
        return urllib.parse.urlparse(self.url).netloc.split(".")[0]

    def psql_connection(self) -> tuple[list[str], dict[str, str]]:
        """psql arguments and environment for the database, keeping the password off the command line."""
        env = {"PGSSLMODE": "require", "PGCONNECT_TIMEOUT": "15"}
        if self.database_url:
            return ["-d", self.database_url], env
        if not self.database_password:
            raise SystemExit(f"{KEY_FILE} has no database_password (or database_url).")
        env["PGPASSWORD"] = self.database_password
        return ["-h", f"db.{self.ref}.supabase.co", "-p", "5432", "-U", "postgres", "-d", "postgres"], env


def load() -> SupabaseConfig:
    if not KEY_FILE.exists():
        raise SystemExit(f"No Supabase credentials at {KEY_FILE}. See tools/supabase_config.py.")
    raw = json.loads(KEY_FILE.read_text(encoding="utf-8"))
    parsed = urllib.parse.urlparse(raw.get("url") or "")
    if not parsed.netloc.endswith(".supabase.co"):
        raise SystemExit(f"{KEY_FILE}: url should look like https://<project ref>.supabase.co")
    if not raw.get("secret_key"):
        raise SystemExit(f"{KEY_FILE} has no secret_key.")
    return SupabaseConfig(
        # Only the origin: the dashboard offers addresses with /rest/v1/ on the end.
        url=f"{parsed.scheme}://{parsed.netloc}",
        secret_key=raw["secret_key"],
        database_password=raw.get("database_password"),
        database_url=raw.get("database_url") if str(raw.get("database_url", "")).startswith("postgres") else None,
    )
