import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import Card, Setting, User
    from services import fingerprint_index
    from services.card_fingerprint import HASH_BYTES
    from services.card_upsert import upsert_card

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _hash(seed):
    return bytes((seed + i) % 256 for i in range(HASH_BYTES))


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class FingerprintIndexTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.owner = User(username="index-owner", hashed_password="x", is_active=True)
        self.stranger = User(username="index-other", hashed_password="x", is_active=True)
        self.db.add_all([
            self.owner,
            self.stranger,
            Setting(key="tcgdex_sync_languages", value="en,de"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
        ])
        self.db.commit()
        fingerprint_index.reset()

    def tearDown(self):
        fingerprint_index.reset()
        self.db.close()
        self.engine.dispose()

    def _card(self, card_id, **kwargs):
        values = dict(
            id=card_id,
            tcg_card_id=card_id.split("_")[0],
            name=card_id,
            number="1",
            set_id="sv1",
            lang="en",
            is_custom=False,
            is_digital=False,
            images_small=f"https://example.invalid/{card_id}.png",
            image_phash=_hash(len(card_id)),
        )
        values.update(kwargs)
        card = Card(**values)
        self.db.add(card)
        return card

    # --- what may be indexed ------------------------------------------------

    def test_only_visible_catalogue_cards_are_indexed(self):
        """The index is one shared structure, so it must hold nothing private.

        Filtering on "image_phash IS NOT NULL" alone put another user's private
        custom cards, digital-only cards while digital sets are off, and cards
        in unsynced languages into every user's shortlist.
        """
        self._card("plain_en")
        self._card("plain_de", lang="de")
        self._card("hidden_fr", lang="fr")
        self._card("digital_en", is_digital=True)
        self._card("private_custom", is_custom=True,
                   custom_owner_id=self.stranger.id)
        self._card("shared_custom", is_custom=True,
                   custom_owner_id=self.stranger.id, is_shared_template=True)
        self._card("own_custom", is_custom=True, custom_owner_id=self.owner.id)
        self.db.commit()

        indexed = {row["id"] for row in fingerprint_index.get(self.db).rows}
        self.assertEqual(indexed, {"plain_en", "plain_de"})

    def test_digital_cards_join_the_index_when_digital_sets_are_enabled(self):
        self._card("digital_en", is_digital=True)
        self.db.commit()
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 0)

        self.db.query(Setting).filter(
            Setting.key == "tcgdex_digital_sets_enabled"
        ).update({"value": "true"})
        self.db.commit()
        fingerprint_index.reset()
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 1)

    def test_malformed_and_mixed_length_hashes_are_dropped(self):
        self._card("good_en")
        self._card("short_en", image_phash=b"\x01\x02")
        self._card("long_en", image_phash=b"\x00" * (HASH_BYTES + 3))
        self._card("empty_en", image_phash=b"")
        self._card("missing_en", image_phash=None)
        self.db.commit()

        snapshot = fingerprint_index.get(self.db)
        self.assertEqual([row["id"] for row in snapshot.rows], ["good_en"])
        # Rows and hashes stay aligned; a short row must not shift the array.
        self.assertEqual(snapshot.packed.shape, (1, HASH_BYTES))

    def test_an_empty_index_is_a_cacheable_answer(self):
        """An un-backfilled catalogue must not rescan the cards table per request.

        Treating "no rows" as stale made every scan run a full table scan under
        the process-wide lock and then return 503 -- free amplification for any
        logged-in user.
        """
        self._card("no_hash_en", image_phash=None)
        self.db.commit()

        loads = []
        real_load = fingerprint_index._load

        def counting_load(db):
            loads.append(1)
            return real_load(db)

        fingerprint_index._load = counting_load
        try:
            for _ in range(5):
                self.assertEqual(len(fingerprint_index.get(self.db).rows), 0)
        finally:
            fingerprint_index._load = real_load
        self.assertEqual(len(loads), 1)

    # --- ordering and snapshots --------------------------------------------

    def test_row_order_is_deterministic_across_rebuilds(self):
        for suffix in ("c", "a", "b"):
            self._card(f"sv1-{suffix}_en")
        self.db.commit()

        first = [row["id"] for row in fingerprint_index.get(self.db).rows]
        fingerprint_index.reset()
        second = [row["id"] for row in fingerprint_index.get(self.db).rows]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_reads_are_one_atomic_snapshot(self):
        """A reader must never get new hashes with stale metadata.

        The packed array and the row list used to be two separate attribute
        loads with no lock, so a read interleaved with a rebuild could return
        one row's hash with another row's identity -- or index past the end of
        the shorter list and 500.
        """
        for i in range(30):
            self._card(f"sv1-{i:03d}_en")
        self.db.commit()

        snapshot = fingerprint_index.get(self.db)
        self.assertEqual(snapshot.packed.shape[0], len(snapshot.rows))

        stop = threading.Event()
        mismatches = []

        def read_forever():
            # A Session is not thread safe; each reader gets its own, as each
            # request does in the app.
            session = self.Session()
            try:
                while not stop.is_set():
                    taken = fingerprint_index.get(session)
                    if taken.packed.shape[0] != len(taken.rows):
                        mismatches.append(taken)
                    for row_index, _ in enumerate(taken.rows):
                        taken.rows[row_index]["id"]
            finally:
                session.close()

        readers = [threading.Thread(target=read_forever) for _ in range(4)]
        for reader in readers:
            reader.start()
        try:
            for i in range(30, 60):
                self._card(f"sv1-{i:03d}_en")
                self.db.commit()
                fingerprint_index.invalidate()
        finally:
            stop.set()
            for reader in readers:
                reader.join()
        self.assertEqual(mismatches, [])

    def test_a_snapshot_is_not_mutated_after_it_is_handed_out(self):
        self._card("first_en")
        self.db.commit()
        held = fingerprint_index.get(self.db)

        self._card("second_en")
        self.db.commit()
        fingerprint_index.invalidate()
        rebuilt = fingerprint_index.get(self.db)

        self.assertEqual(len(held.rows), 1)
        self.assertEqual(len(rebuilt.rows), 2)
        self.assertIsNot(held, rebuilt)

    # --- invalidation -------------------------------------------------------

    def test_an_invalidation_during_a_rebuild_is_not_swallowed(self):
        """The lost-wakeup bug: `dirty = False` after the read cleared a newer flag.

        card_upsert marks the index stale before its commit, so a rebuild that
        starts in between could read the old rows and then clear the flag,
        leaving the index wrong for the full MAX_AGE_SECONDS.
        """
        self._card("first_en")
        self.db.commit()

        real_load = fingerprint_index._load

        def load_then_someone_writes(db):
            snapshot = real_load(db)
            fingerprint_index.invalidate()  # a commit landing mid-rebuild
            return snapshot

        fingerprint_index._load = load_then_someone_writes
        try:
            fingerprint_index.get(self.db)
        finally:
            fingerprint_index._load = real_load

        self._card("second_en")
        self.db.commit()
        # The generation moved on, so the next read must rebuild rather than
        # serve the snapshot that raced the write.
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 2)

    def test_another_process_can_invalidate_through_the_database(self):
        """The backfill script runs in its own process.

        It used to call invalidate(), which only touched its own module global
        and then exited, leaving the server serving old hashes for 15 minutes.
        """
        self._card("first_en")
        self.db.commit()
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 1)

        other_process = self.Session()
        try:
            other_process.add(Card(
                id="second_en", tcg_card_id="second", name="second", number="2",
                set_id="sv1", lang="en", is_custom=False, is_digital=False,
                images_small="https://example.invalid/second.png",
                image_phash=_hash(9),
            ))
            other_process.commit()
            fingerprint_index.bump_version(other_process)
        finally:
            other_process.close()

        self.assertEqual(len(fingerprint_index.get(self.db).rows), 2)

    def test_the_index_is_rebuilt_once_it_reaches_the_age_ceiling(self):
        self._card("first_en")
        self.db.commit()
        stale = fingerprint_index.get(self.db)

        self._card("second_en")
        self.db.commit()
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 1)

        object.__setattr__(
            fingerprint_index._snapshot, "built_at",
            stale.built_at - fingerprint_index.MAX_AGE_SECONDS - 1,
        )
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 2)

    # --- coverage -----------------------------------------------------------

    def test_coverage_reports_a_fraction_and_only_claims_ready_when_it_is(self):
        for i in range(600):
            self._card(f"sv1-{i:04d}_en", image_phash=None)
        self.db.commit()

        stats = fingerprint_index.coverage(self.db)
        self.assertEqual(stats["cards_with_image"], 600)
        self.assertEqual(stats["cards_fingerprinted"], 0)
        self.assertEqual(stats["coverage"], 0.0)
        self.assertFalse(stats["ready"])

        # One fingerprinted card out of 600 is not a usable index.
        self.db.query(Card).filter(Card.id == "sv1-0000_en").update(
            {"image_phash": _hash(1)}
        )
        self.db.commit()
        self.assertFalse(fingerprint_index.coverage(self.db)["ready"])

        self.db.query(Card).update({"image_phash": _hash(2)})
        self.db.commit()
        stats = fingerprint_index.coverage(self.db)
        self.assertEqual(stats["coverage"], 1.0)
        self.assertTrue(stats["ready"])

    def test_coverage_ignores_cards_that_would_never_be_indexed(self):
        self._card("plain_en")
        self._card("hidden_fr", lang="fr")
        self._card("own_custom", is_custom=True, custom_owner_id=self.owner.id)
        self.db.commit()
        self.assertEqual(fingerprint_index.coverage(self.db)["cards_total"], 1)


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class UpsertInvalidationTests(unittest.TestCase):
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
        ])
        self.db.commit()
        fingerprint_index.reset()

    def tearDown(self):
        fingerprint_index.reset()
        self.db.close()
        self.engine.dispose()

    def _payload(self, image):
        return {
            "id": "sv1-1_en", "tcg_card_id": "sv1-1", "name": "Card",
            "number": "1", "set_id": "sv1", "lang": "en", "is_custom": False,
            "images_small": image, "images_large": image,
        }

    def test_a_changed_artwork_url_clears_the_stored_fingerprint(self):
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.db.commit()
        self.db.query(Card).update({"image_phash": _hash(3)})
        self.db.commit()

        upsert_card(self.db, self._payload("https://example.invalid/b.png"))
        self.db.commit()
        self.assertIsNone(self.db.query(Card).one().image_phash)

    def test_an_unchanged_artwork_url_keeps_the_fingerprint(self):
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.db.commit()
        self.db.query(Card).update({"image_phash": _hash(3)})
        self.db.commit()

        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.db.commit()
        self.assertEqual(self.db.query(Card).one().image_phash, _hash(3))

    def test_the_index_is_invalidated_only_once_the_write_is_committed(self):
        """Invalidating before the commit is a lost wakeup, not a safety margin.

        A rebuild racing between the two would read the pre-commit rows and
        cache them as current.
        """
        before = fingerprint_index._generation
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.assertEqual(fingerprint_index._generation, before)
        self.db.commit()
        self.assertGreater(fingerprint_index._generation, before)

    def test_turning_off_a_language_invalidates_the_index(self):
        """Visibility settings change what may be indexed, silently.

        Nothing upserts a card when the catalogue languages change, so without
        this the cards of a language just switched off stay matchable until the
        index ages out.
        """
        from api.settings import _apply_setting_side_effect

        before = fingerprint_index._generation
        _apply_setting_side_effect(self.db, "tcgdex_sync_languages", "en")
        self.db.commit()
        self.assertGreater(fingerprint_index._generation, before)

    def test_a_rolled_back_upsert_does_not_invalidate(self):
        before = fingerprint_index._generation
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.db.rollback()
        self.assertEqual(fingerprint_index._generation, before)


if __name__ == "__main__":
    unittest.main()
