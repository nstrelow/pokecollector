"""Rehearse the game-axis migration (E3, PicaLens #62) on a PostgreSQL copy. Never touches a live database.

    python scripts/game_axis_migration_check.py --old-tree BASELINE/backend --new-tree GAME-AXIS/backend \
        --pg "postgresql://USER@127.0.0.1:PORT" --csv canonical.csv [--keep]          # seeded stand-in
    python scripts/game_axis_migration_check.py --old-tree DEPLOYED/backend --new-tree GAME-AXIS/backend \
        --pg "postgresql://USER@127.0.0.1:PORT" --from-dump pokemon_tcg.dump [--pg-restore PATH] [--keep]   # live copy

`--from-dump` is the live-copy rehearsal (the mandatory pre-deploy step of PicaLens docs/plans/E3-HUB-GAME-AXIS.md): the
owner makes `docker exec pokemon-postgres pg_dump -U pokemon -Fc pokemon_tcg > X.dump` on CT 100, and this script, run on
the host against a scratch PostgreSQL, restores it into gx_old (instead of seeding), then runs the same steps 2-7. Use
a scratch server of the SAME major version as production (PG 18): pg_restore refuses an archive newer than itself.
--old-tree must be the code that is deployed now (it makes the reference for the baseline's own startup fix-ups and is
the "old code" of the rollback check). Nothing is written to the dump file; the scratch databases are dropped at the end.

What it does, in two scratch databases on a server YOU started (use a throw-away cluster; it creates and drops
databases named gx_old / gx_new):

 1. gx_old: either the baseline tree's own init_db() (the production schema path) seeded from a collection CSV
    (pokemon-collection/canonical.csv, the pokemirror snapshot of the real collection) plus a few of every other kind,
    or, with --from-dump, `pg_restore --no-owner --no-privileges --exit-on-error` of the live dump.
 2. fingerprint every table: row count + sha256 over the rows (all columns, ordered by primary key).
 3. gx_new = CREATE DATABASE ... TEMPLATE gx_old (stands in for the dump/restore copy of the live database).
 4. run the NEW tree's init_db() on gx_new (the same code path that runs on deploy).
 5. check: row counts per old table equal; the old columns of every row are byte-identical; `game` exists on cards, sets,
    collection, scan_jobs with 0 NULLs and every row 'pokemon'; game_prices is empty; relfilenode of cards and collection
    unchanged (no table rewrite); a second init_db() changes nothing.
 6. Lorcana on the migrated copy: enable the flag, add rows through the API functions, check the Pokemon fingerprint of the
    old columns is still identical.
 7. rollback: (a) old code refuses to start while Lorcana 'Foil' rows exist (its startup audit); (b) the rollback SQL of the
    plan doc, one transaction: Lorcana rows out (collection before cards), the collection variant check back to the four
    Pokemon values (it must FAIL if a Foil row is left); (c) old code then runs on the copy, and the optional schema cleanup
    (drop the four columns and game_prices) gives back the exact fingerprint of step 2, check definitions included.
 The v63 step also widens the `collection` variant check to take 'Foil' (idempotent; other tables keep four values).
Exit 0 only if every check passes. Prints one line per check.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap

import psycopg2

ap = argparse.ArgumentParser()
ap.add_argument("--old-tree", required=True)
ap.add_argument("--new-tree", required=True)
ap.add_argument("--pg", required=True, help="server URL without database, e.g. postgresql://pokemon@127.0.0.1:55432")
ap.add_argument("--csv", help="seed gx_old from this collection CSV (the stand-in mode)")
ap.add_argument("--from-dump", metavar="FILE", help="restore this pg_dump -Fc file into gx_old instead of seeding it")
ap.add_argument("--pg-restore", default="pg_restore", help="pg_restore binary (version >= the dump's and the server's)")
ap.add_argument("--keep", action="store_true", help="do not drop gx_old / gx_new afterwards")
args = ap.parse_args()
if bool(args.csv) == bool(args.from_dump):
    ap.error("give exactly one of --csv (seed a stand-in) or --from-dump (restore a pg_dump -Fc)")

BASE = args.pg.rstrip("/")
GAME_TABLES = ("cards", "sets", "collection", "scan_jobs")
fails = []


def check(ok, what, detail=""):
    print(("PASS  " if ok else "FAIL  ") + what + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(what)


def conn(db):
    c = psycopg2.connect(f"{BASE}/{db}")
    c.autocommit = True
    return c


def run_tree(tree, db, code, extra_env=None):
    env = {**os.environ, "DATABASE_URL": f"{BASE}/{db}", "JWT_SECRET_KEY": "migration-check-not-a-secret",
           "PRE_UPGRADE_BACKUP_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
    r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {os.path.abspath(tree)!r}); {code}"],
                       env=env, cwd=tree, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout[-2000:], r.stderr[-3000:])
        raise SystemExit(f"subprocess failed in {tree}")
    return r.stdout


def jdefault(o):
    """JSON fallback for the fingerprints. bytea comes back as a memoryview whose str() holds a memory address, which made
    two identical rows hash differently (found on the first real dump: cards.image_phash / image_embedding, image_cache.data)."""
    if isinstance(o, (bytes, bytearray, memoryview)):
        return "bytes:" + hashlib.sha256(bytes(o)).hexdigest()
    return str(o)


def run_tree_rc(tree, db, code):
    """Like run_tree, but returns (returncode, stdout, stderr) instead of stopping on a failure."""
    env = {**os.environ, "DATABASE_URL": f"{BASE}/{db}", "JWT_SECRET_KEY": "migration-check-not-a-secret",
           "PRE_UPGRADE_BACKUP_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {os.path.abspath(tree)!r}); {code}"],
                       env=env, cwd=tree, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def check_defs(c):
    """{constraint: (allowed values, nullable)} of the variant checks (part of the schema the rollback must restore).
    Compared by meaning, not by text: PostgreSQL prints the same check differently after a dump/restore or an ALTER."""
    cur = c.cursor()
    cur.execute("SELECT conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname IN ('ck_collection_variant','ck_product_cards_variant','ck_product_ledger_variant',"
                "'ck_trade_items_variant') ORDER BY 1")
    return {name: (tuple(sorted(re.findall(r"'([^']+)'", d))), "IS NULL" in d) for name, d in cur.fetchall()}


def tables(c):
    cur = c.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")
    return [r[0] for r in cur.fetchall()]


# user_settings.id: startup inserts the default settings in set order (PYTHONHASHSEED), so the ids differ between two
# runs of the SAME code; the rows (user, key, value) are what matters.
ALWAYS_SKIP = {"user_settings": {"id"}}


def fingerprint(c, skip_cols=None):
    """{table: (rows, sha256)}; rows ordered by all columns' text so it needs no key; skip_cols = {table: {col}}."""
    skip_cols = {t: set(v) for t, v in (skip_cols or {}).items()}
    for t, v in ALWAYS_SKIP.items():
        skip_cols.setdefault(t, set()).update(v)
    out, cur = {}, c.cursor()
    for t in tables(c):
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s "
                    "ORDER BY ordinal_position", (t,))
        cols = [r[0] for r in cur.fetchall() if r[0] not in skip_cols.get(t, ())]
        if not cols:
            continue
        sel = ", ".join(f'"{x}"' for x in cols)
        cur.execute(f'SELECT {sel} FROM "{t}"')
        rows = sorted(json.dumps(r, default=jdefault, sort_keys=True) for r in cur.fetchall())
        out[t] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest())
    return out


