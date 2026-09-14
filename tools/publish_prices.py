"""
The nightly price job: what every published printing is worth, by printing ID.

Reads the TCGplayer links from the catalog database -- every published printing that is not
withdrawn and has a TCGplayer product linked in the editor -- prices each one from TCGCSV,
and publishes two kinds of file beside the catalog on Cloudflare R2:

    prices/prices.json.gz              today's market price per printing, in cents, with the
                                       previous day's figures so a price can say how it moved
    prices/history/<set id>.json.gz    one set's printings, day by day: every day for the last
                                       five weeks and one day a week before that

The app reads both (TCG_Binder_Tracking_App, data/PublishedPrices.kt and PriceHistory.kt).
A printing with no link, or whose product TCGplayer does not quote in that printing, is left
out rather than given a nearby figure: no price is a truer answer than someone else's.

TCGCSV's terms are one pull a day and ingesting rather than querying live, which is exactly
this job: one run a night, every Pokemon group's prices once, and one file everybody reads.

    python tools/publish_prices.py               # build and publish
    python tools/publish_prices.py --dry-run     # build, say what it found, publish nothing
    python tools/publish_prices.py --backfill    # also rebuild history from TCGCSV's daily
                                                 # archives back to February 2024 (needs 7z)

Credentials come from ~/keystores (tools/supabase_config.py, tools/r2.py) or, on GitHub
Actions, from environment variables. For tests: POCKETFUL_REST_URL and POCKETFUL_REST_KEY,
POCKETFUL_STORE=local:<folder>, POCKETFUL_TCGCSV_FIXTURES=<folder>, POCKETFUL_TODAY=<date>.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS.parent / "editor"))

import tcgplayer  # noqa: E402
from tcgcsv import CATEGORY_BY_LANGUAGE, POKEMON, Tcgcsv  # noqa: E402

SCHEMA = 2
PRICES_KEY = "prices/prices.json.gz"
DAILY_DAYS = 35
FRESH = "public, max-age=3600, must-revalidate"


def history_key(set_id: str) -> str:
    return f"prices/history/{set_id}.json.gz"


def language_of(printing_id: str) -> str:
    parts = printing_id.split("-")
    return parts[1] if len(parts) > 2 else "en"


def set_of(printing_id: str) -> str:
    parts = printing_id.split("_")[0].split("-")
    return "-".join(parts[:3]) if len(parts) >= 4 else ""


# ------------------------------------------------------------------------------ inputs


def connect():
    from backend.db import Db  # noqa: PLC0415
    from backend.storage import LocalStore, R2Store  # noqa: PLC0415

    rest_url = os.environ.get("POCKETFUL_REST_URL")
    if rest_url:
        db = Db(rest_url, os.environ.get("POCKETFUL_REST_KEY", ""))
    else:
        import supabase_config  # noqa: PLC0415
        config = supabase_config.load()
        db = Db(config.url + "/rest/v1", config.secret_key)

    spec = os.environ.get("POCKETFUL_STORE", "")
    store = LocalStore(Path(spec[len("local:"):]), "http://localhost") if spec.startswith("local:") else R2Store()
    fixtures = os.environ.get("POCKETFUL_TCGCSV_FIXTURES")
    tcg = Tcgcsv(fixtures=Path(fixtures)) if fixtures else Tcgcsv()
    return db, store, tcg


def published_links(db) -> list[dict]:
    """Every printing that went out in a publish, is not withdrawn, and has a product linked."""
    withdrawn = {c["id"] for c in db.get("cards", {"select": "id", "withdrawn": "is.true"})}
    rows = db.get("printings", {"select": "id,card_id,tcgplayer_product,tcgplayer_printing",
                                "locked": "is.true", "withdrawn": "is.false", "tcgplayer_product": "not.is.null"})
    return [r for r in rows if r["card_id"] not in withdrawn]


def quotes_for(tcg: Tcgcsv, categories: set[int]) -> dict[int, dict[str, int]]:
    """Product ID to TCGplayer printing name to cents, across every group of these categories."""
    quotes: dict[int, dict[str, int]] = {}
    for category in sorted(categories):
        for group in tcg.groups(category):
            for product, by_name in tcg.prices(group["groupId"], category).items():
                quotes.setdefault(product, {}).update(by_name)
    return quotes


def price(link: dict, quotes: dict[int, dict[str, int]]) -> int | None:
    """
    A printing's figure: its product's quote in its linked TCGplayer printing. With no printing
    named, a product quoted in exactly one printing is unambiguous; one quoted in several is not.
    """
    by_name = quotes.get(link["tcgplayer_product"]) or {}
    if link.get("tcgplayer_printing"):
        return by_name.get(link["tcgplayer_printing"])
    return next(iter(by_name.values())) if len(by_name) == 1 else None


def read_gz(store, key: str) -> dict | None:
    try:
        return json.loads(gzip.decompress(store.get_public(key)))
    except Exception:  # noqa: BLE001 -- missing and unreadable are the same answer here: start fresh
        return None


def write_gz(store, key: str, doc: dict) -> None:
    body = gzip.compress(json.dumps(doc, separators=(",", ":")).encode("utf-8"), mtime=0)
    store.put_public(key, body, "application/gzip", FRESH)


# ------------------------------------------------------------------------------ documents


def build_prices(figures: dict[str, int], today: str, earlier: dict | None) -> dict:
    """Today's file. Its `previous` is the last file from an earlier day, or that file's own previous on a rerun."""
    previous = None
    if earlier and earlier.get("schema") == SCHEMA:
        if earlier.get("date") and earlier["date"] < today:
            previous = {"date": earlier["date"], "printings": earlier.get("printings") or {}}
        elif earlier.get("date") == today:
            previous = earlier.get("previous")
    doc = {
        "schema": SCHEMA,
        "fetchedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "date": today,
        "currency": "USD",
        "printings": dict(sorted(figures.items())),
    }
    if previous:
        doc["previous"] = previous
    return doc


def record(doc: dict | None, set_id: str, date: str, figures: dict[str, int]) -> dict:
    """One day's figures into a set's history, replacing that day if it is already there."""
    doc = doc if doc and doc.get("schema") == SCHEMA else {"schema": SCHEMA, "set": set_id, "dates": [], "printings": {}}
    dates: list[str] = doc["dates"]
    series: dict[str, list] = doc["printings"]
    if date in dates:
        index = dates.index(date)
    else:
        dates.append(date)
        order = sorted(range(len(dates)), key=lambda i: dates[i])
        doc["dates"] = dates = [dates[i] for i in order]
        for key in list(series):
            values = series[key] + [None]
            series[key] = [values[i] for i in order]
        index = dates.index(date)
    for printing in set(series) | set(figures):
        values = series.setdefault(printing, [None] * len(dates))
        if len(values) < len(dates):
            values.extend([None] * (len(dates) - len(values)))
        values[index] = figures.get(printing)
    return doc


def thin(doc: dict, today: dt.date) -> dict:
    """Every day for the last DAILY_DAYS, then one day per week (the last recorded in each)."""
    cutoff = (today - dt.timedelta(days=DAILY_DAYS)).isoformat()
    keep: dict[str, int] = {}
    for i, date in enumerate(doc["dates"]):
        if date >= cutoff:
            keep[date] = i
        else:
            year, week, _ = dt.date.fromisoformat(date).isocalendar()
            keep[f"w{year}-{week:02d}"] = i
    indices = sorted(keep.values())
    doc["dates"] = [doc["dates"][i] for i in indices]
    printings = {}
    for key, values in doc["printings"].items():
        kept = [values[i] if i < len(values) else None for i in indices]
        if any(v is not None for v in kept):
            printings[key] = kept
    doc["printings"] = printings
    return doc


# ------------------------------------------------------------------------------ run


def run(dry_run: bool, backfill: bool, seven_zip: str | None) -> dict:
    db, store, tcg = connect()
    today = os.environ.get("POCKETFUL_TODAY") or dt.datetime.now(dt.timezone.utc).date().isoformat()
    links = published_links(db)
    categories = {CATEGORY_BY_LANGUAGE.get(language_of(link["id"]), POKEMON) for link in links} or {POKEMON}
    print(f"{len(links)} published printings are linked to TCGplayer", flush=True)

    quotes = quotes_for(tcg, categories) if links else {}
    figures = {link["id"]: cents for link in links if (cents := price(link, quotes))}
    print(f"{len(figures)} priced today ({today})", flush=True)

    prices_doc = build_prices(figures, today, read_gz(store, PRICES_KEY))
    by_set: dict[str, dict[str, int]] = {}
    for printing, cents in figures.items():
        by_set.setdefault(set_of(printing), {})[printing] = cents

    histories: dict[str, dict] = {}
    for set_id in sorted({set_of(link["id"]) for link in links}):
        doc = record(read_gz(store, history_key(set_id)), set_id, today, by_set.get(set_id, {}))
        histories[set_id] = doc

    if backfill:
        if not seven_zip:
            raise SystemExit("--backfill needs 7z on PATH (or --7z).")
        backfill_histories(histories, links, dt.date.fromisoformat(today), seven_zip)

    for set_id, doc in histories.items():
        thin(doc, dt.date.fromisoformat(today))

    if dry_run:
        print("dry run: nothing published")
    else:
        write_gz(store, PRICES_KEY, prices_doc)
        for set_id, doc in histories.items():
            write_gz(store, history_key(set_id), doc)
        print(f"published {PRICES_KEY} and {len(histories)} set histories")
    return {"prices": prices_doc, "histories": histories}


def backfill_histories(histories: dict[str, dict], links: list[dict], today: dt.date, seven_zip: str) -> None:
    """Every archive day TCGCSV has, priced through today's links."""
    import price_history  # noqa: PLC0415 -- the old job's archive reader, keyed by tcgplayer.finish_key

    work = Path(tempfile.mkdtemp(prefix="pocketful-backfill-"))
    try:
        for day in price_history.archive_dates(today):
            archive = price_history.archive_quotes(day, work, seven_zip)
            if archive is None:
                print(f"  {day} no archive")
                continue
            by_set: dict[str, dict[str, int]] = {}
            for link in links:
                by_key = archive.get(link["tcgplayer_product"]) or {}
                wanted = tcgplayer.finish_key(link.get("tcgplayer_printing"))
                cents = by_key.get(wanted) if wanted else (next(iter(by_key.values())) if len(by_key) == 1 else None)
                if cents:
                    by_set.setdefault(set_of(link["id"]), {})[link["id"]] = cents
            for set_id in histories:
                histories[set_id] = record(histories[set_id], set_id, day.isoformat(), by_set.get(set_id, {}))
            print(f"  {day} {sum(len(v) for v in by_set.values())} printings")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="build everything, publish nothing")
    ap.add_argument("--backfill", action="store_true", help="also rebuild history from TCGCSV archives")
    ap.add_argument("--7z", dest="seven_zip", default=shutil.which("7z") or shutil.which("7za"))
    args = ap.parse_args()
    run(args.dry_run, args.backfill, args.seven_zip)


if __name__ == "__main__":
    main()
