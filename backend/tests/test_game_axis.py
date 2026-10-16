"""Game axis (E3, PicaLens #62): the default Pokemon scope, the Lorcana routes, Lorcana prices, the migration list.

SQLite in memory, like the rest of the suite. The PostgreSQL migration statements themselves are exercised against a
real PostgreSQL by docs/GAME-AXIS.md's recipe (a SQL test needs a server), not here.
"""
import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import aliased, joinedload, sessionmaker
    from sqlalchemy.pool import StaticPool

    import api.lorcana as lorcana_api
    from api.auth import get_current_user
    from database import Base, _run_migrations
    from models import Card, CollectionItem, GamePrice, ScanJob, Set, User
    from services import lorcana_dreamborn as dream
    from services.games import LORCANA, POKEMON, game_session, lorcana_enabled, scope_session, validate_game
    from services.lorcana_catalog import UnknownPrint, card_id_for, ensure_lorcana_card, print_id_of
    from services.lorcana_prices import (
        PriceFileError, import_price_file, latest_file, price_for, prices_status, read_price_file,
    )

    DEPS = True
except ModuleNotFoundError:
    DEPS = False

BACKEND = Path(__file__).resolve().parent.parent
FIXTURE = BACKEND / "tests" / "fixtures" / "dreamborn" / "sample_export.csv"


def price_doc(day, rows, fetched="2026-10-16T06:51:00Z"):
    return {"format": "lorcascan-lorcast-prices", "version": 1,
            "meta": {"date": day, "fetched_at": fetched, "source": "lorcast"}, "prices": rows}