def relfile(c, names):
    cur = c.cursor()
    cur.execute("SELECT relname, relfilenode FROM pg_class WHERE relname = ANY(%s) AND relkind='r'", (list(names),))
    return dict(cur.fetchall())


SEED = textwrap.dedent('''
    import csv, datetime as dt
    from database import SessionLocal
    import models as M
    T0 = dt.datetime(2025, 10, 1, 12, 0, 0)
    db = SessionLocal()
    db.add_all([M.User(id=1, username="admin", hashed_password="x", role="admin", is_active=True, created_at=T0),
                M.User(id=2, username="misty", hashed_password="x", role="trainer", is_active=True, created_at=T0)])
    db.flush()
    rows = list(csv.DictReader(open(%(csv)r, encoding="utf-8")))
    seen_sets, seen_cards = set(), set()
    for r in rows:
        sid = f"{r['set_id']}_{r['lang']}"
        if sid not in seen_sets:
            seen_sets.add(sid)
            db.add(M.Set(id=sid, tcg_set_id=r["set_id"], name=r["set_name"], lang=r["lang"], total=0, updated_at=T0))
        cid = r["card_id"]
        custom = r["is_custom"] == "True"
        if cid not in seen_cards:
            seen_cards.add(cid)
            mp = float(r["market_price"]) if r["market_price"] else None
            db.add(M.Card(id=cid, tcg_card_id=None if custom else cid.rsplit("_", 1)[0], name=r["name"],
                          set_id=r["set_id"], number=r["number"], rarity=r["rarity"], lang=r["lang"],
                          images_small=r["image_url"], images_large=r["image_url"], price_market=mp, price_trend=mp,
                          is_custom=custom, custom_owner_id=1 if custom else None, updated_at=T0))
    db.flush()
    for i, r in enumerate(rows):
        db.add(M.CollectionItem(card_id=r["card_id"], user_id=1, quantity=int(r["quantity"]), condition=r["condition"],
                                variant=r["variant"], lang=r["lang"],
                                purchase_price=float(r["purchase_price"]) if r["purchase_price"] else None,
                                added_at=dt.datetime.fromisoformat(r["purchase_date"]) + dt.timedelta(seconds=i)))
    first = rows[0]["card_id"]
    db.add_all([M.WishlistItem(card_id=first, user_id=1, quantity=1, created_at=T0),
                M.PriceHistory(card_id=first, date=dt.date(2026, 10, 14), price_market=1.0, price_trend=1.1),
                M.PriceHistory(card_id=first, date=dt.date(2026, 10, 15), price_market=1.2, price_trend=1.3)])
    db.add(M.Binder(id=1, name="Main", user_id=1, color="#ee1515", binder_type="collection", created_at=T0))
    db.flush()
    db.add(M.BinderCard(binder_id=1, card_id=first, added_at=T0))
    job = M.ScanJob(user_id=1, status="done", created_at=T0, updated_at=T0, expires_at=T0 + dt.timedelta(days=1),
                    session_lang="en")
    db.add(job)
    db.flush()
    db.add(M.ScanJobItem(job_id=job.id, user_id=1, position=0, byte_size=10, status="done", resolved=True,
                         created_at=T0, updated_at=T0, recognized={"name": "x"}))
    db.commit()
    print("seeded", len(rows), "collection rows")
''')

