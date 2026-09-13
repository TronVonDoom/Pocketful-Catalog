# Pocketful Editor

Where the catalog is built, reviewed and published. It follows
[docs/database.md](../docs/database.md): series, sets, cards and printings live in the
catalog database in Supabase, pictures and published files on Cloudflare R2, and nothing
reaches the app until a set is published.

## Opening it

On Windows, once:

```powershell
powershell -ExecutionPolicy Bypass -File editor\install-shortcut.ps1
```

That puts **Pocketful Editor** in the Start menu and on the desktop. It opens in a window of
its own and stops when the window closes. Without the shortcut, double-click
`editor\Pocketful Editor.cmd`, or:

```bash
python editor/server.py          # a browser tab at http://127.0.0.1:8767
python editor/server.py --app    # its own window
```

It needs the two credential files in `~/keystores` (`pocketful-supabase.json` and
`pocketful-r2.json`; see `tools/supabase_config.py` and `tools/r2.py`). The page never sees
them: it talks to this editor, and only the editor talks to Supabase and R2.

## Building a set

1. **New series** in the sidebar, then **New set** in the series. Codes follow the design:
   the set code is the series code plus two digits (`base01`, `me05`).
2. **Import** (optional): one set, from TCGdex, only when you start it. It brings that set's
   cards as suggestions and creates nothing. Choose which to accept; each becomes a card
   with its printings, unreviewed. Anything that did not map to a word or term is listed
   and written into the card's notes rather than guessed.
3. **Review** each card: its picture on the left, its fields on the right, and what TCGdex
   says above them with a **Use** button beside each difference.
   - <kbd>Ctrl</kbd>+<kbd>Enter</kbd> saves, marks the card and its printings reviewed, and opens the next card.
   - <kbd>Ctrl</kbd>+<kbd>S</kbd> saves; <kbd>Alt</kbd>+<kbd>←</kbd>/<kbd>→</kbd> moves between cards.
   - **Flag** blocks publishing until the card is reviewed.
4. **Pictures**: drop a file, paste (a copied image or a <kbd>Win</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd>
   snip), choose a file, give a web address, or use TCGdex's. The page resizes to the
   standard (WebP, at most 734×1024, never enlarged, with a thumbnail) and warns when a
   picture is not card-shaped or is soft. Earlier pictures stay as choices. A card with no
   picture anywhere is marked so, and the app shows the card back: set that on the catalog's
   Overview.
5. **Publish** lists everything in the way. When the list is empty, publishing writes the
   set's next version and the index the app reads, and locks what went out.

**Words & terms** is where new variation words, rarities, types and subtypes are added, and
labels are written for each language.

## Testing

```bash
python tools/test_database.py    # the schema and its rules
python tools/test_editor.py      # the editor, end to end
```

`test_editor.py` runs the editor against a throwaway PostgreSQL behind a local PostgREST (as
Supabase does), a folder instead of R2, and recorded TCGdex answers in `editor/tests/fixtures`.
Nothing real is touched. The first run downloads PostgREST into `%LOCALAPPDATA%\pocketful-test`.

## Safety

The server listens on 127.0.0.1 only, and every request must come from the editor's own page
(checked on `Host` and `Origin`); every write must be JSON. The database refuses anything that
breaks the catalog's rules, and the editor shows its reason as it is.

The old editor, for the TCGdex-built catalog, is in `legacy/editor/`.
