"""
The editor, end to end, against nothing real.

Starts the same throwaway PostgreSQL tools/test_database.py uses, puts PostgREST in front of
it the way Supabase does, runs editor/server.py against that with a folder standing in for
R2 and recorded TCGdex answers standing in for TCGdex, and then walks one set through its
whole life over the editor's own API: create a series and a set, import it, accept cards,
review them, give them pictures, publish, and edit after publishing. Along the way it checks
the editor refuses what it should.

Nothing touches Supabase, R2 or TCGdex, so it can run as often as the editor changes.

PostgREST is the one thing this needs that is not already on the machine. The first run
downloads the Windows build of PostgREST 14, the major version Supabase runs,
into %LOCALAPPDATA%\\pocketful-test, and later runs reuse it.

    python tools/test_editor.py
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_database import find_bin, throwaway_database  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EDITOR = ROOT / "editor" / "server.py"
FIXTURES = ROOT / "editor" / "tests" / "fixtures" / "tcgdex"

DB_PORT = 55439
REST_PORT = 55440
EDITOR_PORT = 55441
POSTGREST_VERSION = "v14.18"
JWT_SECRET = "pocketful-local-test-secret-that-is-long-enough"

FAILURES: list[str] = []


# ------------------------------------------------------------------------------ plumbing


def postgrest_exe() -> Path:
    home = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "pocketful-test" / f"postgrest-{POSTGREST_VERSION}"
    exe = home / "postgrest.exe"
    if not exe.exists():
        url = (f"https://github.com/PostgREST/postgrest/releases/download/{POSTGREST_VERSION}/"
               f"postgrest-{POSTGREST_VERSION}-windows-x86-64.zip")
        print(f"      downloading PostgREST {POSTGREST_VERSION} for the tests into {home}", flush=True)
        home.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=120) as response:
            zipfile.ZipFile(io.BytesIO(response.read())).extractall(home)
    return exe


def service_jwt() -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"role": "service_role", "iss": "pocketful-test"}).encode())
    signature = b64(hmac.new(JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def wait_for(url: str, seconds: float = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def call(method: str, path: str, body=None, origin: str | None = "own", content_type="application/json"):
    headers = {}
    if origin == "own":
        headers["Origin"] = f"http://127.0.0.1:{EDITOR_PORT}"
    elif origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    request = urllib.request.Request(f"http://127.0.0.1:{EDITOR_PORT}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            kind = response.headers.get_content_type()
            return response.status, (json.loads(raw) if kind == "application/json" else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def check(ok: bool, label: str, detail=None) -> None:
    if ok:
        print(f"ok    {label}", flush=True)
    else:
        print(f"FAIL  {label}" + (f"\n      {json.dumps(detail, ensure_ascii=False)[:600]}" if detail is not None else ""), flush=True)
        FAILURES.append(label)


def expect(status_body, status: int, label: str):
    got, body = status_body
    check(got == status, label, {"status": got, "body": body})
    return body


def fake_webp(width: int, height: int) -> str:
    """Just enough of a lossless WebP header for the editor to read its size."""
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\x00" * 16
    chunk = b"VP8L" + len(payload).to_bytes(4, "little") + payload
    return base64.b64encode(b"RIFF" + (4 + len(chunk)).to_bytes(4, "little") + b"WEBP" + chunk).decode()


# ------------------------------------------------------------------------------ the walk


def walk(store: Path) -> None:
    boot = expect(call("GET", "/api/bootstrap"), 200, "the editor starts and reads the catalogs")
    check(len(boot["catalogs"]) == 4 and len(boot["words"]) == 139, "four catalogs and the kept word list",
          {"catalogs": len(boot["catalogs"]), "words": len(boot["words"])})

    # Safety -----------------------------------------------------------------------------
    expect(call("POST", "/api/series", {"catalog_id": "ptcg-en", "code": "x", "name": "X"},
                origin="https://evil.example"), 403, "a request from another web page is refused")
    expect(call("POST", "/api/series", {"catalog_id": "ptcg-en", "code": "x", "name": "X"},
                content_type="text/plain"), 415, "a write that is not JSON is refused")
    expect(call("POST", "/api/series", {"catalog_id": "ptcg-en", "code": "x", "name": "X", "locked": True}),
           400, "a field the page may not write is refused")

    # Series and set ---------------------------------------------------------------------
    series = expect(call("POST", "/api/series", {"catalog_id": "ptcg-en", "code": "base", "name": "Base", "sort": 1}),
                    201, "create a series")
    check(series.get("id") == "ptcg-en-base", "the series ID is built from its parts", series)
    the_set = expect(call("POST", "/api/sets", {"series_id": "ptcg-en-base", "code": "base01", "name": "Base Set",
                                                "release_date": "1999-01-09", "printed_total": 102}),
                     201, "create a set")
    check(the_set.get("id") == "ptcg-en-base01", "the set ID leaves the series out", the_set)
    expect(call("POST", "/api/sets", {"series_id": "ptcg-en-base", "code": "Base 01", "name": "Bad"}),
           400, "a set code the database will not take is refused with its reason")

    # Import -----------------------------------------------------------------------------
    listing = expect(call("GET", "/api/tcgdex/sets?catalog=ptcg-en"), 200, "list TCGdex's sets when asked")
    check(listing["sets"][0]["id"] == "base1", "TCGdex's set list is shown", listing)
    empty = expect(call("GET", "/api/sets/ptcg-en-base01/candidates"), 200, "a set that was never imported")
    check(empty["import"] is None and empty["candidates"] == [], "nothing is imported until asked", empty)

    imported = expect(call("POST", "/api/sets/ptcg-en-base01/import", {"source": "tcgdex", "key": "base1"}),
                      200, "import Base Set from TCGdex")
    check(imported["imported"]["cards"] == 4, "four recorded cards came in", imported["imported"])
    cards = expect(call("GET", "/api/sets/ptcg-en-base01"), 200, "read the set")["cards"]
    check(cards == [], "importing creates no cards", cards)
    by_key = {c["key"]: c for c in imported["candidates"]}
    check(all(c["status"] == "new" for c in by_key.values()), "every candidate is new", by_key)
    check(by_key["base1-1"]["printings"] == ["holo", "shadowless-holo", "1st-edition-holo", "1999-2000-copyright-holo"]
          or sorted(by_key["base1-1"]["printings"]) == sorted(["holo", "shadowless-holo", "1st-edition-holo", "1999-2000-copyright-holo"]),
          "Alakazam's printings map onto the word list", by_key["base1-1"]["printings"])
    check("shadowless-normal-red-cheeks" in by_key["base1-58"]["printings"]
          and "1st-edition-normal-red-cheeks" in by_key["base1-58"]["printings"]
          and "normal-poketour-99" in by_key["base1-58"]["printings"],
          "Pikachu's red cheeks and PokéTour stamp map, and its jumbo card is left out", by_key["base1-58"]["printings"])
    check(any("Mystery Rare" in p for p in by_key["base1-2"]["problems"]),
          "a rarity that is not a term is reported, not invented", by_key["base1-2"])
    check(by_key["base1-1"]["printed_number"] == "1/102", "the printed number is rebuilt as printed", by_key["base1-1"])

    accepted = expect(call("POST", "/api/sets/ptcg-en-base01/accept", {"keys": ["base1-1", "base1-2", "base1-58", "base1-96"]}),
                      200, "accept all four")
    check(accepted["accepted"]["created"] == 4, "four cards were made", accepted["accepted"])
    check(all(c["status"] == "accepted" for c in accepted["candidates"]), "the candidates now say accepted", accepted["candidates"])
    again = expect(call("POST", "/api/sets/ptcg-en-base01/accept", {"keys": ["base1-1"]}), 200, "accept one again")
    check(again["accepted"]["created"] == 0 and "base1-1" in again["accepted"]["skipped"],
          "accepting twice makes nothing twice", again["accepted"])

    detail = expect(call("GET", "/api/sets/ptcg-en-base01"), 200, "read the set again")
    ids = [c["id"] for c in detail["cards"]]
    check(ids == ["ptcg-en-base01-1", "ptcg-en-base01-2", "ptcg-en-base01-58", "ptcg-en-base01-96"],
          "cards are in printed order with IDs built from the number", ids)
    alakazam = next(c for c in detail["cards"] if c["number"] == "1")
    check(alakazam["review"] == "unreviewed" and alakazam["types"] == ["psychic"] and alakazam["subtypes"] == ["stage-2"]
          and alakazam["rarity"] == "rare" and alakazam["abilities"][0]["kind"] == "pokemon-power",
          "an accepted card starts unreviewed, with its fields in the catalog's terms", alakazam)
    blastoise = next(c for c in detail["cards"] if c["number"] == "2")
    check(blastoise["rarity"] is None and "Mystery Rare" in (blastoise["notes"] or ""),
          "what did not map is left empty and written in the card's notes", blastoise)
    dce = next(c for c in detail["cards"] if c["number"] == "96")
    check(dce["subtypes"] == ["special-energy"] and dce["rules"], "an energy card keeps its text", dce)

    card = expect(call("GET", "/api/cards/ptcg-en-base01-1"), 200, "open a card")
    check(card["sources"] and card["sources"][0]["key"] == "base1-1", "the card shows what TCGdex said", card.get("sources"))
    check(card["next"] == "ptcg-en-base01-2" and card["previous"] is None and card["count"] == 4,
          "the card knows its neighbours", {k: card[k] for k in ("previous", "next", "count")})

    # Editing and review -----------------------------------------------------------------
    expect(call("PATCH", "/api/cards/ptcg-en-base01-2", {"rarity": "made-up"}), 400,
           "an unknown rarity is refused by the database, with its reason")
    term = expect(call("POST", "/api/terms", {"kind": "rarity", "code": "mystery-rare", "labels": {"en": "Mystery Rare"}}),
                  201, "add a rarity term")
    check(term["code"] == "mystery-rare", "the new term is there", term)
    expect(call("PATCH", "/api/cards/ptcg-en-base01-2", {"rarity": "mystery-rare"}), 200, "use the new term")
    word = expect(call("POST", "/api/words", {"word": "test-stamp", "kind": "stamp", "label": "Test Stamp"}), 201, "add a word")
    check(word["word"] == "test-stamp", "the new word is there", word)
    printing = expect(call("POST", "/api/printings", {"card_id": "ptcg-en-base01-96", "finish": "normal", "stamps": ["test-stamp"]}),
                      201, "add a printing by hand")
    check(printing["id"] == "ptcg-en-base01-96_normal-test-stamp", "its ID is built from its words", printing)
    expect(call("DELETE", f"/api/printings/{printing['id']}"), 200, "delete an unpublished printing")

    flagged = expect(call("PATCH", "/api/cards/ptcg-en-base01-58", {"review": "flagged", "review_note": "check the red cheeks"}),
                     200, "flag a card")
    check(flagged["review"] == "flagged" and flagged["reviewed_at"] is None, "a flag has no review date", flagged)

    problems = expect(call("GET", "/api/sets/ptcg-en-base01/problems"), 200, "ask what stands in the way")["problems"]
    check(any(p["problem"].startswith("flagged: check the red cheeks") for p in problems)
          and any("no logo" in p["problem"] for p in problems),
          "the flag and the missing logo are both listed", problems)
    refused = call("POST", "/api/sets/ptcg-en-base01/publish", {})
    check(refused[0] == 409 and refused[1].get("problems"), "publishing is refused while anything stands in the way", refused)

    for c in detail["cards"]:
        expect(call("PATCH", f"/api/cards/{c['id']}", {"review": "reviewed"}), 200, f"review {c['id']}")
    for p in detail["printings"]:
        call("PATCH", f"/api/printings/{p['id']}", {"review": "reviewed"})
    expect(call("PATCH", "/api/sets/ptcg-en-base01", {"no_symbol": True}), 200, "mark the set as having no symbol")

    # Pictures ---------------------------------------------------------------------------
    expect(call("POST", "/api/images", {"subject_kind": "card", "subject_id": "ptcg-en-base01-1", "role": "front",
                                        "image": fake_webp(900, 1256), "thumb": fake_webp(245, 342)}),
           400, "a picture larger than the standard is refused")
    expect(call("POST", "/api/images", {"subject_kind": "card", "subject_id": "ptcg-en-base01-1", "role": "front",
                                        "image": base64.b64encode(b"not a picture at all").decode(), "thumb": fake_webp(245, 342)}),
           400, "something that is not WebP is refused")
    front = expect(call("POST", "/api/images", {"subject_kind": "card", "subject_id": "ptcg-en-base01-1", "role": "front",
                                                "image": fake_webp(734, 1024), "thumb": fake_webp(245, 342),
                                                "source_id": "tcgdex", "source_url": "https://assets.tcgdex.net/en/base/base1/1/high.webp"}),
                   201, "give Alakazam a picture")
    check(front["chosen"] and (store / "public" / front["path"]).is_file() and (store / "public" / front["thumb_path"]).is_file()
          and not front["below_standard"], "the picture and its thumbnail are in storage, chosen", front)
    soft = expect(call("POST", "/api/images", {"subject_kind": "card", "subject_id": "ptcg-en-base01-2", "role": "front",
                                               "image": fake_webp(600, 820), "thumb": fake_webp(245, 342),
                                               "original": base64.b64encode(b"\x89PNG pretend").decode(),
                                               "original_type": "image/png"}),
                  201, "give Blastoise a small uploaded picture")
    check(soft["below_standard"] and soft["original_path"] and (store / "private" / soft["original_path"]).is_file(),
          "a small picture is marked, and an upload's original is kept privately", soft)
    replacement = expect(call("POST", "/api/images", {"subject_kind": "card", "subject_id": "ptcg-en-base01-2", "role": "front",
                                                      "image": fake_webp(734, 1020), "thumb": fake_webp(245, 342)}),
                         201, "replace Blastoise's picture")
    pictures = expect(call("GET", "/api/cards/ptcg-en-base01-2"), 200, "read Blastoise")["images"]
    check(sum(1 for i in pictures if i["chosen"]) == 1 and next(i for i in pictures if i["chosen"])["id"] == replacement["id"],
          "only the newest picture is chosen; the old one stays as a candidate", pictures)
    expect(call("PATCH", f"/api/images/{soft['id']}", {"chosen": True}), 200, "choose the old picture again")
    pictures = expect(call("GET", "/api/cards/ptcg-en-base01-2"), 200, "read Blastoise again")["images"]
    check(next(i for i in pictures if i["chosen"])["id"] == soft["id"], "choosing swaps which picture is in use", pictures)
    expect(call("POST", "/api/images", {"subject_kind": "set", "subject_id": "ptcg-en-base01", "role": "logo",
                                        "image": fake_webp(400, 160)}), 201, "give the set a logo")
    expect(call("PATCH", "/api/cards/ptcg-en-base01-58", {"no_image": True}), 200, "mark Pikachu as having no picture")
    expect(call("PATCH", "/api/cards/ptcg-en-base01-96", {"no_image": True}), 200, "mark the energy as having no picture")

    problems = expect(call("GET", "/api/sets/ptcg-en-base01/problems"), 200, "ask again")["problems"]
    check([p["problem"] for p in problems] == ["cards without a picture need a card back for this catalog or series"],
          "only the card back is missing now", problems)
    expect(call("POST", "/api/images", {"subject_kind": "catalog", "subject_id": "ptcg-en", "role": "back",
                                        "image": fake_webp(734, 1024), "thumb": fake_webp(245, 342)}),
           201, "give English a card back")

    # Publish ----------------------------------------------------------------------------
    published = expect(call("POST", "/api/sets/ptcg-en-base01/publish", {}), 200, "publish Base Set")
    check(published.get("file") == "catalog/sets/ptcg-en-base01.v1.json.gz" and published.get("cards") == 4,
          "version 1 went out with four cards", published)
    doc = json.loads(gzip.decompress((store / "public" / published["file"]).read_bytes()))
    check(doc["schema"] == 2 and doc["logo"] and "symbol" not in doc and len(doc["cards"]) == 4,
          "the set file has the logo, no symbol, and every card", {k: doc.get(k) for k in ("schema", "logo", "symbol")})
    alakazam_doc = doc["cards"][0]
    check(alakazam_doc["image"] == front["path"] and alakazam_doc["printings"][0]["id"] == "ptcg-en-base01-1_holo"
          and alakazam_doc["printedNumber"] == "1/102",
          "a card carries its picture, printed number and printings, plainest first", alakazam_doc)
    check("image" not in doc["cards"][2], "a card with no picture has no image, so the app draws the back", doc["cards"][2])
    index = json.loads((store / "public" / "catalog" / "index.json").read_bytes())
    entry = index["catalogs"][0]["series"][0]["sets"][0]
    check(index["catalogs"][0]["id"] == "ptcg-en" and index["catalogs"][0]["back"] and entry["version"] == 1
          and entry["file"] == published["file"] and index["publicUrl"],
          "the index lists the set, its file and the catalog's card back", index)
    check(len(index["catalogs"]) == 1, "catalogs with nothing published are not in the index", [c["id"] for c in index["catalogs"]])

    # After publishing -------------------------------------------------------------------
    expect(call("PATCH", "/api/cards/ptcg-en-base01-1", {"number": "1a"}), 400,
           "a published card's number cannot change, and the database says why")
    expect(call("DELETE", "/api/cards/ptcg-en-base01-1"), 409, "a published card cannot be deleted")
    expect(call("DELETE", "/api/sets/ptcg-en-base01"), 409, "a published set cannot be deleted")
    expect(call("PATCH", "/api/sets/ptcg-en-base01", {"name": "Base Set (renamed)"}), 200, "rename the published set")
    boot = expect(call("GET", "/api/bootstrap"), 200, "read the overview")
    status = next(s["status"] for s in boot["sets"] if s["id"] == "ptcg-en-base01")
    check(status == "published_changed", "the set now has unpublished changes", status)
    expect(call("POST", "/api/publish-index", {}), 200, "publish the index alone")
    index = json.loads((store / "public" / "catalog" / "index.json").read_bytes())
    check(index["catalogs"][0]["series"][0]["sets"][0]["name"] == "Base Set",
          "the index keeps the published name until the set is published again", index["catalogs"][0]["series"][0]["sets"][0])
    second = expect(call("POST", "/api/sets/ptcg-en-base01/publish", {}), 200, "publish again")
    check(second["file"].endswith(".v2.json.gz") and (store / "public" / published["file"]).is_file(),
          "version 2 is a new file and version 1 is still there", second)
    index = json.loads((store / "public" / "catalog" / "index.json").read_bytes())
    check(index["catalogs"][0]["series"][0]["sets"][0]["name"] == "Base Set (renamed)", "now the index has the new name")

    lists = expect(call("GET", "/api/lists/no-picture?catalog=ptcg-en"), 200, "list published cards without a picture")
    check(sorted(c["number"] for c in lists["cards"]) == ["58", "96"], "Pikachu and the energy are on the list", lists)

    # Re-import notices a change ---------------------------------------------------------
    changed_dir = Path(tempfile.mkdtemp(prefix="pocketful-fixtures-"))
    shutil.copytree(FIXTURES, changed_dir, dirs_exist_ok=True)
    card_file = changed_dir / "en" / "cards" / "base1-1.json"
    data = json.loads(card_file.read_text(encoding="utf-8"))
    data["hp"] = 90
    card_file.write_text(json.dumps(data), encoding="utf-8")
    return changed_dir


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="pocketful-editor-test-"))
    store = work / "store"
    processes: list[subprocess.Popen] = []
    try:
        with throwaway_database(DB_PORT):
            env = {**os.environ, "PATH": f"{find_bin()}{os.pathsep}{os.environ.get('PATH', '')}",
                   "PGRST_DB_URI": f"postgres://authenticator@localhost:{DB_PORT}/postgres",
                   "PGRST_DB_SCHEMAS": "public", "PGRST_DB_ANON_ROLE": "anon", "PGRST_JWT_SECRET": JWT_SECRET,
                   "PGRST_SERVER_HOST": "127.0.0.1", "PGRST_SERVER_PORT": str(REST_PORT), "PGRST_DB_POOL": "4"}
            rest_log = open(work / "postgrest.log", "w")
            processes.append(subprocess.Popen([str(postgrest_exe())], env=env, stdout=rest_log, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL))
            if not wait_for(f"http://127.0.0.1:{REST_PORT}/"):
                print("FAIL  start PostgREST\n" + (work / "postgrest.log").read_text(errors="replace"))
                raise SystemExit(1)
            print(f"ok    PostgREST {POSTGREST_VERSION} on localhost:{REST_PORT}", flush=True)

            def start_editor(fixtures: Path) -> subprocess.Popen:
                editor_env = {**os.environ, "POCKETFUL_REST_URL": f"http://127.0.0.1:{REST_PORT}",
                              "POCKETFUL_REST_KEY": service_jwt(), "POCKETFUL_STORE": f"local:{store}",
                              "POCKETFUL_TCGDEX_FIXTURES": str(fixtures), "PYTHONIOENCODING": "utf-8"}
                log = open(work / "editor.log", "a")
                process = subprocess.Popen([sys.executable, str(EDITOR), "--port", str(EDITOR_PORT), "--no-open"],
                                           env=editor_env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                if not wait_for(f"http://127.0.0.1:{EDITOR_PORT}/api/ping"):
                    print("FAIL  start the editor\n" + (work / "editor.log").read_text(errors="replace"))
                    raise SystemExit(1)
                return process

            editor = start_editor(FIXTURES)
            processes.append(editor)
            print(f"ok    editor on localhost:{EDITOR_PORT}", flush=True)
            changed_fixtures = walk(store)

            # TCGdex "changes its mind": a new import of the same set is noticed on the card.
            editor.terminate()
            editor.wait(10)
            processes.append(start_editor(changed_fixtures))
            expect(call("POST", "/api/sets/ptcg-en-base01/import", {"source": "tcgdex", "key": "base1"}), 200,
                   "import Base Set again after TCGdex changed a card")
            candidates = expect(call("GET", "/api/sets/ptcg-en-base01/candidates"), 200, "read the candidates")
            status = {c["key"]: c["status"] for c in candidates["candidates"]}
            check(status.get("base1-1") == "changed" and status.get("base1-2") == "accepted",
                  "only the card TCGdex changed is marked changed", status)
            card = expect(call("GET", "/api/cards/ptcg-en-base01-1"), 200, "open the changed card")
            check(card["sources"][0]["changed"] and card["sources"][0]["fields"]["hp"] == 90 and card["card"]["hp"] == 80,
                  "the card shows TCGdex's new HP beside the reviewed one", {"source": card["sources"][0]["fields"]["hp"], "card": card["card"]["hp"]})
            expect(call("PATCH", "/api/cards/ptcg-en-base01-1", {"review": "reviewed"}), 200, "review it again")
            card = expect(call("GET", "/api/cards/ptcg-en-base01-1"), 200, "open it once more")
            check(not card["sources"][0]["changed"], "reviewing again clears the change")
            shutil.rmtree(changed_fixtures, ignore_errors=True)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
        if FAILURES:
            for name in ("editor.log", "postgrest.log"):
                log = work / name
                if log.exists():
                    tail = log.read_text(errors="replace")[-3000:]
                    print(f"\n--- {name} (end) ---\n{tail}")
        shutil.rmtree(work, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed.")
        raise SystemExit(1)
    print("\nEvery editor check passed.")


if __name__ == "__main__":
    main()