LORCANA = textwrap.dedent('''
    from database import SessionLocal
    from services.games import game_session
    from api.lorcana import _add
    with game_session(SessionLocal, "lorcana") as db:
        _add(db, %(u1)d, "en:1-1", 4, None, "NM", None)
        _add(db, %(u1)d, "en:1-207", 1, None, "NM", None)
        _add(db, %(u2)d, "de:1-1", 2, True, "LP", 1.5)
        db.commit()
    print("lorcana rows added")
''')

LORCANA_PRICES = textwrap.dedent('''
    import json, pathlib, tempfile
    from database import SessionLocal
    from services.games import game_session
    from services.lorcana_prices import import_price_file, price_for
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / "lorcast_2026-10-16.json"
    p.write_text(json.dumps({"format": "lorcascan-lorcast-prices", "version": 1, "meta": {"date": "2026-10-16"},
                             "prices": {"en:1-1": {"usd": 0.13, "usd_foil": 0.75},
                                        "en:1-207": {"usd": None, "usd_foil": 952.84}}}))
    with game_session(SessionLocal, "lorcana") as db:
        print(import_price_file(db, p), import_price_file(db, p)["added"], price_for(db, "de:1-1", True).amount)
''')

OLD_CODE_USES = textwrap.dedent('''
    import datetime as dt
    from database import SessionLocal, init_db
    import models as M
    init_db()
    db = SessionLocal()
    n = db.query(M.CollectionItem).count()
    db.add(M.CollectionItem(card_id=db.query(M.Card).first().id, user_id=db.query(M.User).order_by(M.User.id).first().id, quantity=1, condition="LP", variant="Normal",
                            lang="en", added_at=dt.datetime(2026, 10, 16)))
    db.commit()
    print("old code: collection rows before/after", n, db.query(M.CollectionItem).count())
''')


