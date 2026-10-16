# Game axis (E3, PicaLens #62)

Short version. The map, the reasoning, deploy steps and rollback live in PicaLens `docs/plans/E3-HUB-GAME-AXIS.md`.

* `cards`, `sets`, `collection`, `scan_jobs` have `game VARCHAR NOT NULL DEFAULT 'pokemon'` (migration v63 in
  `database.py`, additive, no row is rewritten). New table `game_prices` (append-only, non-Pokemon prices).
* Every ORM SELECT of those four models is scoped to one game (`models.py`, bottom). Default `pokemon`, so every
  existing endpoint, job and test sees what it saw before. A session opts in with `services.games.scope_session`.
* `/api/lorcana/*` (`api/lorcana.py`) exists only with `LORCANA_ENABLED=1`: collection with a foil flag (stored as the variant `Foil`; Pokemon uses `PokemonVariant`, four values, and never accepts it), dreamborn CSV
  import/export (`docs/DREAMBORN-CSV.md`), current price per print. Lorcana prices are imported from lorcascan's
  `lorcast_<date>.json` files: `python -m scripts.import_lorcast_prices` (`LORCANA_PRICES_DIR`).
* Checks: `backend/tests/test_game_axis.py` (unit), `backend/scripts/api_snapshot.py` (Pokemon API byte-diff between two
  trees, optionally with Lorcana data present), `backend/scripts/game_axis_migration_check.py` (migration, idempotence,
  rollback on a throw-away PostgreSQL copy).

## Live-copy rehearsal (mandatory before the deploy)

`python backend/scripts/game_axis_migration_check.py --old-tree <deployed>/backend --new-tree <this>/backend --pg postgresql://pokemon@127.0.0.1:PORT --from-dump FILE.dump --pg-restore <pg_restore of the production major version>`

The owner makes the dump on CT 100 (`docker exec pokemon-postgres pg_dump -U pokemon -Fc pokemon_tcg > FILE.dump`); the
script restores it into a scratch PostgreSQL (use PG 18 like production), fingerprints it, runs the new `init_db`,
re-checks counts, `game='pokemon'` everywhere, old columns byte-identical, runs the rollback SQL and compares the final
fingerprint with the start. Deploy only on `ALL PASS`. Without `--from-dump`, `--csv canonical.csv` seeds a stand-in.

## Rollback (exact order)

1. While the new code is still up, export each user's Lorcana collection: `GET /api/lorcana/collection/export-dreamborn`.
2. Stop the backend, then delete the Lorcana rows, `collection` BEFORE `cards` (`collection.card_id` is
   `ON DELETE SET NULL`; deleting cards first leaves orphan rows with a NULL `card_id`):
   ```
   BEGIN;
   DELETE FROM collection WHERE game <> 'pokemon';
   DELETE FROM scan_jobs  WHERE game <> 'pokemon';
   DELETE FROM cards      WHERE game <> 'pokemon';
   DELETE FROM sets       WHERE game <> 'pokemon';
   ALTER TABLE collection DROP CONSTRAINT ck_collection_variant;
   ALTER TABLE collection ADD CONSTRAINT ck_collection_variant
       CHECK (variant IN ('Normal', 'Holo', 'Reverse Holo', 'First Edition'));
   COMMIT;
   ```
   (The `ADD CONSTRAINT` fails while a `Foil` row exists and the whole transaction rolls back.)
3. Start the previous image. (Optional: `ALTER TABLE <cards|sets|collection|scan_jobs> DROP COLUMN game; DROP TABLE game_prices;`.)
4. Only if data was damaged, with the backend stopped:
   `pg_restore -U pokemon -d pokemon_tcg --clean --if-exists --no-owner --no-privileges --exit-on-error < pre-game-axis.dump`,
   then `DROP TABLE IF EXISTS game_prices` (the old dump does not contain it, so `--clean` leaves it).

A plain-SQL backup made before this deploy and restored through `/api/backup/restore` brings back tables without the
`game` columns; every game-scoped query then fails until the backend restarts (v63 re-adds the columns at start).

## Import and price rules (review round 1)

* Dreamborn import: bad numbers (including `²` and other non-ASCII digits) are reported as unmapped rows, a `csv.Error` is
  422, any count above 999 (per print and finish, or to add) is 422 for the whole file, an upload whose Content-Length is
  above 1 MB + 64 KB is 413 before the body is read. A foil-only card listed as normal stays normal; the plan line has
  `foil_only_card: true` and the result `foil_only_as_normal`.
* Prices: the normal-to-foil fallback only applies to foil-only prints (enchanted, iconic, epic); `inf`/`nan` prices
  are refused; a NULL `card_id` on a Lorcana row is listed without a print id and skipped in the export.

## Owner decisions 2026-10-16

* Foil is the variant `Foil`: in `CollectionVariant` and in the `collection` variant check only (widened idempotently at
  startup by `harden_collection_variant_constraints`, part of v63). Rolling back narrows the check again, which fails
  while a Foil row exists (see the rollback SQL; it now ends with the `DROP`/`ADD CONSTRAINT ck_collection_variant` pair).
* de/fr/it prints show the English print's USD price: every price block has `currency: "USD"`, `market: "US"`,
  `inherited` and `print_id` (the English print). No EUR conversion.
* `game_prices` keeps daily full rows (about 2.5 million rows, 755 MB with indexes, a year; measured), idempotent per day,
  indexes `ix_game_prices_lookup` and `ix_game_prices_day`.
* Price files come in through a read-only folder: compose volume `${LORCANA_PRICES_HOST_DIR:-./data/lorcana-prices}`;
  on CT 100 `LORCANA_PRICES_HOST_DIR=/mnt/lorcana-prices` (Proxmox bind mount of lorcascan's `data/prices`).