def write_price_file(directory, day, rows):
    path = Path(directory) / f"lorcast_{day}.json"
    path.write_text(json.dumps(price_doc(day, rows)), encoding="utf-8")
    return path


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class GameAxisBase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()                      # default scope: Pokemon
        self.user = User(username="ash", hashed_password="x", is_active=True)
        self.db.add(self.user)
        self.db.commit()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def lorcana(self):
        return scope_session(self.Session(), LORCANA)

    def add_pokemon(self):
        self.db.add_all([
            Set(id="sv1_en", tcg_set_id="sv1", name="Scarlet & Violet", lang="en"),
            Card(id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito", set_id="sv1", number="1", lang="en"),
            Card(id="sv1-2_en", tcg_card_id="sv1-2", name="Floragato", set_id="sv1", number="2", lang="en"),
        ])
        self.db.flush()
        self.db.add_all([
            CollectionItem(card_id="sv1-1_en", user_id=self.user.id, quantity=2, lang="en"),
            CollectionItem(card_id="sv1-2_en", user_id=self.user.id, quantity=1, lang="en", variant="Holo"),
        ])
        self.db.commit()


class DefaultScopeTests(GameAxisBase):
    def test_new_rows_default_to_pokemon_in_python_and_in_the_database(self):
        self.add_pokemon()
        self.assertEqual({c.game for c in self.db.query(Card)}, {"pokemon"})
        with self.engine.begin() as conn:      # an INSERT that does not mention game (an old code path) gets the default
            conn.execute(text("INSERT INTO cards (id, name, is_shared_template) VALUES ('raw_en', 'Raw', 0)"))
            conn.execute(text("INSERT INTO scan_jobs (user_id, status, created_at, updated_at, expires_at) VALUES (1, 'pending', '2030-01-01', '2030-01-01', '2030-01-01')"))
            self.assertEqual(conn.execute(text("SELECT game FROM cards WHERE id='raw_en'")).scalar(), "pokemon")
            self.assertEqual(conn.execute(text("SELECT game FROM scan_jobs")).scalar(), "pokemon")

    def test_default_session_never_sees_lorcana_rows(self):
        self.add_pokemon()
        lc = self.lorcana()
        ensure_lorcana_card(lc, "en:1-1")
        lc.add(CollectionItem(card_id="lorcana:1-1_en", user_id=self.user.id, quantity=5, lang="en", game=LORCANA))
        lc.commit()
        self.assertEqual(self.db.query(Card).count(), 2)
        self.assertEqual(self.db.query(CollectionItem).count(), 2)
        self.assertEqual(self.db.query(Card).filter(Card.id == "lorcana:1-1_en").first(), None)
        rows = self.db.query(CollectionItem).join(Card, Card.id == CollectionItem.card_id).all()
        self.assertEqual(sorted(r.card_id for r in rows), ["sv1-1_en", "sv1-2_en"])
        ali = aliased(Card)
        self.assertEqual(self.db.query(CollectionItem).join(ali, ali.id == CollectionItem.card_id).count(), 2)
        lc.close()

    def test_lorcana_session_sees_only_lorcana_and_star_sees_both(self):
        self.add_pokemon()
        lc = self.lorcana()
        ensure_lorcana_card(lc, "en:1-1")
        lc.commit()
        self.assertEqual([c.id for c in lc.query(Card)], ["lorcana:1-1_en"])
        self.assertEqual(self.db.query(Card).execution_options(game="*").count(), 3)
        self.assertEqual(lc.query(Card).execution_options(game=POKEMON).count(), 2)
        self.db.info["game"] = "*"                                      # maintenance only; scope_session refuses it
        self.assertEqual(self.db.query(Card).count(), 3)
        lc.close()

    def test_lazy_and_joined_loads_stay_in_the_game(self):
        self.add_pokemon()
        item = self.db.query(CollectionItem).options(joinedload(CollectionItem.card)).first()
        self.assertTrue(item.card.id.startswith("sv1-"))
        self.db.expire_all()
        item = self.db.query(CollectionItem).first()
        self.assertTrue(item.card.id.startswith("sv1-"))                  # lazy load

    def test_pokemon_collection_endpoint_ignores_lorcana_items(self):
        from api.collection import get_collection
        self.add_pokemon()
        before = [(i.id, i.card_id, i.quantity, i.variant) for i in get_collection(self.user, self.db)]
        lc = self.lorcana()
        lorcana_api._add(lc, self.user.id, "en:1-207", 3, None, "NM", None)
        lc.commit()
        lc.close()
        after = [(i.id, i.card_id, i.quantity, i.variant) for i in get_collection(self.user, self.db)]
        self.assertEqual(before, after)
        self.assertEqual(len(after), 2)

    def test_validate_game(self):
        self.assertEqual(validate_game(None), "pokemon")
        self.assertEqual(validate_game(" Lorcana "), "lorcana")
        with self.assertRaises(ValueError):
            validate_game("magic")
        with game_session(self.Session, LORCANA) as db:
            self.assertEqual(db.info["game"], "lorcana")


class CatalogTests(GameAxisBase):
    def test_card_ids_and_unknown_prints(self):
        self.assertEqual(card_id_for("en:1-1")[:2], ("lorcana:1-1_en", "en"))
        self.assertEqual(card_id_for("de:1-1")[0], "lorcana:1-1_de")
        self.assertEqual(print_id_of("lorcana:1-1_de"), "de:1-1")
        self.assertEqual(print_id_of("lorcana:1-P1-1_en"), "en:1-P1-1")
        self.assertIsNone(print_id_of(None))                        # NULL card_id (card row deleted): no crash
        for bad in ("en:9-999", "it:1-1", "xx:1-1", "1-1"):          # it: has no print of 1-1 in the map
            with self.assertRaises(UnknownPrint):
                card_id_for(bad)
        with self.assertRaises(ValueError):
            print_id_of("sv1-1_en")

    def test_ensure_card_creates_once_and_keeps_pokemon_apart(self):
        lc = self.lorcana()
        a = ensure_lorcana_card(lc, "en:1-207")
        lc.commit()
        b = ensure_lorcana_card(lc, "en:1-207")
        self.assertIs(a, b)
        self.assertEqual((a.game, a.name, a.set_id, a.number, a.rarity, a.lang), (
            "lorcana", "Elsa - Spirit of Winter", "1", "207", "Enchanted", "en"))
        self.assertEqual(a.types, ["Amethyst"])
        self.assertFalse(a.variants_normal)                              # Enchanted is foil only
        self.assertEqual(lc.query(Card).count(), 1)
        self.assertEqual(self.db.query(Card).count(), 0)
        # a default (Pokemon) session asked for a Lorcana print still must not insert a second row
        c = ensure_lorcana_card(self.db, "en:1-207")
        self.assertEqual(c.id, "lorcana:1-207_en")
        lc.close()


class LorcanaApiTests(GameAxisBase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"LORCANA_ENABLED": "1"})
        self.env.start()
        app = FastAPI()
        app.include_router(lorcana_api.router, prefix="/api/lorcana")
        self.lc = self.lorcana()

        def override_db():
            yield self.lc

        app.dependency_overrides[lorcana_api.get_lorcana_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.lc.close()
        self.env.stop()
        super().tearDown()

    def add(self, **body):
        body.setdefault("print_id", "en:1-1")
        return self.client.post("/api/lorcana/collection/", json=body)

    def test_off_by_default(self):
        with patch.dict(os.environ, {"LORCANA_ENABLED": ""}):
            self.assertFalse(lorcana_enabled())
            self.assertEqual(self.client.get("/api/lorcana/collection/").status_code, 404)
            self.assertEqual(self.add().status_code, 404)

    def test_main_mounts_the_router_only_when_enabled(self):
        code = ("import os, sys; sys.path.insert(0, %r); os.environ['DATABASE_URL']='postgresql://u:p@127.0.0.1:1/x'; "
                "import main; print(sorted({r.path for r in main.app.routes if r.path.startswith('/api/lorcana')}))")
        env = {k: v for k, v in os.environ.items() if k != "LORCANA_ENABLED"}
        off = subprocess.run([sys.executable, "-c", code % str(BACKEND)], env=env, capture_output=True, text=True,
                             cwd=self.tmp.name)
        on = subprocess.run([sys.executable, "-c", code % str(BACKEND)], env={**env, "LORCANA_ENABLED": "1"},
                            capture_output=True, text=True, cwd=self.tmp.name)
        self.assertEqual(off.returncode, 0, off.stderr[-500:])
        self.assertEqual(off.stdout.strip().splitlines()[-1], "[]")
        self.assertIn("/api/lorcana/collection/", on.stdout)

    def test_add_normal_and_foil_are_separate_rows_and_grouping_adds_up(self):
        r = self.add(quantity=2)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["action"], r.json()["item"]["foil"], r.json()["item"]["quantity"]),
                         ("added", False, 2))
        self.assertEqual(self.add(quantity=1).json()["action"], "updated")
        foil = self.add(foil=True).json()["item"]
        self.assertTrue(foil["foil"])
        items = self.client.get("/api/lorcana/collection/").json()
        self.assertEqual(sorted((i["foil"], i["quantity"]) for i in items), [(False, 3), (True, 1)])
        stored = {(i.variant, i.game, i.lang) for i in self.db.query(CollectionItem).execution_options(game="*")}
        self.assertEqual(stored, {("Normal", "lorcana", "en"), ("Foil", "lorcana", "en")})

    def test_foil_defaults_follow_the_card_and_an_explicit_choice_wins(self):
        enchanted = self.add(print_id="en:1-207").json()
        self.assertTrue(enchanted["item"]["foil"])                       # foil-only card, no choice given: foil
        self.assertEqual(enchanted["notes"], {})
        wrong = self.add(print_id="en:1-205", foil=False).json()
        self.assertFalse(wrong["item"]["foil"])                          # the user said so: kept, but flagged
        self.assertEqual(wrong["notes"], {"foil_only_card": True})
        self.assertFalse(self.add().json()["item"]["foil"])              # ordinary card, no choice: normal

    def test_bad_input_is_refused_not_guessed(self):
        for body in (dict(print_id="en:9-999"), dict(print_id="it:1-1"), dict(print_id="sv1-1_en"),
                     dict(quantity=0), dict(condition="Pristine"), dict(purchase_price=-1)):
            self.assertEqual(self.add(**body).status_code, 422, body)
        self.assertEqual(self.db.query(CollectionItem).execution_options(game="*").count(), 0)
        self.assertEqual(self.db.query(Card).execution_options(game="*").count(), 0)

    def test_update_merges_into_an_identical_row_and_delete_removes(self):
        a = self.add(quantity=2).json()["item"]
        b = self.add(foil=True, quantity=1).json()["item"]
        r = self.client.put(f"/api/lorcana/collection/{b['id']}", json={"foil": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["id"], r.json()["quantity"], r.json()["foil"]), (a["id"], 3, False))
        self.assertEqual(self.client.get("/api/lorcana/collection/").json()[0]["quantity"], 3)
        self.assertEqual(self.client.put(f"/api/lorcana/collection/{a['id']}", json={"quantity": 7, "condition": "LP"}
                                         ).json()["condition"], "LP")
        self.assertEqual(self.client.put("/api/lorcana/collection/999", json={"quantity": 1}).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/lorcana/collection/{a['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/lorcana/collection/").json(), [])

    def test_another_users_item_is_not_reachable(self):
        a = self.add().json()["item"]
        other = User(username="misty", hashed_password="x", is_active=True)
        self.db.add(other)
        self.db.commit()
        self.app.dependency_overrides[get_current_user] = lambda: other
        self.assertEqual(self.client.delete(f"/api/lorcana/collection/{a['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/lorcana/collection/").json(), [])

    def test_prices_in_the_list_and_the_price_route(self):
        write_price_file(self.tmp.name, "2026-10-16", {
            "en:1-1": {"usd": 0.13, "usd_foil": 0.75},
            "en:1-207": {"usd": None, "usd_foil": 952.84},
        })
        import_price_file(self.lc, Path(self.tmp.name) / "lorcast_2026-10-16.json")
        self.add(foil=True)
        self.add(print_id="en:1-207")
        by_pid = {i["print_id"]: i for i in self.client.get("/api/lorcana/collection/").json()}
        self.assertEqual(by_pid["en:1-1"]["price"]["amount"], 0.75)
        self.assertEqual(by_pid["en:1-207"]["price"]["amount"], 952.84)
        r = self.client.get("/api/lorcana/prices/de:1-1", params={"foil": "false"}).json()["price"]
        self.assertEqual((r["amount"], r["inherited"], r["print_id"]), (0.13, True, "en:1-1"))
        self.assertIsNone(self.client.get("/api/lorcana/prices/en:3-4a").json()["price"])
        self.assertEqual(self.client.get("/api/lorcana/prices/en:9-999").status_code, 422)
        self.assertEqual(self.client.get("/api/lorcana/status").json()["prices"]["newest"], "2026-10-16")

    def test_every_api_price_block_says_usd(self):
        write_price_file(self.tmp.name, "2026-10-16", {"en:1-1": {"usd": 0.13, "usd_foil": 0.75}})
        import_price_file(self.lc, Path(self.tmp.name) / "lorcast_2026-10-16.json")
        self.add(print_id="de:1-1")
        self.add(print_id="en:3-4a")                                      # no price on file at all
        items = {i["print_id"]: i for i in self.client.get("/api/lorcana/collection/").json()}
        self.assertEqual(items["de:1-1"]["price"]["currency"], "USD")
        self.assertEqual((items["de:1-1"]["price"]["inherited"], items["de:1-1"]["price"]["print_id"]), (True, "en:1-1"))
        self.assertIsNone(items["en:3-4a"]["price"])
        self.assertEqual({i["currency"] for i in items.values()}, {"USD"})   # also where there is no price yet
        one = self.client.get("/api/lorcana/prices/de:1-1").json()
        self.assertEqual((one["currency"], one["price"]["currency"]), ("USD", "USD"))
        self.assertEqual(self.client.get("/api/lorcana/prices/en:3-4a").json()["currency"], "USD")
        self.assertEqual(self.client.get("/api/lorcana/status").json()["prices"]["currency"], "USD")

    def post_csv(self, text, **params):
        return self.client.post("/api/lorcana/collection/import-dreamborn", params=params,
                                files={"file": ("export.csv", text.encode("utf-8"), "text/csv")})

    def test_dreamborn_preview_writes_nothing_and_commit_is_idempotent(self):
        text = FIXTURE.read_text(encoding="utf-8")
        preview = self.post_csv(text).json()
        self.assertFalse(preview["committed"])
        self.assertEqual(preview["report"]["rows"], 22)
        self.assertEqual(preview["report"]["unmapped"], 3)
        self.assertEqual(len(preview["report"]["unmapped_rows"]), 3)
        self.assertEqual(self.db.query(CollectionItem).execution_options(game="*").count(), 0)
        done = self.post_csv(text, commit="true").json()
        self.assertTrue(done["committed"])
        copies = sum(i["quantity"] for i in self.client.get("/api/lorcana/collection/").json())
        self.assertEqual(copies, done["would_add_copies"])
        again = self.post_csv(text, commit="true").json()                # same file again: nothing more is added
        self.assertEqual(again["would_add_copies"], 0)
        self.assertEqual(sum(i["quantity"] for i in self.client.get("/api/lorcana/collection/").json()), copies)

    def test_dreamborn_fill_never_lowers_and_add_mode_adds(self):
        self.add(quantity=5)                                              # en:1-1 normal: 5 owned, file says 3
        text = "Set Number,Card Number,Variant,Count\n001,001,normal,3\n001,001,foil,2\n"
        plan = {(p["print_id"], p["foil"]): p for p in self.post_csv(text, commit="true").json()["plan"]}
        self.assertEqual(plan[("en:1-1", False)]["add"], 0)
        self.assertIn("lower", plan[("en:1-1", False)]["note"])
        self.assertEqual(plan[("en:1-1", True)]["add"], 2)
        self.post_csv(text, commit="true", mode="add")
        got = {(i["foil"]): i["quantity"] for i in self.client.get("/api/lorcana/collection/").json()}
        self.assertEqual(got, {False: 8, True: 4})

    def test_dreamborn_garbage_and_zero_rows(self):
        self.assertEqual(self.post_csv("hello\nworld\n").status_code, 422)
        self.assertEqual(self.post_csv("").status_code, 422)
        self.assertEqual(self.post_csv("Set Number,Card Number,Variant,Count\n001,001,normal,0\n").json()["skipped_zero"], 1)
        self.assertEqual(self.post_csv("Set Number,Card Number,Variant,Count\n001,001,normal,1\n", lang="xx").status_code, 422)
        big = "Set Number,Card Number,Variant,Count\n" + "001,001,normal,1\n" * 80000
        self.assertEqual(self.post_csv(big).status_code, 413)
        self.assertEqual(self.db.query(CollectionItem).execution_options(game="*").count(), 0)

    def test_999_cap_also_holds_when_merging_into_an_existing_row(self):
        self.assertEqual(self.add(quantity=900).status_code, 200)
        over = self.add(quantity=100)                                    # 900 + 100 = 1000
        self.assertEqual(over.status_code, 422, over.text)
        self.assertIn("1000", over.json()["detail"])
        self.assertEqual(self.add(quantity=99).json()["item"]["quantity"], 999)           # exactly 999 is fine
        self.assertEqual(self.add(quantity=1).status_code, 422)
        self.assertEqual([i["quantity"] for i in self.client.get("/api/lorcana/collection/").json()], [999])
        # PUT that merges two rows into one above the cap
        other = self.add(foil=True, quantity=5).json()["item"]
        r = self.client.put(f"/api/lorcana/collection/{other['id']}", json={"foil": False})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(sorted((i["foil"], i["quantity"]) for i in self.client.get("/api/lorcana/collection/").json()),
                         [(False, 999), (True, 5)])

    def test_dreamborn_add_mode_cap_refuses_the_whole_file_and_writes_nothing(self):
        self.add(quantity=900)
        self.add(print_id="en:1-205", quantity=1)
        head = "Set Number,Card Number,Variant,Count\n"
        text = head + "001,001,normal,100\n001,205,normal,1\n"       # first line would make 1000, second is fine alone
        for params in ({"commit": "true", "mode": "add"}, {"mode": "add"}):          # the preview says 422 as well
            r = self.post_csv(text, **params)
            self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(sorted((i["print_id"], i["quantity"]) for i in self.client.get("/api/lorcana/collection/").json()),
                         [("en:1-1", 900), ("en:1-205", 1)])                # nothing of the file was applied
        ok = self.post_csv(head + "001,001,normal,99\n", commit="true", mode="add")
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual({i["print_id"]: i["quantity"] for i in self.client.get("/api/lorcana/collection/").json()}["en:1-1"], 999)

    def test_dreamborn_unicode_digits_and_csv_errors_are_422_not_500(self):
        head = "Set Number,Card Number,Variant,Count\n"
        for body in (head + "001,001,normal,\u00b2\n", head + "\u00b2,001,normal,1\n", head + "001,\u00b2,normal,1\n",
                     head + "001,\u0661,normal,1\n", head + "001,001,normal," + "9" * 5000 + "\n"):
            r = self.post_csv(body)
            self.assertEqual(r.status_code, 200, body)                    # a bad cell is a reported unmapped row ...
            self.assertEqual(r.json()["report"]["unmapped"], 1, body)
            self.assertEqual(r.json()["plan"], [], body)                  # ... and never written
        wide = "Name,Normal,Foil,Set,Card Number\nAriel,\u00b2,0,001,001\n"
        self.assertEqual(self.post_csv(wide).json()["report"]["unmapped"], 1)
        r = self.post_csv(head + "001,001,normal," + "1" * 200000 + "\n")      # one field above the csv module's limit
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(self.post_csv("a,b\n\"unterminated\n").status_code, 422)
        self.assertEqual(self.db.query(CollectionItem).execution_options(game="*").count(), 0)

    def test_dreamborn_count_above_999_is_422(self):
        head = "Set Number,Card Number,Variant,Count\n"
        self.assertEqual(self.post_csv(head + "001,001,normal,1000\n", commit="true").status_code, 422)
        self.assertEqual(self.post_csv(head + "001,001,normal,600\n001,001,normal,600\n").status_code, 422)   # summed
        ok = self.post_csv(head + "001,001,normal,999\n", commit="true")
        self.assertEqual((ok.status_code, ok.json()["would_add_copies"]), (200, 999))
        # 999 are stored now; adding one more in add mode would make 1000 in the merged row: 422, nothing changes
        self.assertEqual(self.post_csv(head + "001,001,normal,1\n", commit="true", mode="add").status_code, 422)
        self.assertEqual(self.post_csv(head + "001,001,normal,1\n", commit="true").status_code, 200)       # fill: nothing to add
        self.assertEqual(self.db.query(CollectionItem).execution_options(game="*").count(), 1)

    def test_dreamborn_oversize_is_refused_on_content_length_before_the_body_is_read(self):
        reads = []
        real = lorcana_api.UploadFile.read

        async def spy(self_, *a, **k):
            reads.append(1)
            return await real(self_, *a, **k)

        big = b"x" * (lorcana_api.IMPORT_MAX_BYTES + lorcana_api.IMPORT_ENVELOPE_BYTES + 10)
        with patch.object(lorcana_api.UploadFile, "read", spy):
            r = self.client.post("/api/lorcana/collection/import-dreamborn", files={"file": ("e.csv", big, "text/csv")})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(reads, [])                                       # the handler never ran
        near = b"Set Number,Card Number,Variant,Count\n" + b"001,001,normal,0\n" * 60000      # < 1 MB: still parsed
        self.assertLess(len(near), lorcana_api.IMPORT_MAX_BYTES)
        self.assertEqual(self.client.post("/api/lorcana/collection/import-dreamborn",
                                          files={"file": ("e.csv", near, "text/csv")}).status_code, 200)
        slightly = b"x" * (lorcana_api.IMPORT_MAX_BYTES + 1000)          # inside the framing allowance: the read limit
        self.assertEqual(self.client.post("/api/lorcana/collection/import-dreamborn",
                                          files={"file": ("e.csv", slightly, "text/csv")}).status_code, 413)

    def test_dreamborn_foil_only_card_as_normal_is_kept_normal_and_flagged(self):
        text = "Set Number,Card Number,Variant,Count\n001,207,normal,1\n001,001,normal,1\n"
        done = self.post_csv(text, commit="true").json()
        flags = {p["print_id"]: p.get("foil_only_card") for p in done["plan"]}
        self.assertEqual(flags, {"en:1-1": None, "en:1-207": True})
        self.assertEqual(done["foil_only_as_normal"], 1)
        stored = {(i["print_id"], i["foil"]) for i in self.client.get("/api/lorcana/collection/").json()}
        self.assertEqual(stored, {("en:1-1", False), ("en:1-207", False)})   # the file's word is kept, not rewritten

    def test_null_card_id_does_not_break_list_or_export(self):
        self.add(quantity=2)
        orphan = CollectionItem(card_id=None, user_id=self.user.id, quantity=1, variant="Normal", lang="en",
                                game="lorcana", condition="NM", added_at=datetime.datetime.utcnow())
        self.db.add(orphan)
        self.db.commit()
        items = self.client.get("/api/lorcana/collection/")
        self.assertEqual(items.status_code, 200, items.text)
        self.assertEqual(sorted(i["print_id"] or "" for i in items.json()), ["", "en:1-1"])
        self.assertEqual(self.client.get("/api/lorcana/collection/export-dreamborn").status_code, 200)
        self.assertEqual(self.post_csv("Set Number,Card Number,Variant,Count\n001,001,normal,2\n").status_code, 200)


    def test_dreamborn_export_round_trips_the_collection(self):
        self.add(quantity=3)
        self.add(foil=True)
        self.add(print_id="en:1-207")
        r = self.client.get("/api/lorcana/collection/export-dreamborn")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        parsed = dream.parse_csv(r.text)
        self.assertEqual(parsed.report()["unmapped"], 0)
        self.assertEqual(dream.totals(parsed.entries),
                         {("en:1-1", False): 3, ("en:1-1", True): 1, ("en:1-207", True): 1})

    def test_language_copies_fold_into_one_dreamborn_row(self):
        self.add(quantity=2)
        self.add(print_id="de:1-1", quantity=1)
        parsed = dream.parse_csv(self.client.get("/api/lorcana/collection/export-dreamborn").text)
        self.assertEqual(dream.totals(parsed.entries), {("en:1-1", False): 3})


class FoilVariantTests(GameAxisBase):
    """Owner 2026-10-16: Lorcana foil is the variant "Foil". Pokemon keeps its four and never accepts it."""

    def test_pokemon_contract_is_unchanged_and_rejects_foil(self):
        from services.collection_variants import (CANONICAL_VARIANTS, COLLECTION_TABLE_VARIANTS, LORCANA_VARIANTS,
                                                  CollectionVariant, normalize_collection_variant)
        self.assertEqual(CANONICAL_VARIANTS, ("Normal", "Holo", "Reverse Holo", "First Edition"))
        self.assertEqual(CollectionVariant.FOIL.value, "Foil")
        self.assertEqual(COLLECTION_TABLE_VARIANTS, CANONICAL_VARIANTS + ("Foil",))
        self.assertEqual(LORCANA_VARIANTS, ("Normal", "Foil"))
        with self.assertRaises(ValueError) as cm:
            normalize_collection_variant("Foil")
        self.assertIn("Normal, Holo, Reverse Holo, First Edition", str(cm.exception))
        self.assertNotIn("Foil", str(cm.exception))

    def test_pokemon_api_refuses_foil_and_writes_nothing(self):
        from api.collection import router as collection_router
        from database import get_db
        self.add_pokemon()
        app = FastAPI()
        app.include_router(collection_router, prefix="/api/collection")
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        with TestClient(app) as client:
            r = client.post("/api/collection/", json={"card_id": "sv1-1_en", "variant": "Foil"})
            bogus = client.post("/api/collection/", json={"card_id": "sv1-1_en", "variant": "Bogus"})
            self.assertEqual(r.status_code, 422, r.text)
            # the same answer as for any other bad value, and the text still lists only the four Pokemon variants
            self.assertEqual(r.json()["detail"][0]["msg"], bogus.json()["detail"][0]["msg"])
            self.assertNotIn("Foil", bogus.json()["detail"][0]["msg"])
        self.assertEqual(self.db.query(CollectionItem).filter(CollectionItem.variant == "Foil").count(), 0)

    def test_collection_table_takes_foil_other_tables_do_not(self):
        from sqlalchemy.exc import IntegrityError
        self.add_pokemon()
        self.db.add(CollectionItem(card_id="sv1-1_en", user_id=self.user.id, quantity=1, lang="en", variant="Foil"))
        self.db.commit()                                         # the model's check on `collection` has Foil
        self.assertEqual(self.db.query(CollectionItem).filter(CollectionItem.variant == "Foil").count(), 1)
        self.db.add(CollectionItem(card_id="sv1-1_en", user_id=self.user.id, quantity=1, lang="en", variant="Shiny"))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()
        from models import COLLECTION_ITEM_VARIANT_CHECK, COLLECTION_VARIANT_CHECK
        self.assertIn("'Foil'", COLLECTION_ITEM_VARIANT_CHECK)
        self.assertNotIn("Foil", COLLECTION_VARIANT_CHECK)       # product_cards / ledger / trade items keep four

    def test_startup_hardening_widens_old_triggers_and_is_idempotent(self):
        from database import harden_collection_variant_constraints
        eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        with eng.begin() as c:
            c.execute(text("CREATE TABLE collection (id INTEGER PRIMARY KEY, variant TEXT NOT NULL DEFAULT 'Normal')"))
            c.execute(text("CREATE TABLE product_cards (id INTEGER PRIMARY KEY, variant TEXT NOT NULL DEFAULT 'Normal')"))
            c.execute(text("CREATE TABLE product_ledger_entries (id INTEGER PRIMARY KEY, variant TEXT)"))
            c.execute(text("CREATE TABLE trade_items (id INTEGER PRIMARY KEY, variant TEXT)"))
            for suffix, action in (("insert", "INSERT"), ("update", "UPDATE OF variant")):     # the pre-E3 four-value trigger
                c.execute(text(f"""CREATE TRIGGER ck_collection_variant_{suffix} BEFORE {action} ON collection
                    WHEN NEW.variant IS NULL OR NEW.variant NOT IN ('Normal','Holo','Reverse Holo','First Edition')
                    BEGIN SELECT RAISE(ABORT, 'unsupported collection variant'); END"""))
            c.execute(text("INSERT INTO collection(variant) VALUES ('Holo')"))
        for _ in range(2):                                       # twice: idempotent
            harden_collection_variant_constraints(eng)
        with eng.begin() as c:
            c.execute(text("INSERT INTO collection(variant) VALUES ('Foil')"))
            c.execute(text("UPDATE collection SET variant='Foil' WHERE variant='Holo'"))
        for table in ("product_cards", "trade_items", "product_ledger_entries"):
            with self.assertRaises(Exception, msg=table):
                with eng.begin() as c:
                    c.execute(text(f"INSERT INTO {table}(variant) VALUES ('Foil')"))
        with self.assertRaises(Exception):
            with eng.begin() as c:
                c.execute(text("INSERT INTO collection(variant) VALUES ('Shiny')"))
        # an existing Foil row is valid for `collection`, so a later start does not stop on it
        harden_collection_variant_constraints(eng)


class PricesTests(GameAxisBase):
    def path(self, day, rows):
        return write_price_file(self.tmp.name, day, rows)

    def test_import_is_append_only_and_idempotent(self):
        f = self.path("2026-10-15", {"en:1-1": {"usd": 0.13, "usd_foil": None}})
        lc = self.lorcana()
        first = import_price_file(lc, f)
        self.assertEqual((first["added"], first["already_there"]), (2, 0))
        second = import_price_file(lc, f)
        self.assertEqual((second["added"], second["already_there"]), (0, 2))
        rows = {(r.finish, r.amount, r.currency, r.source, r.source_file) for r in lc.query(GamePrice)}
        self.assertEqual(rows, {("normal", 0.13, "USD", "lorcast", f.name), ("foil", None, "USD", "lorcast", f.name)})
        lc.close()

    def test_a_new_day_adds_rows_and_the_newest_wins(self):
        lc = self.lorcana()
        import_price_file(lc, self.path("2026-10-15", {"en:1-1": {"usd": 0.13, "usd_foil": 0.75}}))
        import_price_file(lc, self.path("2026-10-16", {"en:1-1": {"usd": 0.20, "usd_foil": 0.80}}))
        self.assertEqual(lc.query(GamePrice).count(), 4)
        p = price_for(lc, "en:1-1", False)
        self.assertEqual((p.amount, p.date, p.inherited, p.finish_fallback), (0.20, "2026-10-16", False, None))
        self.assertEqual(prices_status(lc)["days"], 2)
        lc.close()

    def test_null_is_not_zero_foil_only_falls_back_and_localised_inherits(self):
        lc = self.lorcana()
        import_price_file(lc, self.path("2026-10-16", {
            "en:1-207": {"usd": None, "usd_foil": 952.84}, "en:1-2": {"usd": None, "usd_foil": None}}))
        p = price_for(lc, "en:1-207", False)
        self.assertEqual((p.amount, p.finish, p.finish_fallback), (952.84, "foil", "foil"))
        none = price_for(lc, "en:1-2", False)
        self.assertIsNone(none.amount)                                    # a row, but no price: None, not 0
        self.assertIsNone(price_for(lc, "en:3-4a", False))               # no row at all
        de = price_for(lc, "de:1-207", True)
        self.assertEqual((de.amount, de.inherited, de.print_id), (952.84, True, "en:1-207"))
        lc.close()

    def test_foil_fallback_only_for_foil_only_prints(self):
        lc = self.lorcana()
        import_price_file(lc, self.path("2026-10-16", {
            "en:1-207": {"usd": None, "usd_foil": 952.84},      # Enchanted: foil-only
            "en:1-1": {"usd": None, "usd_foil": 0.75},          # a normal card whose normal price is missing
        }))
        enchanted = price_for(lc, "en:1-207", False)
        self.assertEqual((enchanted.amount, enchanted.finish_fallback), (952.84, "foil"))
        normal = price_for(lc, "en:1-1", False)
        self.assertIsNone(normal.amount)                                  # not valued at the foil price
        self.assertEqual((normal.finish, normal.finish_fallback), ("normal", None))
        self.assertEqual(price_for(lc, "en:1-1", True).amount, 0.75)
        de = price_for(lc, "de:1-207", False)                             # inherited twin keeps the rule
        self.assertEqual((de.amount, de.inherited, de.finish_fallback), (952.84, True, "foil"))
        lc.close()

    def test_every_price_is_usd_and_carries_its_market(self):
        lc = self.lorcana()
        import_price_file(lc, self.path("2026-10-16", {"en:1-1": {"usd": 0.13, "usd_foil": None},
                                                       "en:1-207": {"usd": None, "usd_foil": 952.84}}))
        for pid, foil in (("en:1-1", False), ("de:1-1", False), ("fr:1-1", True), ("en:1-207", False),
                          ("de:1-207", True)):
            info = price_for(lc, pid, foil)
            self.assertIsNotNone(info, (pid, foil))
            d = info.as_dict()
            self.assertEqual((d["currency"], d["market"]), ("USD", "US"), (pid, foil))
        de = price_for(lc, "de:1-1", False).as_dict()             # the English print's dollar price, flagged in the data
        self.assertEqual((de["amount"], de["currency"], de["inherited"], de["print_id"]), (0.13, "USD", True, "en:1-1"))
        self.assertEqual(prices_status(lc)["currency"], "USD")
        lc.close()

    def test_import_is_idempotent_per_day_also_when_two_imports_race(self):
        from sqlalchemy.exc import IntegrityError
        lc = self.lorcana()
        f = self.path("2026-10-16", {"en:1-1": {"usd": 0.13, "usd_foil": 0.75}})
        real_commit, calls = lc.commit, []

        def racing_commit():
            calls.append(1)
            if len(calls) == 1:                                   # the other import wins: its rows are in, ours are refused
                other = self.lorcana()
                import_price_file(other, f)
                other.close()
                raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: game_prices"))
            return real_commit()

        with patch.object(lc, "commit", racing_commit):
            out = import_price_file(lc, f)
        self.assertEqual(lc.query(GamePrice).count(), 2)          # one row per print, finish and day, never doubled
        self.assertEqual((out["added"], out["already_there"]), (0, 2))
        lc.close()

    def test_indexes_fit_the_queries(self):
        names = {i.name: [c.name for c in i.columns] for i in GamePrice.__table__.indexes}
        self.assertEqual(names["ix_game_prices_lookup"], ["game", "print_id", "finish", "price_date"])    # newest price
        self.assertEqual(names["ix_game_prices_day"], ["game", "source", "price_date"])                   # day set, status
        uq = [c for c in GamePrice.__table__.constraints if getattr(c, "name", None) == "uq_game_prices_obs"]
        self.assertEqual([c.name for c in uq[0].columns], ["game", "print_id", "source", "finish", "price_date"])

    def test_non_finite_prices_are_refused(self):
        lc = self.lorcana()
        d = Path(self.tmp.name)
        for i, raw in enumerate(('Infinity', '-Infinity', 'NaN', '1e999')):
            name = f"lorcast_2026-11-0{i + 1}.json"
            (d / name).write_text('{"format": "lorcascan-lorcast-prices", "version": 1, "meta": {}, "prices": '
                                  '{"en:1-1": {"usd": %s, "usd_foil": null}}}' % raw)
            with self.assertRaises(PriceFileError, msg=raw):
                import_price_file(lc, d / name)
        self.assertEqual(lc.query(GamePrice).count(), 0)
        lc.close()

    def test_bad_files_are_refused_and_nothing_is_written(self):
        lc = self.lorcana()
        d = Path(self.tmp.name)
        bad = {
            "lorcast_2026-10-16.json": json.dumps({"format": "other", "version": 1, "prices": {"en:1-1": {}}}),
            "lorcast_2026-10-17.json": json.dumps(price_doc("2026-10-17", {})),
            "lorcast_2026-10-18.json": json.dumps(price_doc("2026-10-19", {"en:1-1": {"usd": 1.0}})),
            "lorcast_2026-10-20.json": json.dumps(price_doc("2026-10-20", {"en:1-1": {"usd": -1.0}})),
            "lorcast_2026-10-21.json": json.dumps(price_doc("2026-10-21", {"1-1": {"usd": 1.0}})),
            "lorcast_2026-10-22.json": json.dumps(price_doc("2026-10-22", {"en:1-1": {"usd": "0.13"}})),
            "prices.json": "{}",
        }
        for name, body in bad.items():
            (d / name).write_text(body)
            with self.assertRaises(PriceFileError, msg=name):
                import_price_file(lc, d / name)
        self.assertEqual(lc.query(GamePrice).count(), 0)
        lc.close()

    def test_latest_file_goes_by_the_date_in_the_name(self):
        for day in ("2026-10-09", "2026-10-16", "2026-10-10"):
            self.path(day, {"en:1-1": {"usd": 1.0, "usd_foil": 2.0}})
        (Path(self.tmp.name) / "lorcast_notes.json").write_text("{}")
        self.assertEqual(latest_file(self.tmp.name).name, "lorcast_2026-10-16.json")
        self.assertIsNone(latest_file(Path(self.tmp.name) / "nope"))
        day, fetched, prices = read_price_file(latest_file(self.tmp.name))
        self.assertEqual((day, fetched, prices["en:1-1"]), (datetime.date(2026, 10, 16), "2026-10-16T06:51:00Z",
                                                            {"usd": 1.0, "usd_foil": 2.0}))

    def test_real_lorcascan_file_parses_when_present(self):
        real = Path("/srv/repos/PicaLens/scanner/lorcascan/data/prices")
        files = sorted(real.glob("lorcast_*.json")) if real.is_dir() else []
        if not files:
            self.skipTest("no lorcascan price file on this host")
        day, _fetched, prices = read_price_file(files[-1])
        lc = self.lorcana()
        out = import_price_file(lc, files[-1])
        self.assertEqual(out["added"], len(prices) * 2)
        self.assertGreater(len(prices), 3000)
        lc.close()


class MigrationListTests(unittest.TestCase):
    @unittest.skipUnless(DEPS, "dependencies missing")
    def test_game_migration_statements_are_additive_and_idempotent_by_form(self):
        stmts = []

        class Conn:
            def execute(self, stmt):
                stmts.append(str(stmt))

            def commit(self):
                pass

            def rollback(self):
                pass

        _run_migrations(Conn())
        game = [s for s in stmts if "game" in s.lower()]
        self.assertTrue(game)
        for s in game:
            head = " ".join(s.split())
            self.assertRegex(head, r"^(ALTER TABLE \w+ ADD COLUMN IF NOT EXISTS game VARCHAR NOT NULL DEFAULT 'pokemon'"
                                   r"|CREATE TABLE IF NOT EXISTS game_prices \(.*|CREATE INDEX IF NOT EXISTS ix_game_prices_(lookup|day) .*)$")
            self.assertNotRegex(head.upper(), r"\b(UPDATE|DELETE|DROP|TRUNCATE)\b")
        self.assertEqual(sorted(s.split()[2] for s in game if s.startswith("ALTER")),
                         ["cards", "collection", "scan_jobs", "sets"])

    @unittest.skipUnless(DEPS, "dependencies missing")
    def test_game_prices_is_in_the_cards_backup_group(self):
        from api.backup import BACKUP_GROUPS
        self.assertIn("game_prices", BACKUP_GROUPS["cards"])


if __name__ == "__main__":
    unittest.main()