def drop(name):
    c = conn("postgres")
    c.cursor().execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    c.close()


for name in ("gx_old", "gx_ref", "gx_new"):
    drop(name)
c = conn("postgres")
c.cursor().execute("CREATE DATABASE gx_old")
c.close()

# 1. the "live" database: baseline schema through the baseline code, seeded, or a restored dump
if args.from_dump:
    dump = os.path.abspath(args.from_dump)
    if not os.path.isfile(dump):
        raise SystemExit(f"no such dump file: {dump}")
    srv = conn("postgres")
    print("scratch server version", srv.server_version, "restoring", dump)
    srv.close()
    r = subprocess.run([args.pg_restore, "--no-owner", "--no-privileges", "--exit-on-error", "-d", f"{BASE}/gx_old", dump],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"pg_restore failed (is the scratch server / pg_restore as new as the dump's PostgreSQL?):\n"
                         f"{r.stderr[-2000:]}")
else:
    run_tree(args.old_tree, "gx_old", "import database; database.init_db()")
    print(run_tree(args.old_tree, "gx_old", SEED % {"csv": os.path.abspath(args.csv)}).strip())
old = conn("gx_old")
fp_seed = fingerprint(old)
print("restored/seeded rows:", ", ".join(f"{t}={n}" for t, (n, _h) in sorted(fp_seed.items()) if n))
check("game_prices" not in fp_seed, "the starting database has no game_prices table yet")
cur = old.cursor()
cur.execute("SELECT count(*) FROM information_schema.columns WHERE column_name='game' AND table_schema='public'")
check(cur.fetchone()[0] == 0, "the starting database has no game column anywhere")
cur.execute("SELECT id FROM users ORDER BY id LIMIT 2")
uids = [r[0] for r in cur.fetchall()]
check(bool(uids), "the starting database has at least one user", str(uids))
U1, U2 = uids[0], uids[-1]
defs_start = check_defs(old)
check(defs_start.get("collection.ck_collection_variant", ((), 0))[0] == ("First Edition", "Holo", "Normal", "Reverse Holo"),
      "the starting database's collection variant check has the four Pokemon values (no Foil yet)",
      str(defs_start.get("collection.ck_collection_variant", "no such constraint")))
rf_before = relfile(old, GAME_TABLES)
old.close()

# 3. the copies: gx_ref = what the BASELINE code makes of the seeded data on its next start (startup has its own data
#    fix-ups, e.g. linking binder rows, creating default user settings); gx_new = the copy the NEW code migrates.
c = conn("postgres")
c.cursor().execute("CREATE DATABASE gx_ref TEMPLATE gx_old")
c.cursor().execute("CREATE DATABASE gx_new TEMPLATE gx_old")
c.close()
run_tree(args.old_tree, "gx_ref", "import database; database.init_db()")
ref = conn("gx_ref")
fp_old = fingerprint(ref)
ref.close()
if not args.keep:
    drop("gx_ref")          # a real dump is big: do not hold three copies of it longer than needed

# 4. migrate the copy with the new code
run_tree(args.new_tree, "gx_new", "import database; database.init_db()")
new = conn("gx_new")
cur = new.cursor()
fp_new_all = fingerprint(new)
fp_new_old_cols = fingerprint(new, {t: {"game"} for t in GAME_TABLES})

# 5. checks
check(set(fp_old) <= set(fp_new_all), "every baseline table is still there", ", ".join(sorted(set(fp_new_all) - set(fp_old))) + " added")
changed = [t for t in fp_seed if t in fp_old and fp_old[t] != fp_seed[t]]
print("INFO  tables the baseline's own startup fix-ups change (the reference, not the raw start):", changed or "none")
check(all(fp_new_all[t][0] == fp_old[t][0] for t in fp_old), "row count of every baseline table unchanged",
      ", ".join(f"{t}={fp_old[t][0]}" for t in ("cards", "sets", "collection", "scan_jobs", "price_history")))
