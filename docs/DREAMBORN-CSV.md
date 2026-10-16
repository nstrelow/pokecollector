# dreamborn.ink collection CSV (Lorcana) — PicaLens #47 (B7)

Code: `backend/services/lorcana_dreamborn.py` (pure, stdlib only, no DB, no network), map
`backend/data/lorcana_dreamborn_map_v1.json`, builder `backend/scripts/build_lorcana_dreamborn_map.py`, CLI
`backend/scripts/dreamborn_csv.py`, tests `backend/tests/test_lorcana_dreamborn.py` + `tests/fixtures/dreamborn/`.
Status: module + CLI only. No API, UI or DB change (see "What waits for E3").

## Format (confidence: medium-high for the columns, medium for the cell formats)

dreamborn.ink has no public spec for its collection export. Everything below comes from third-party tools that read
real exports; nothing was fetched from dreamborn.ink itself (about ten polite requests to GitHub and the search API only, no
user data). Sources, all fetched 2026-10-15:

* Doberjohn/inkweave issue #553, "measured facts" from a real 5,329-row export:
  https://github.com/Doberjohn/inkweave/issues/553
  — header `Set Number,Card Number,Variant,Count,Name,Color,Rarity`; "`Set Number` is zero-padded"; some cards have a
  foil row only; card number `0` exists (set 9, Bruno Madrigal); `4a`..`4e` exist (set 3, Dalmatian Puppy); `Count`
  up to 10 (602 rows above 4); header may be missing in pasted files.
* Brausepaul/lorcana-app-importer (`index.html`, `mapping.csv`, BSD-2), a converter that reads the export:
  https://github.com/Brausepaul/lorcana-app-importer
  — reads `Set Number`, `Card Number`, `Variant` (`normal` | `foil`), `Count`, `Rarity`; strips leading zeros from set
  and card number; card numbers of promos look like `9/P4`, `3/PD1`, `1/D23`, `1/P1` under the numeric set; Challenge
  promos are set `C1`, card `1`..; set 9 number 0 appears as an empty string after zero stripping.
* TomParik/Lorcana (README: "compatible with dreamborn.ink and lorcana.gg"), `CSV/collection.csv` header
  `Set Number,Card Number,Variant,Count`, variants `normal` / `foil`:
  https://github.com/TomParik/Lorcana
* An older / alternative "wide" layout, one row per card: `Name,Normal,Foil,Color,Rarity,Set,Card Number`
  (entchen66/LorcanaCardCollector, `create_collection_per_color.py`): https://github.com/entchen66/LorcanaCardCollector
  — `Color` is space separated for dual ink. Import-only here (see below), low confidence on the `Set` cell format.
