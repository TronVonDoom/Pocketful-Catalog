"""
TCGplayer's catalog and prices, by way of tcgcsv.com, politely.

TCGCSV republishes TCGplayer's product catalog and market prices as plain JSON: a list of
groups (sets) per category, and per group its products and today's prices. Its terms are
one pull a day and an explicit request to cache rather than ask live, so every file here is
kept on disk and asked for again only when it is old:

    groups      a day      a new set appears a handful of times a year
    products    a week     a set's products do not change once it is out
    prices      20 hours   rebuilt by TCGCSV once a day

Used by the editor, when you ask it to match a set's printings to TCGplayer, and by the
nightly price job. Both read the same cache, so a match made in the editor and the price the
job reads come from the same files.

For tests, `fixtures` is a folder of recorded answers named like the cache (groups-3.json,
products-604.json, prices-604.json), and nothing is fetched.

Stdlib only.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://tcgcsv.com/tcgplayer"
USER_AGENT = "Pocketful-catalog/2.0 (+https://github.com/TronVonDoom/Pocketful-Catalog)"
CACHE = Path(__file__).resolve().parent.parent / "catalog" / ".tcgcsv" / "v2"

POKEMON = 3
POKEMON_JAPAN = 85
CATEGORY_BY_LANGUAGE = {"en": POKEMON, "jp": POKEMON_JAPAN}

DAY = 24 * 60 * 60
TTL = {"groups": DAY, "products": 7 * DAY, "prices": 20 * 60 * 60}


class TcgcsvError(Exception):
    pass


class Tcgcsv:
    def __init__(self, cache: Path = CACHE, fixtures: Path | None = None, pause: float = 0.1):
        self.cache = cache
        self.fixtures = fixtures
        self.pause = pause

    def _rows(self, kind: str, name: str, url: str) -> list[dict]:
        if self.fixtures:
            file = self.fixtures / name
            if not file.exists():
                return []
            return _results(json.loads(file.read_text(encoding="utf-8")))

        path = self.cache / name
        if path.exists() and time.time() - path.stat().st_mtime < TTL[kind]:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                pass

        last_error = None
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=60) as response:
                    rows = _results(json.loads(response.read()))
                self.cache.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(rows), encoding="utf-8")
                tmp.replace(path)
                time.sleep(self.pause)
                return rows
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return []
                last_error = e
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                last_error = e
            time.sleep(1.5 * (attempt + 1))
        # An old copy beats nothing when TCGCSV is having a bad hour.
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        raise TcgcsvError(f"tcgcsv.com did not answer for {url}: {last_error}")

    def groups(self, category: int = POKEMON) -> list[dict]:
        return self._rows("groups", f"groups-{category}.json", f"{BASE}/{category}/groups")

    def products(self, group_id: int, category: int = POKEMON) -> list[dict]:
        return self._rows("products", f"products-{group_id}.json", f"{BASE}/{category}/{group_id}/products")

    def prices(self, group_id: int, category: int = POKEMON) -> dict[int, dict[str, int]]:
        """Product ID to TCGplayer printing name ("Holofoil") to market price in cents."""
        out: dict[int, dict[str, int]] = {}
        for row in self._rows("prices", f"prices-{group_id}.json", f"{BASE}/{category}/{group_id}/prices"):
            market = row.get("marketPrice")
            if market is None or row.get("productId") is None or not row.get("subTypeName"):
                continue
            cents = round(float(market) * 100)
            if cents > 0:
                out.setdefault(int(row["productId"]), {})[row["subTypeName"]] = cents
        return out


def _results(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("results") or []
    return []