bad = [t for t in fp_old if fp_new_old_cols[t] != fp_old[t]]
check(not bad, "old columns of every row byte-identical (sha256 per table)", ", ".join(bad))
for t in GAME_TABLES:
    cur.execute(f"SELECT count(*), count(*) FILTER (WHERE game IS NULL), count(*) FILTER (WHERE game='pokemon') FROM {t}")
    n, nulls, pk = cur.fetchone()
    check(n == fp_old[t][0] and nulls == 0 and pk == n, f"{t}.game: all {n} rows 'pokemon', no NULL")
    cur.execute("SELECT is_nullable, column_default FROM information_schema.columns WHERE table_name=%s AND column_name='game'", (t,))
    check(cur.fetchone() == ("NO", "'pokemon'::character varying"), f"{t}.game is NOT NULL DEFAULT 'pokemon'")
cur.execute("SELECT count(*) FROM game_prices")
check(cur.fetchone()[0] == 0, "game_prices exists and is empty")
cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='game_prices' ORDER BY 1")
check([r[0] for r in cur.fetchall()] == ["game_prices_pkey", "ix_game_prices_day", "ix_game_prices_lookup", "uq_game_prices_obs"],
      "game_prices: primary key, unique observation key, lookup index, per-day index")
rf_after = relfile(new, GAME_TABLES)
check(rf_before == rf_after, "cards/collection/sets/scan_jobs not rewritten (relfilenode unchanged)", str(rf_after))
run_tree(args.new_tree, "gx_new", "import database; database.init_db()")
check(fingerprint(new) == fp_new_all, "second init_db() changes nothing (idempotent)")

defs_new = check_defs(new)
check("Foil" in defs_new.get("collection.ck_collection_variant", ((), 0))[0], "collection variant check now takes Foil (v63 step)",
      str(defs_new.get("collection.ck_collection_variant", "")))
check({k: v for k, v in defs_new.items() if k != "collection.ck_collection_variant"} ==
      {k: v for k, v in defs_start.items() if k != "collection.ck_collection_variant"},
      "product_cards / ledger / trade_items variant checks unchanged (Foil is a `collection` value only)")

# 6. Lorcana on the copy
print(run_tree(args.new_tree, "gx_new", LORCANA % {"u1": U1, "u2": U2}, {"LORCANA_ENABLED": "1"}).strip())
print(run_tree(args.new_tree, "gx_new", LORCANA_PRICES).strip())
cur.execute("SELECT game, count(*) FROM cards GROUP BY 1 ORDER BY 1")
check(cur.fetchall() == [("lorcana", 3), ("pokemon", fp_old["cards"][0])], "cards: 3 Lorcana + the Pokemon rows")
cur.execute("SELECT game, variant, count(*), sum(quantity) FROM collection GROUP BY 1,2 ORDER BY 1,2")
rows = cur.fetchall()
check([r for r in rows if r[0] == "lorcana"] == [("lorcana", "Foil", 2, 3), ("lorcana", "Normal", 1, 4)],
      "collection: Lorcana foil/normal rows stored as Foil/Normal", str([r for r in rows if r[0] == "lorcana"]))
poke_where = {"cards": "game='pokemon'", "collection": "game='pokemon'"}
snap_poke = {}
for t, w in poke_where.items():
    cur.execute(f"SELECT * FROM {t} WHERE {w}")
    snap_poke[t] = len(cur.fetchall())
check(snap_poke == {"cards": fp_old["cards"][0], "collection": fp_old["collection"][0]}, "Pokemon rows unchanged in number after Lorcana adds")
# Pokemon rows byte-identical with Lorcana data present: compare only game='pokemon' rows of old columns
def pokemon_only_fp(c):
    cu, out = c.cursor(), {}
    for t in ("cards", "collection", "sets", "scan_jobs", "price_history", "wishlist", "binders", "binder_cards"):
        cu.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name<>'game' ORDER BY ordinal_position", (t,))
        cols = ", ".join(f'"{r[0]}"' for r in cu.fetchall())
        flt = " WHERE game='pokemon'" if t in GAME_TABLES else ""
        cu.execute(f'SELECT {cols} FROM "{t}"{flt}')
        rs = sorted(json.dumps(r, default=jdefault, sort_keys=True) for r in cu.fetchall())
        out[t] = (len(rs), hashlib.sha256("\n".join(rs).encode()).hexdigest())
    return out
