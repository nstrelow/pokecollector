# Game axis (E3, PicaLens #62)

Short version. The map, the reasoning, deploy steps and rollback live in PicaLens `docs/plans/E3-HUB-GAME-AXIS.md`.

* `cards`, `sets`, `collection`, `scan_jobs` have `game VARCHAR NOT NULL DEFAULT 'pokemon'` (migration v63 in
  `database.py`, additive, no row is rewritten). New table `game_prices` (append-only, non-Pokemon prices).
* Every ORM SELECT of those four models is scoped to one game (`models.py`, bottom). Default `pokemon`, so every
  existing endpoint, job and test sees what it saw before. A session opts in with `services.games.scope_session`.
* `/api/lorcana/*` (`api/lorcana.py`) exists only with `LORCANA_ENABLED=1`: collection with a foil flag, dreamborn CSV
  import/export (`docs/DREAMBORN-CSV.md`), current price per print. Lorcana prices are imported from lorcascan's
  `lorcast_<date>.json` files: `python -m scripts.import_lorcast_prices` (`LORCANA_PRICES_DIR`).
* Checks: `backend/tests/test_game_axis.py` (unit), `backend/scripts/api_snapshot.py` (Pokemon API byte-diff between two
  trees, optionally with Lorcana data present), `backend/scripts/game_axis_migration_check.py` (migration, idempotence,
  rollback on a throw-away PostgreSQL copy).
