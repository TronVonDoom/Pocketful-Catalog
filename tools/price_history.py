#!/usr/bin/env python3
"""
What every card has been worth, over time, one small file per set.

The price file answers "what is it worth today", and a price with nothing to compare it to
is half an answer: whether a card is climbing or sliding is most of what someone checking
their collection wants to know. So the nightly price job also keeps a history.

Shape
-----
One document per catalog set, published beside the price file as
`history-<set id>.json.gz` on the `price-history` release tag:

    {
      "schema": 1,
      "set": "mep",
      "dates": ["2024-02-08", "2024-02-15", ..., "2026-09-12"],
      "cards":   {"mep-070": {"holofoil": [null, 305, ..., 266]}},
      "special": {"mep-070": {"holo~pokemon-center": {"holofoil": [..., 2905]}}}
    }

Every series is aligned with `dates`, null where the card had no quote that day. The
figures are exactly what the price file carried that day -- the same keys, the same
plain/special split -- so a chart and a price tag can never disagree.

Why per set
-----------
The app needs the history of the card it is showing, or of the cards in one collection,
never all twenty thousand. A single file would be several megabytes downloaded to draw one
line; a set is tens of kilobytes, and a collection spans the sets it spans.

How much is kept
----------------
Every day for the last five weeks, and one day per week before that, all the way back.
That is what the app's ranges need -- a month is drawn day by day, and a year or more reads
the same from weekly points -- and it keeps a set's file from growing by a point a day
forever.

Backfill
--------
TCGCSV publishes a daily archive of TCGplayer's prices from 2024-02-08. `--backfill` reads
those archives (weekly, then daily for the kept window) and prices every card through
today's product match, so the history starts complete rather than on the day this was
built. It is run once, by hand, from the Prices workflow.

Usage:
    python tools/price_history.py --backfill --history dist/history   # after pull_prices.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
RESOLUTION = DIST / "resolution.json"

SCHEMA = 1
DAILY_DAYS = 35
FIRST_ARCHIVE = dt.date(2024, 2, 8)
ARCHIVE = "https://tcgcsv.com/archive/tcgplayer/prices-{}.ppmd.7z"
POKEMON = 3
USER_AGENT = "Pocketful-catalog-builder/0.4 (+https://github.com/TronVonDoom/Pocketful)"


def shard_path(directory: Path, set_id: str) -> Path:
    return directory / f"history-{set_id}.json.gz"


def load(directory: Path, set_id: str) -> dict:
    path = shard_path(directory, set_id)
    if path.exists():
        try:
            doc = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            if doc.get("schema") == SCHEMA:
                return doc
        except (OSError, ValueError):
            pass
    return {"schema": SCHEMA, "set": set_id, "dates": [], "cards": {}, "special": {}}


def save(directory: Path, doc: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    shard_path(directory, doc["set"]).write_bytes(gzip.compress(raw, compresslevel=9))


def _series_maps(doc: dict):
    """Every (key path, series list) in a document, for reshaping them all together."""
    for card, finishes in doc["cards"].items():
        for finish, series in finishes.items():
            yield series
    for card, printings in doc["special"].items():
        for printing, finishes in printings.items():
            for finish, series in finishes.items():
                yield series


def record(doc: dict, date: str, cards: dict[str, dict[str, int]],
           special: dict[str, dict[str, dict[str, int]]]) -> None:
    """
    Writes one day's figures into `doc`, replacing that day if it is already there.

    A date lands in order, so a backfill can fill in the past around days a nightly run
    already recorded.
    """
    dates = doc["dates"]
    if date in dates:
        index = dates.index(date)
        for series in _series_maps(doc):
            series[index] = None
    else:
        index = next((i for i, d in enumerate(dates) if d > date), len(dates))
        dates.insert(index, date)
        for series in _series_maps(doc):
            series.insert(index, None)

    width = len(dates)
    for card, finishes in cards.items():
        target = doc["cards"].setdefault(card, {})
        for finish, cents in finishes.items():
            target.setdefault(finish, [None] * width)[index] = cents
    for card, printings in special.items():
        target = doc["special"].setdefault(card, {})
        for printing, finishes in printings.items():
            inner = target.setdefault(printing, {})
            for finish, cents in finishes.items():
                inner.setdefault(finish, [None] * width)[index] = cents


def thin(doc: dict, today: dt.date) -> None:
    """
    Keeps every day of the last DAILY_DAYS and the latest day of each week before that,
    and drops any series left with nothing in it.
    """
    dates = doc["dates"]
    cutoff = today - dt.timedelta(days=DAILY_DAYS)
    keep: list[int] = []
    last_week_index: dict[tuple[int, int], int] = {}
    for i, text in enumerate(dates):
        day = dt.date.fromisoformat(text)
        if day >= cutoff:
            keep.append(i)
        else:
            last_week_index[day.isocalendar()[:2]] = i
    keep = sorted(set(keep) | set(last_week_index.values()))
    if len(keep) != len(dates):
        doc["dates"] = [dates[i] for i in keep]
        for card in list(doc["cards"]):
            for finish in list(doc["cards"][card]):
                doc["cards"][card][finish] = [doc["cards"][card][finish][i] for i in keep]
        for card in list(doc["special"]):
            for printing in list(doc["special"][card]):
                for finish in list(doc["special"][card][printing]):
                    series = doc["special"][card][printing][finish]
                    doc["special"][card][printing][finish] = [series[i] for i in keep]

    for card in list(doc["cards"]):
        doc["cards"][card] = {f: s for f, s in doc["cards"][card].items() if any(v is not None for v in s)}
        if not doc["cards"][card]:
            del doc["cards"][card]
    for card in list(doc["special"]):
        for printing in list(doc["special"][card]):
            kept = {f: s for f, s in doc["special"][card][printing].items() if any(v is not None for v in s)}
            if kept:
                doc["special"][card][printing] = kept
            else:
                del doc["special"][card][printing]
        if not doc["special"][card]:
            del doc["special"][card]


def previous(doc: dict, today: str) -> tuple[str | None, dict, dict]:
    """
    The most recent recorded day before `today`, and every figure from it.

    What the price file publishes as `previous`, so a price tag can say how far a card has
    moved without the app downloading any history at all.
    """
    earlier = [i for i, d in enumerate(doc["dates"]) if d < today]
    if not earlier:
        return None, {}, {}
    index = earlier[-1]
    cards = {}
    for card, finishes in doc["cards"].items():
        kept = {f: s[index] for f, s in finishes.items() if s[index] is not None}
        if kept:
            cards[card] = kept
    special = {}
    for card, printings in doc["special"].items():
        for printing, finishes in printings.items():
            kept = {f: s[index] for f, s in finishes.items() if s[index] is not None}
            if kept:
                special.setdefault(card, {})[printing] = kept
    return doc["dates"][index], cards, special


# ------------------------------------------------------------------------------ backfill


def archive_dates(today: dt.date) -> list[dt.date]:
    """Weekly from the first archive until the daily window, then every day until yesterday."""
    out = []
    cutoff = today - dt.timedelta(days=DAILY_DAYS)
    day = FIRST_ARCHIVE
    while day < cutoff:
        out.append(day)
        day += dt.timedelta(days=7)
    day = cutoff
    while day < today:
        out.append(day)
        day += dt.timedelta(days=1)
    return out


def archive_quotes(day: dt.date, work: Path, seven_zip: str) -> dict[int, dict[str, int]] | None:
    """Every Pokemon product's quotes on `day`, from TCGCSV's archive, or None if missing."""
    import requests  # noqa: PLC0415  (only the backfill needs it; the module is imported by pull_prices)

    from tcgplayer import finish_key  # noqa: PLC0415

    target = work / f"{day}.7z"
    for attempt in range(3):
        try:
            with requests.get(ARCHIVE.format(day), headers={"User-Agent": USER_AGENT},
                              timeout=120, stream=True) as r:
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                with target.open("wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
            break
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
    else:
        return None

    out_dir = work / str(day)
    # Only the Pokemon category is extracted: the archive holds every game TCGplayer sells.
    subprocess.run([seven_zip, "x", "-y", f"-o{out_dir}", str(target), f"*/{POKEMON}/*"],
                   check=False, capture_output=True)
    quotes: dict[int, dict[str, int]] = {}
    for path in out_dir.rglob("prices"):
        try:
            rows = json.loads(path.read_text(encoding="utf-8")).get("results") or []
        except (OSError, ValueError):
            continue
        for row in rows:
            market = row.get("marketPrice")
            key = finish_key(row.get("subTypeName"))
            if market and market > 0 and key:
                quotes.setdefault(row["productId"], {})[key] = round(market * 100)
    target.unlink(missing_ok=True)
    shutil.rmtree(out_dir, ignore_errors=True)
    return quotes


def backfill(history: Path, seven_zip: str) -> None:
    from tcgplayer import plain_quotes, special_quotes  # noqa: PLC0415

    if not RESOLUTION.exists():
        raise SystemExit("No dist/resolution.json. Run pull_prices.py first; the backfill "
                         "prices the past through today's product match.")
    resolution = json.loads(RESOLUTION.read_text(encoding="utf-8"))["sets"]
    today = dt.date.today()
    docs = {set_id: load(history, set_id) for set_id in resolution}

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for day in archive_dates(today):
            quotes = archive_quotes(day, work, seven_zip)
            if not quotes:
                print(f"  {day} no archive")
                continue
            priced = 0
            for set_id, cards in resolution.items():
                day_cards, day_special = {}, {}
                for card_id, answer in cards.items():
                    plain = answer.get("product")
                    if plain and quotes.get(plain):
                        figures = plain_quotes(quotes[plain])
                        if figures:
                            day_cards[card_id] = figures
                            priced += 1
                    for key, printing in (answer.get("special") or {}).items():
                        figures = special_quotes(printing, quotes.get(printing["product"]))
                        if figures:
                            day_special.setdefault(card_id, {})[key] = figures
                record(docs[set_id], day.isoformat(), day_cards, day_special)
            print(f"  {day} {priced} cards")

    for doc in docs.values():
        thin(doc, today)
        save(history, doc)
    print(f"{len(docs)} history files written to {history}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", type=Path, default=DIST / "history")
    ap.add_argument("--backfill", action="store_true", help="fill in the past from TCGCSV's archives")
    ap.add_argument("--7z", dest="seven_zip", default=shutil.which("7z") or shutil.which("7za") or "7z")
    args = ap.parse_args()
    if args.backfill:
        backfill(args.history, args.seven_zip)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