fp_pk_lorcana = pokemon_only_fp(new)
check(all(fp_pk_lorcana[t] == fp_old[t] for t in fp_pk_lorcana), "Pokemon rows (old columns) still byte-identical with Lorcana rows present")
cur.execute("SELECT count(*) FROM game_prices")
check(cur.fetchone()[0] == 4, "game_prices: 2 prints x 2 finishes imported once (second import added 0)")

# 7a. old code on the migrated copy WITH Lorcana rows present: its startup audit stops on the Foil variant (and, had it
#     started, it does not scope by game). So the Lorcana rows must be exported and removed first.
rc, _out, err = run_tree_rc(args.old_tree, "gx_new", OLD_CODE_USES)
check(rc != 0 and "Unsupported stored collection variants" in err,
      "(info) old code refuses to start while Lorcana 'Foil' rows exist: export them (dreamborn CSV) and delete them first")

# 7b. the rollback SQL of docs/plans/E3-HUB-GAME-AXIS.md, exactly: collection FIRST (collection.card_id is ON DELETE SET
#     NULL, deleting cards first would leave orphan rows with a NULL card_id that no filter on the id could still find),
#     then the check goes back to four values. It is one transaction: a Foil row that is still there makes the ADD
#     CONSTRAINT fail and nothing is changed.
cur.execute("SELECT count(*) FROM collection WHERE game='lorcana'")
n_l = cur.fetchone()[0]
NARROW = ("ALTER TABLE collection DROP CONSTRAINT ck_collection_variant",
          "ALTER TABLE collection ADD CONSTRAINT ck_collection_variant "
          "CHECK (variant IN ('Normal', 'Holo', 'Reverse Holo', 'First Edition'))")
cur.execute("BEGIN")
failed = False
try:
    for st in NARROW:
        cur.execute(st)
except psycopg2.Error:
    failed = True
cur.execute("ROLLBACK")
check(failed and "Foil" in check_defs(new)["collection.ck_collection_variant"][0],
      "narrowing the check while Foil rows exist FAILS and changes nothing (no silent loss)")
cur.execute("BEGIN")
for t in ("collection", "scan_jobs", "cards", "sets"):
    cur.execute(f"DELETE FROM {t} WHERE game <> 'pokemon'")
for st in NARROW:
    cur.execute(st)
cur.execute("COMMIT")
cur.execute("SELECT count(*) FROM collection WHERE card_id IS NULL AND game <> 'pokemon'")
check(cur.fetchone()[0] == 0, "rollback SQL leaves no orphan Lorcana collection row")
cur.execute("SELECT count(*) FROM collection WHERE variant='Foil'")
check(cur.fetchone()[0] == 0, "no Foil row is left after the rollback SQL")
check(check_defs(new) == defs_start, "variant checks are back to the starting definitions")

# 7c. old code on the rolled-back (columns still there) copy works; then the optional schema cleanup
print(run_tree(args.old_tree, "gx_new", OLD_CODE_USES).strip())
cur.execute("SELECT game, count(*) FROM collection GROUP BY 1 ORDER BY 1")
check(dict(cur.fetchall()) == {"pokemon": fp_old["collection"][0] + 1},
      "old code runs on the rolled-back DB; its new row gets game='pokemon' from the column default")
cur.execute("DELETE FROM collection WHERE id = (SELECT max(id) FROM collection)")     # the row the old-code test added
for t in GAME_TABLES:
    cur.execute(f"ALTER TABLE {t} DROP COLUMN game")
cur.execute("DROP TABLE game_prices")
fp_back = fingerprint(new)
check(fp_back == fp_old, "after rollback every table fingerprint equals the pre-migration one", f"{n_l} Lorcana collection rows removed first")
check(check_defs(new) == defs_start, "after rollback the variant check definitions equal the starting ones")
new.close()

if not args.keep:
    for name in ("gx_old", "gx_ref", "gx_new"):
        drop(name)
print("\nALL PASS" if not fails else f"\nFAILED: {fails}")
raise SystemExit(1 if fails else 0)