* A Tampermonkey script (Dralliev, gist 7d50e3498dbe61ade65fb617e10532d5, Greasy Fork 474548) writes a header-less
  `qty,qtyFoil,number,name` 4-field line from the web page. Not supported (not dreamborn's own export).

### Columns (long format, the one we read and write)

| column | meaning | example |
|---|---|---|
| Set Number | numeric set, zero-padded; or a quest set / a promo group used as a set | `001`, `010`, `Q1`, `C1` |
| Card Number | collector number, zero-padded, optional art letter, optional `/<promo group>` | `207`, `004a`, `001/P1`, `009/P4`, `024A/P2` |
| Variant | finish: `normal` or `foil` (anything else, e.g. rainbow foil, is not supported by dreamborn per Brausepaul) | `foil` |
| Count | integer copies, 0..n, not clamped | `10` |
| Name | card name in the UI language | `Ariel - On Human Legs` |
| Color | ink(s), space separated | `Emerald Steel` |
| Rarity | rarity word in the UI language (inkweave/Brausepaul list `Mythisch`, `Epic`, `Verzaubert`, `Enchanted`) | `Super Rare` |

One row per (card, finish); a card owned in both finishes has two rows. There is no language column: the export is by
print (set + number), not by language.

### Assumptions (not confirmed by any source; the parser tolerates the variations, the writer picks one)

* padding width: 3 digits for both set and card number (`001`, `004a`). The reader accepts any padding.
* Quest sets are `Q1`..`Q3` in `Set Number`, card number plain. (The real export of Q-sets was not seen.)
* Which promo groups are listed "as a set": only `C1` is confirmed (Brausepaul mapping). The writer uses
  `Set=C1, Card=001` for C1 and `Set=<set>, Card=<nnn>/<group>` for every other group (P1..P4, PD1, D23, DIS, RPH, C2,
  CC1). The reader accepts both forms for every group, and a group-as-set row only resolves if the number is unique
  in that group (it is, for all groups except P2, see below).
* line terminator `\n`, minimal CSV quoting (fields with comma / quote are quoted), UTF-8, no BOM, header present.
  The reader detects `\r\n`, BOM and a missing final newline and writes them back unchanged; an export that quotes
  every field would still round-trip row for row but not byte for byte.
* Generated `Name` is the English catalog full name (`Name - Subtitle`), `Color` is the English ink with `-` -> space,
  `Rarity` the English rarity. A real export in another UI language will carry other words; the importer never
  uses them to decide a card except to separate the two `26/P2` cards (see below).
* Rows with `Count` 0 may or may not exist in real exports; they are read and written like any other row.

## Mapping to lorcascan `print_id` (`<lang>:<set>-<number>`, scheme in lorcascan `docs/CATALOG.md`)

Identity is looked up exactly in a map generated from `catalog_v1.sqlite` (LorcanaJSON snapshot 2026-10-15,
snapshot sha256 `f9f07992...2da1`, 3,555 numbered prints; the sqlite is not read at runtime):

| dreamborn cells | print_id | note |
|---|---|---|
| `001`, `001` | `en:1-1` | zero stripped; `Set Number` `1` works too |
| `001`, `207` | `en:1-207` | Enchanted / Epic / Iconic are own numbers above the set size |
| `003`, `004a` | `en:3-4a` | art letter stays on the number; case-insensitive, catalog case kept (`7-P2-24A`) |
| `009`, `000` | `en:9-0` | |
| `001`, `001/P1` | `en:1-P1-1` | promo group between set and number |
| `C1`, `001` | `en:1-C1-1` | group used as set: found by (group, number), must be unique |
| `Q1`, `001` | `en:Q1-1` | |
| `007`, `026/P2` | ambiguous | two catalog cards (Moana / Vaiana, `~1433` / `~1663`) share it; resolved only if `Name` equals exactly one candidate's name (with or without the subtitle), else the row is unmapped |

`lang` (default `en`) is an import parameter: the number is language independent, so the result is `<lang>:` +
the same rest id, and a row whose card has no print in that language (e.g. a P1 promo in `de`) is unmapped
("no de print ..."). On export languages are folded: `en:1-1` and `de:1-1` become one row, counts summed.

### Never dropped, never guessed

A row that cannot be mapped stays in `ParsedCsv.entries` with `print_id=None` and a `reason`, is listed by
`ParsedCsv.report()` / `dreamborn_csv.py check`, and is written back verbatim on export. Reasons: unknown set,
card not in catalog, ambiguous (candidate list), unreadable number, bad count, unknown variant (e.g. `rainbow foil`),
wrong column count, no print in the chosen language. The Q3 token (`Q3-id3551`, no number) is not in the map.
Blank lines are the only thing not carried over.

### Normalized row

`DreambornEntry(print_id, count, foil, raw, line, reason, cells)`; `raw` is the source row as `{header: cell}`,
`cells` the verbatim cells (exported untouched while set; `dataclasses.replace(e, count=5, cells=None)` regenerates
the row from the map). `totals(entries)` sums repeated rows per `(print_id, foil)`. Hub rows without a source line are
`make_entry(print_id, count, foil)`.

## Round trip (the Done-when of #47)

`backend/tests/fixtures/dreamborn/sample_export.csv`: 22 hand-built rows in the documented layout: normal and foil of
one card, Enchanted, Epic, Iconic, art letters `a`/`e`, number 0, 10 copies, dual ink, names with comma and with
quotes, P1 / P4 / P2-with-letter promos, a C1 challenge promo, a quest card, the ambiguous `26/P2` pair (one resolved
by name, one not), an unknown set, an unsupported finish. Result: import -> export is byte-equal and row-equal
(22 of 22 rows, in order), 19 rows mapped, the 3 unmapped rows kept verbatim. Also: all 3,555 catalog prints
generate cells that parse back to their own `print_id`. Run:

    cd backend && nice -n 19 python3 -m unittest tests.test_lorcana_dreamborn      # 23 tests
    python3 scripts/dreamborn_csv.py check|roundtrip|json|export ...

A real export has not been available; the first one the owner supplies should be run through `check` (and
`roundtrip`), and the assumptions above confirmed or fixed (no network or account needed).

## Rebuilding the map

    python3 backend/scripts/build_lorcana_dreamborn_map.py --catalog <lorcascan>/data/catalog_vN.sqlite \
        --out backend/data/lorcana_dreamborn_map_v2.json

It refuses to overwrite (append-only). To switch, change `DEFAULT_MAP_PATH` and keep `_v1` in the repo.

## Wired into the hub (E3, PicaLens #62)

Server side only, behind `LORCANA_ENABLED=1` (default off; the deployed app has no `/api/lorcana` route until G2, #72).
Design, map of what lives where, deploy and rollback: PicaLens `docs/plans/E3-HUB-GAME-AXIS.md`; short version in `docs/GAME-AXIS.md`.

* `POST /api/lorcana/collection/import-dreamborn` (multipart `file`, `lang=en|de|fr|it`, `commit=false|true`, `mode=fill|add`):
  `commit=false` (default) writes nothing and returns the report, the plan per print+finish and every unmapped row.
  `fill` raises each print+finish up to the file's count and never lowers (re-importing the same file adds nothing);
  `add` adds the file's counts. Count 0 rows are skipped, nothing is ever deleted. Unmapped rows are returned, never guessed.
* `GET /api/lorcana/collection/export-dreamborn`: the user's Lorcana collection as a dreamborn CSV (languages folded).
* Foil is stored as the existing variant `Holo` (foil) / `Normal`; the `foil` flag is the API's word for it.
