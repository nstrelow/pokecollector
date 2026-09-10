"""The catalogue is written from more places than upsert_card.

Three of those writers copied parsed TCGdex fields onto an existing row one by
one -- `images_small` included -- without clearing `cards.image_phash`. Because
the backfill only ever selected rows with a NULL hash, a fingerprint that
survived a URL rotation was never recomputed: photographs of the OLD artwork
kept matching it at distance 0 with a wide margin, and nothing repaired it.

These tests run the real call sites, not the helper they now share.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api.auth import get_current_user
    from api.cards import router as cards_router
    from api.collection import _find_card_by_code, ensure_card_exists
    from database import Base, get_db
    from models import Card, CustomCardMatch, Set, Setting, User
    from services import fingerprint_backfill, fingerprint_index
    from services.card_upsert import invalidate_fingerprint_index_after_commit

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False

OLD_URL = "https://assets.invalid/sv1/1/old.png"
NEW_URL = "https://assets.invalid/sv1/1/new.png"
STALE_HASH = bytes(range(8))


@unittest.skipUnless(DEPS_AVAILABLE, "API dependencies are not installed")
class CatalogueWriterFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.db.add_all([
            Setting(key="tcgdex_sync_languages", value="en,de"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
            Set(id="sv1_en", tcg_set_id="sv1", name="Scarlet & Violet",
                abbreviation="SV1", total=10, lang="en", is_digital=False),
        ])
        self.db.commit()
        fingerprint_index.reset()

    def tearDown(self):
        fingerprint_index.reset()
        self.db.close()
        self.engine.dispose()

    def _seed_fingerprinted_card(self):
        self.db.add(Card(
            id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito", number="1",
            set_id="sv1", lang="en", is_custom=False, is_digital=False,
            images_small=OLD_URL, images_large=OLD_URL,
            image_phash=STALE_HASH, image_phash_source=OLD_URL,
        ))
        self.db.commit()

    def _api_card(self, url):
        return {
            "id": "sv1-1", "name": "Sprigatito",
            "localId": "1", "image": url.rsplit(".", 1)[0],
            "set": {"id": "sv1", "name": "Scarlet & Violet", "cardCount": {"total": 10}},
        }

    def _parsed(self, number, url):
        return {
            "id": f"sv1-{number}_en", "tcg_card_id": f"sv1-{number}",
            "name": f"Card {number}", "number": number, "set_id": "sv1",
            "lang": "en", "is_custom": False,
            "images_small": url, "images_large": url,
        }

    def _import_lookup(self, wanted_number, refreshed):
        """Drive _find_card_by_code down its "cache the whole set" branch.

        It only reaches the TCGdex refresh when the requested number is not in
        the database yet, and then it rewrites every card in the set -- which is
        where an existing, already fingerprinted row gets its artwork rotated.
        """
        with patch("api.collection.pokemon_api.get_set_cards",
                   return_value={"cards": [{}] * len(refreshed)}), \
             patch("api.collection.pokemon_api.parse_card_for_db",
                   side_effect=[dict(row) for row in refreshed]):
            return _find_card_by_code(self.db, "SV1", wanted_number, "en")

    def test_the_csv_import_card_cache_does_not_leave_a_stale_fingerprint(self):
        """api/collection.py's _find_card_by_code refreshes a whole set."""
        self._seed_fingerprinted_card()
        before = fingerprint_index._generation

        found = self._import_lookup("2", [
            self._parsed("1", NEW_URL),   # existing row, new artwork URL
            self._parsed("2", NEW_URL),   # the card actually being imported
        ])
        self.assertEqual(found.id, "sv1-2_en")

        rotated = self.db.query(Card).filter(Card.id == "sv1-1_en").one()
        self.assertEqual(rotated.images_small, NEW_URL)
        self.assertIsNone(rotated.image_phash,
                          "the hash of the previous artwork survived the rotation")
        self.assertIsNone(rotated.image_phash_source)
        self.assertGreater(fingerprint_index._generation, before)

    def test_the_csv_import_card_cache_keeps_a_fingerprint_it_should_keep(self):
        self._seed_fingerprinted_card()
        self._import_lookup("2", [
            self._parsed("1", OLD_URL),
            self._parsed("2", NEW_URL),
        ])
        unchanged = self.db.query(Card).filter(Card.id == "sv1-1_en").one()
        self.assertEqual(unchanged.image_phash, STALE_HASH)
        self.assertEqual(unchanged.image_phash_source, OLD_URL)

    def test_the_csv_import_card_cache_tells_the_index_about_new_rows(self):
        before = fingerprint_index._generation
        self._import_lookup("2", [self._parsed("2", NEW_URL)])
        self.assertGreater(fingerprint_index._generation, before)

    def test_ensure_card_exists_tells_the_index_about_the_row_it_inserts(self):
        before = fingerprint_index._generation
        parsed = {
            "id": "sv1-3_en", "tcg_card_id": "sv1-3", "name": "Meowscarada",
            "number": "3", "set_id": "sv1", "lang": "en", "is_custom": False,
            "images_small": NEW_URL, "images_large": NEW_URL,
        }
        with patch("api.collection.pokemon_api.get_card", return_value={"id": "sv1-3"}), \
             patch("api.collection.pokemon_api.parse_card_for_db",
                   return_value=dict(parsed)):
            card = ensure_card_exists(self.db, "sv1-3_en")
        self.assertEqual(card.id, "sv1-3_en")
        self.assertGreater(fingerprint_index._generation, before)

    # --- api/cards.py: the custom-to-API migration -------------------------

    def _migration_client(self):
        user = User(username="migrator", hashed_password="x", is_active=True)
        self.db.add(user)
        self.db.commit()
        app = FastAPI()
        app.include_router(cards_router, prefix="/api/cards")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app), user

    def _pending_match(self, user):
        self.db.add(Card(
            id="custom-1", tcg_card_id=None, name="My Sprigatito", number="1",
            set_id="sv1", lang="en", is_custom=True, custom_owner_id=user.id,
            images_small="https://mine.invalid/photo.png",
        ))
        match = CustomCardMatch(
            custom_card_id="custom-1", api_card_id="sv1-1", status="pending"
        )
        self.db.add(match)
        self.db.commit()
        return match

    def test_the_custom_card_migration_does_not_leave_a_stale_fingerprint(self):
        """api/cards.py's migrate_custom_card updates the API twin in place.

        It also runs parts of its work inside begin_nested(), so this covers
        the savepoint case end to end: `after_rollback` fires for ROLLBACK TO
        SAVEPOINT, and popping the pending invalidation there threw away the
        signal for rows the enclosing transaction went on to commit.
        """
        self._seed_fingerprinted_card()   # the API card already exists, hashed
        client, user = self._migration_client()
        match = self._pending_match(user)
        before = fingerprint_index._generation

        parsed = self._parsed("1", NEW_URL)
        with patch("api.cards.pokemon_api.get_card", return_value={"id": "sv1-1"}), \
             patch("api.cards.pokemon_api.parse_card_for_db", return_value=dict(parsed)):
            response = client.post(f"/api/cards/custom/migrate/{match.id}")
        self.assertEqual(response.status_code, 200, response.text)

        card = self.db.query(Card).filter(Card.id == "sv1-1_en").one()
        self.assertEqual(card.images_small, NEW_URL)
        self.assertIsNone(card.image_phash,
                          "the hash of the previous artwork survived the rotation")
        self.assertIsNone(card.image_phash_source)
        self.assertGreater(
            fingerprint_index._generation, before,
            "the savepoint rollback inside the endpoint swallowed the invalidation",
        )
        client.close()

    def test_invalidation_waits_for_the_outer_commit_across_a_savepoint(self):
        """`after_commit` also fires when a SAVEPOINT commits.

        `migrate_custom_card` sets the dirty flag via `apply_catalogue_fields`
        and only later opens `db.begin_nested()` for an unrelated re-assignment.
        If the `after_commit` handler flushed at that inner SAVEPOINT commit
        (`in_nested_transaction()` is still true there), a rebuild racing the
        real outer commit would cache pre-commit data and stay stale for
        MAX_AGE_SECONDS. Invalidation must happen exactly once, and only once
        the outer transaction actually commits.
        """
        before = fingerprint_index._generation

        # Mirrors api/cards.py's migrate_custom_card: the helper call that
        # marks the session dirty happens strictly before the begin_nested()
        # block, in the same outer transaction.
        invalidate_fingerprint_index_after_commit(self.db)
        self.db.add(Card(
            id="sv1-9_en", tcg_card_id="sv1-9", name="Placeholder", number="9",
            set_id="sv1", lang="en", is_custom=False, is_digital=False,
        ))

        with self.db.begin_nested():
            self.db.flush()
        self.assertEqual(
            fingerprint_index._generation, before,
            "invalidation fired at the SAVEPOINT commit instead of waiting "
            "for the outer transaction",
        )

        self.db.commit()
        self.assertEqual(
            fingerprint_index._generation, before + 1,
            "invalidation should fire exactly once, at the outer commit",
        )

    def test_the_custom_card_migration_tells_the_index_about_a_new_row(self):
        client, user = self._migration_client()
        match = self._pending_match(user)
        before = fingerprint_index._generation

        parsed = self._parsed("1", NEW_URL)
        with patch("api.cards.pokemon_api.get_card", return_value={"id": "sv1-1"}), \
             patch("api.cards.pokemon_api.parse_card_for_db", return_value=dict(parsed)):
            response = client.post(f"/api/cards/custom/migrate/{match.id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            self.db.query(Card).filter(Card.id == "sv1-1_en").count(), 1
        )
        self.assertGreater(fingerprint_index._generation, before)
        client.close()

    # --- services/sync_service.py: the bulk is_digital flip -----------------

    def test_a_sync_that_marks_cards_digital_invalidates_the_index(self):
        """is_digital decides what may be indexed, and sync flips it in bulk.

        api/settings.py invalidates when an administrator toggles digital sets,
        but a full sync runs `refresh_digital_catalogue_flags` on its own and
        commits the result with no signal at all, so cards silently entered or
        left the index and the running process kept matching against the old
        set for up to MAX_AGE_SECONDS.
        """
        from services.sync_service import perform_full_sync

        before = fingerprint_index._generation
        with patch("services.sync_service._get_tcgdex_sync_languages",
                   return_value=["en"]), \
             patch("services.sync_service.digital_sets_enabled", return_value=False), \
             patch("services.sync_service.refresh_digital_catalogue_flags",
                   return_value={"sets_marked": 2, "cards_marked": 40}), \
             patch("services.sync_service.get_pinned_set_language_pairs",
                   return_value=set()), \
             patch("services.sync_service.pokemon_api.get_all_sets",
                   side_effect=RuntimeError("stop here")), \
             patch("services.sync_service.logger.error"):
            with self.assertRaises(RuntimeError):
                perform_full_sync(self.db)

        self.assertGreater(fingerprint_index._generation, before)

    def test_a_sync_that_marks_nothing_does_not_invalidate(self):
        from services.sync_service import perform_full_sync

        before = fingerprint_index._generation
        with patch("services.sync_service._get_tcgdex_sync_languages",
                   return_value=["en"]), \
             patch("services.sync_service.digital_sets_enabled", return_value=False), \
             patch("services.sync_service.refresh_digital_catalogue_flags",
                   return_value={"sets_marked": 0, "cards_marked": 0}), \
             patch("services.sync_service.get_pinned_set_language_pairs",
                   return_value=set()), \
             patch("services.sync_service.pokemon_api.get_all_sets",
                   side_effect=RuntimeError("stop here")), \
             patch("services.sync_service.logger.error"):
            with self.assertRaises(RuntimeError):
                perform_full_sync(self.db)

        self.assertEqual(fingerprint_index._generation, before)

    def test_a_writer_that_forgets_is_repaired_by_the_backfill_anyway(self):
        """Defence in depth, and the part that does not rely on discipline.

        A guard every call site has to remember has already failed once here.
        Even with the guard bypassed entirely, the row itself now says its hash
        is not a hash of its current picture, so the next backfill re-queues it.
        """
        self._seed_fingerprinted_card()
        card = self.db.query(Card).one()
        # Exactly what a future writer that forgot would do.
        card.images_small = NEW_URL
        self.db.commit()

        pending = fingerprint_backfill.pending_cards(self.db)
        self.assertEqual([row.id for row in pending], ["sv1-1_en"])


if __name__ == "__main__":
    unittest.main()
