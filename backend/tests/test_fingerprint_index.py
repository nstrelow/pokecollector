import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import sqlalchemy as sa
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import Card, Setting, User
    from services import fingerprint_index
    from services.card_fingerprint import HASH_BYTES
    from services.card_upsert import (
        add_catalogue_card,
        apply_catalogue_fields,
        upsert_card,
    )

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

        Uses a file-backed database and one real connection per thread, like
        CommitVisibilityTests below -- not `self.engine`, whose StaticPool
        gives every session in this class the *same* underlying SQLite
        connection. Sharing one connection across the writer and four reader
        threads serialises them onto unsynchronised transaction state, so the
        test could raise `OperationalError` instead of ever exercising a real
        snapshot mismatch.
        """
        tmpdir = tempfile.mkdtemp()
        try:
            engine = create_engine("sqlite:///" + os.path.join(tmpdir, "snapshot.db"))
            Base.metadata.create_all(engine)
            Session = sessionmaker(bind=engine)
            db = Session()
            db.add_all([
                Setting(key="tcgdex_sync_languages", value="en,de"),
                Setting(key="tcgdex_digital_sets_enabled", value="false"),
            ])
            for i in range(30):
                db.add(Card(
                    id=f"sv1-{i:03d}_en", tcg_card_id=f"sv1-{i:03d}", name=f"sv1-{i:03d}_en",
                    number="1", set_id="sv1", lang="en", is_custom=False, is_digital=False,
                    images_small=f"https://example.invalid/sv1-{i:03d}_en.png",
                    image_phash=_hash(i),
                ))
            db.commit()

            snapshot = fingerprint_index.get(db)
            self.assertEqual(snapshot.packed.shape[0], len(snapshot.rows))

            stop = threading.Event()
            mismatches = []

            def read_forever():
                # A Session is not thread safe; each reader gets its own
                # connection, as each request does in the app.
                session = Session()
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
                    db.add(Card(
                        id=f"sv1-{i:03d}_en", tcg_card_id=f"sv1-{i:03d}", name=f"sv1-{i:03d}_en",
                        number="1", set_id="sv1", lang="en", is_custom=False, is_digital=False,
                        images_small=f"https://example.invalid/sv1-{i:03d}_en.png",
                        image_phash=_hash(i),
                    ))
                    db.commit()
                    fingerprint_index.invalidate()
            finally:
                stop.set()
                for reader in readers:
                    reader.join()
            self.assertEqual(mismatches, [])
            db.close()
            engine.dispose()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

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

    def test_the_version_setting_key_has_not_drifted(self):
        """Pinned to a literal so a rename is a deliberate, visible change.

        Every test below writes `Setting(key=fingerprint_index.VERSION_SETTING_KEY, ...)`
        to simulate a genuinely separate process, which is correct -- they have
        to use whatever key the module actually reads. This is the one place
        that pins the key itself, so a change to it cannot pass silently.
        """
        self.assertEqual(
            fingerprint_index.VERSION_SETTING_KEY, "fingerprint_index_version"
        )

    def test_the_database_marker_alone_forces_a_rebuild(self):
        """The cross-process half, tested without the process-local half.

        The test above cannot tell the two mechanisms apart: bump_version also
        calls invalidate(), and both sessions live in this one process, so
        deleting the version comparison in _is_stale entirely left it green.
        Here the marker row is written by hand -- exactly what a genuinely
        separate process's committed bump looks like from in here -- and nothing
        touches the process-local generation counter.
        """
        self._card("first_en")
        self.db.commit()
        generation = fingerprint_index._generation
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 1)

        self._card("second_en")
        self.db.commit()
        # Still one: nothing has signalled anything yet.
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 1)

        writer = self.Session()
        try:
            writer.add(Setting(
                key=fingerprint_index.VERSION_SETTING_KEY, value="written-elsewhere"
            ))
            writer.commit()
        finally:
            writer.close()

        self.assertEqual(fingerprint_index._generation, generation,
                         "this test must not rely on process-local invalidation")
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 2)

    def test_a_process_that_bumps_the_marker_also_refreshes_itself(self):
        """The process-local half, tested without the database half.

        bump_version's own process holds a snapshot too. Writing the marker and
        forgetting to invalidate locally left the writer -- the one process that
        certainly knows the catalogue changed -- serving its own stale copy,
        because _is_stale would then compare the new marker against a snapshot
        it had just rebuilt from.
        """
        self._card("first_en")
        self.db.commit()
        fingerprint_index.get(self.db)

        before = fingerprint_index._generation
        fingerprint_index.bump_version(self.db)
        self.assertGreater(fingerprint_index._generation, before)

    def test_the_marker_is_not_a_read_modify_write(self):
        """Two processes bumping at once must not lose one of the two updates.

        A read-then-increment loses a bump under concurrency, and racing to
        create the row raised a duplicate-key IntegrityError. The value only
        ever has to be *different*, so it is written blind.
        """
        # From nothing, with the row absent.
        first = fingerprint_index.bump_version(self.db)
        self.assertEqual(fingerprint_index.read_version(self.db), first)
        second = fingerprint_index.bump_version(self.db)
        self.assertNotEqual(second, first)
        self.assertEqual(fingerprint_index.read_version(self.db), second)

        statements = []

        def record(conn, cursor, statement, *rest):
            statements.append(statement.strip().upper())

        sa.event.listen(self.engine, "before_cursor_execute", record)
        try:
            fingerprint_index.bump_version(self.db)
        finally:
            sa.event.remove(self.engine, "before_cursor_execute", record)
        self.assertTrue(any(s.startswith("UPDATE") for s in statements))
        self.assertFalse(
            [s for s in statements if s.startswith("SELECT") and "SETTINGS" in s],
            f"the marker must not be read back before being written: {statements}",
        )

    def test_bumping_from_a_second_session_survives_a_racing_first_write(self):
        """Both processes see the row appear; neither may raise."""
        other = self.Session()
        try:
            other.add(Setting(
                key=fingerprint_index.VERSION_SETTING_KEY, value="theirs"
            ))
            other.commit()
            value = fingerprint_index.bump_version(self.db)
        finally:
            other.close()
        self.assertNotEqual(value, "theirs")
        self.assertEqual(fingerprint_index.read_version(self.db), value)

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

    def test_coverage_can_never_exceed_one(self):
        """It divided hashes by artwork URLs and those are different sets.

        A card that kept a hash after its images_small was cleared counted in
        the numerator and not the denominator, and /status reported more than
        100% coverage.
        """
        for i in range(4):
            self._card(f"sv1-{i:04d}_en")
        self._card("orphan_en", images_small=None)
        self.db.commit()

        stats = fingerprint_index.coverage(self.db)
        self.assertEqual(stats["cards_total"], 5)
        self.assertEqual(stats["cards_with_image"], 4)
        self.assertEqual(stats["coverage"], 1.0)
        self.assertLessEqual(stats["coverage"], 1.0)

    def test_readiness_is_a_coverage_rule_not_just_a_non_empty_index(self):
        """/status and the scan endpoint must agree on what "ready" means."""
        for i in range(600):
            self._card(f"sv1-{i:05d}_en", image_phash=None)
        self.db.commit()
        self.assertFalse(fingerprint_index.coverage(self.db)["ready"])
        self.assertFalse(fingerprint_index.get(self.db).ready)

        # A handful of hashes is an index, but not a usable one.
        self.db.query(Card).filter(Card.id < "sv1-00010_en").update(
            {"image_phash": _hash(1)}
        )
        self.db.commit()
        fingerprint_index.reset()
        self.assertGreater(len(fingerprint_index.get(self.db).rows), 0)
        self.assertFalse(fingerprint_index.get(self.db).ready)
        self.assertFalse(fingerprint_index.coverage(self.db)["ready"])

        self.db.query(Card).update({"image_phash": _hash(2)})
        self.db.commit()
        fingerprint_index.reset()
        self.assertTrue(fingerprint_index.get(self.db).ready)
        self.assertTrue(fingerprint_index.coverage(self.db)["ready"])

    def test_readiness_and_coverage_agree_when_a_hash_outlives_its_artwork(self):
        """The two numerators must count the same thing, not just agree by luck.

        `_load`'s readiness used to admit a row with a hash but no
        `images_small` (the same anomaly `test_coverage_can_never_exceed_one`
        covers for `coverage()`: a writer that clears artwork without also
        clearing the hash) into its numerator, while `coverage()` never did.
        Here every real card is fingerprinted-but-unindexable-as-coverage: zero
        of them actually have both artwork and a hash. 500 orphaned rows (hash,
        no artwork) alone clear READY_MIN_CARDS, so the old code called this
        catalogue ready off nothing but the anomaly, while /status -- built
        from `coverage()` -- correctly still said no. `/api/cards/recognize/
        local` and its own `/status` must not be able to disagree that way.
        """
        for i in range(fingerprint_index.READY_MIN_CARDS):
            self._card(f"real-{i:05d}_en", image_phash=None)
        for i in range(fingerprint_index.READY_MIN_CARDS):
            self._card(f"orphan-{i:05d}_en", images_small=None)
        self.db.commit()

        stats = fingerprint_index.coverage(self.db)
        self.assertEqual(stats["cards_fingerprinted"], 0)
        self.assertFalse(stats["ready"])
        self.assertEqual(
            fingerprint_index.get(self.db).ready, stats["ready"],
            "the index and /status must not disagree about readiness",
        )

    def test_the_readiness_thresholds_have_not_drifted(self):
        """Pin READY_MIN_CARDS and READY_COVERAGE to literals, not each other.

        A fixture that derives its card count from `READY_MIN_CARDS` (as the
        test above used to) stays green no matter what the constant is
        changed to -- it tests that coverage() agrees with the module, not
        that the tuned threshold survived. Both boundaries are exercised here
        with literal counts instead.
        """
        self.assertEqual(fingerprint_index.READY_MIN_CARDS, 500)
        self.assertEqual(fingerprint_index.READY_COVERAGE, 0.5)

        # One below READY_MIN_CARDS, fully hashed: coverage is perfect but
        # there still are not enough cards to trust it.
        for i in range(499):
            self._card(f"sv1-{i:05d}_en", image_phash=_hash(i))
        self.db.commit()
        stats = fingerprint_index.coverage(self.db)
        self.assertEqual(stats["cards_total"], 499)
        self.assertEqual(stats["coverage"], 1.0)
        self.assertFalse(stats["ready"])

        # Exactly READY_MIN_CARDS, still fully hashed: now ready.
        self._card("sv1-00499_en", image_phash=_hash(499))
        self.db.commit()
        stats = fingerprint_index.coverage(self.db)
        self.assertEqual(stats["cards_total"], 500)
        self.assertTrue(stats["ready"])

        # Isolate the coverage boundary at a card count well above
        # READY_MIN_CARDS, so only the fraction crosses 0.5, not the count:
        # 1200 cards, 600 hashed is exactly 0.5.
        for i in range(500, 1200):
            self._card(f"sv1-{i:05d}_en", image_phash=None)
        self.db.commit()
        self.db.query(Card).update({"image_phash": None})
        self.db.query(Card).filter(Card.id < "sv1-00600_en").update(
            {"image_phash": _hash(1)}
        )
        self.db.commit()
        stats = fingerprint_index.coverage(self.db)
        self.assertEqual(stats["cards_total"], 1200)
        self.assertEqual(stats["coverage"], 0.5)
        self.assertTrue(stats["ready"])

        # One hash short of that boundary: not ready, even though the
        # fingerprinted count (599) is still comfortably above READY_MIN_CARDS.
        self.db.query(Card).filter(Card.id == "sv1-00599_en").update(
            {"image_phash": None}
        )
        self.db.commit()
        stats = fingerprint_index.coverage(self.db)
        self.assertLess(stats["coverage"], 0.5)
        self.assertGreaterEqual(
            stats["cards_fingerprinted"], fingerprint_index.READY_MIN_CARDS
        )
        self.assertFalse(stats["ready"])

    # --- concurrency --------------------------------------------------------

    def test_a_rebuild_does_not_block_everyone_else(self):
        """One slow rebuild must not queue up every concurrent scan behind it.

        A rebuild reads the whole cards table (1-3s at catalogue scale) and
        every invalidation -- a settings change, a sync -- triggers one. Holding
        the process-wide lock across it meant each waiter also held an anyio
        worker thread and a pooled database connection for the full rebuild.
        Readers take the slightly stale snapshot instead.
        """
        self._card("first_en")
        self.db.commit()
        fingerprint_index.get(self.db)  # prime, so there is something to serve

        self._card("second_en")
        self.db.commit()
        fingerprint_index.invalidate()

        started = threading.Event()
        release = threading.Event()
        real_load = fingerprint_index._load

        def slow_load(db):
            started.set()
            release.wait(5)
            return real_load(db)

        fingerprint_index._load = slow_load
        rebuilt = []
        waiter_result = []
        try:
            rebuilder = threading.Thread(
                target=lambda: rebuilt.append(
                    len(fingerprint_index.get(self.Session()).rows)
                )
            )
            rebuilder.start()
            self.assertTrue(started.wait(5), "the rebuild never started")

            def waiter():
                session = self.Session()
                try:
                    waiter_result.append(len(fingerprint_index.get(session).rows))
                finally:
                    session.close()

            reader = threading.Thread(target=waiter)
            reader.start()
            reader.join(5)
            self.assertFalse(reader.is_alive(), "a reader queued behind the rebuild")
            # Served the stale snapshot rather than waiting for the new one.
            self.assertEqual(waiter_result, [1])
        finally:
            release.set()
            rebuilder.join(5)
            fingerprint_index._load = real_load
        self.assertEqual(rebuilt, [2])

    def test_the_very_first_load_does_block(self):
        """With nothing cached there is nothing to serve, so waiting is correct."""
        self._card("first_en")
        self.db.commit()
        self.assertEqual(len(fingerprint_index.get(self.db).rows), 1)


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

    def test_a_savepoint_rollback_does_not_discard_a_pending_invalidation(self):
        """after_rollback fires for ROLLBACK TO SAVEPOINT as well.

        api/cards.py wraps parts of the custom-card migration in begin_nested().
        Popping the flag there threw away an invalidation for a card the
        enclosing transaction then committed anyway, so the index kept serving
        the old catalogue for a full MAX_AGE_SECONDS. Over-invalidating costs
        one rebuild; under-invalidating serves the wrong card.
        """
        before = fingerprint_index._generation
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))

        savepoint = self.db.begin_nested()
        self.db.add(Card(
            id="scratch_en", tcg_card_id="scratch", name="scratch", number="9",
            set_id="sv1", lang="en", is_custom=False,
        ))
        savepoint.rollback()

        self.db.commit()
        # The upserted card really is in the database...
        self.assertEqual(
            self.db.query(Card).filter(Card.id == "sv1-1_en").count(), 1
        )
        self.assertEqual(
            self.db.query(Card).filter(Card.id == "scratch_en").count(), 0
        )
        # ...so the index must have been told.
        self.assertGreater(fingerprint_index._generation, before)

    def test_a_nested_savepoint_rollback_also_keeps_the_flag(self):
        before = fingerprint_index._generation
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        outer = self.db.begin_nested()
        inner = self.db.begin_nested()
        inner.rollback()
        outer.rollback()
        self.db.commit()
        self.assertGreater(fingerprint_index._generation, before)

    def test_the_three_field_by_field_writers_share_the_staleness_guard(self):
        """The catalogue is written from more places than upsert_card.

        api/cards.py's custom-to-API migration and api/collection.py's CSV
        import cache both copied parsed fields onto an existing row one by one,
        images_small included, which left the old artwork's fingerprint in
        place. That is silent permanent corruption: photos of the OLD picture
        matched at distance 0 and the backfill never revisited the row.
        """
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.db.commit()
        self.db.query(Card).update({
            "image_phash": _hash(3),
            "image_phash_source": "https://example.invalid/a.png",
        })
        self.db.commit()
        card = self.db.query(Card).one()

        before = fingerprint_index._generation
        apply_catalogue_fields(
            self.db, card, self._payload("https://example.invalid/b.png")
        )
        self.db.commit()

        self.assertEqual(card.images_small, "https://example.invalid/b.png")
        self.assertIsNone(card.image_phash)
        self.assertIsNone(card.image_phash_source)
        self.assertGreater(fingerprint_index._generation, before)

    def test_a_field_by_field_write_that_keeps_the_url_keeps_the_fingerprint(self):
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.db.commit()
        self.db.query(Card).update({
            "image_phash": _hash(3),
            "image_phash_source": "https://example.invalid/a.png",
        })
        self.db.commit()
        card = self.db.query(Card).one()

        apply_catalogue_fields(
            self.db, card, self._payload("https://example.invalid/a.png")
        )
        self.db.commit()
        self.assertEqual(card.image_phash, _hash(3))

    def test_inserting_a_catalogue_card_also_tells_the_index(self):
        """A new row carries no stale hash, but the index still has to learn of it."""
        before = fingerprint_index._generation
        add_catalogue_card(self.db, Card(
            id="sv1-2_en", tcg_card_id="sv1-2", name="Card", number="2",
            set_id="sv1", lang="en", is_custom=False,
            images_small="https://example.invalid/c.png",
        ))
        self.assertEqual(fingerprint_index._generation, before)
        self.db.commit()
        self.assertGreater(fingerprint_index._generation, before)

    def test_upsert_records_where_a_later_fingerprint_would_come_from(self):
        """Clearing the hash must clear its provenance too.

        Leaving the old source behind would make the row look "already tried"
        to the backfill, and it would never be fingerprinted again.
        """
        upsert_card(self.db, self._payload("https://example.invalid/a.png"))
        self.db.commit()
        self.db.query(Card).update({
            "image_phash": _hash(3),
            "image_phash_source": "https://example.invalid/a.png",
        })
        self.db.commit()

        upsert_card(self.db, self._payload("https://example.invalid/b.png"))
        self.db.commit()
        card = self.db.query(Card).one()
        self.assertIsNone(card.image_phash)
        self.assertIsNone(card.image_phash_source)


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CommitVisibilityTests(unittest.TestCase):
    """The index must not be invalidated until the rows are actually visible.

    Uses a file-backed SQLite database and a genuinely separate connection,
    because that is the only way to tell before_commit from after_commit: at
    before_commit time the write is still private to the writing transaction, so
    a rebuild triggered then would read the OLD rows and cache them as current.
    The previous test compared the generation counter before and after
    `db.commit()` returned, which both hooks satisfy.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.url = "sqlite:///" + os.path.join(self.dir, "index.db")
        self.engine = create_engine(self.url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.db.add_all([
            Setting(key="tcgdex_sync_languages", value="en,de"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
        ])
        self.db.commit()
        self.observer = create_engine(self.url)
        fingerprint_index.reset()

    def tearDown(self):
        fingerprint_index.reset()
        self.db.close()
        self.engine.dispose()
        self.observer.dispose()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_row_is_visible_to_other_connections_when_the_index_is_invalidated(self):
        seen = []
        real_invalidate = fingerprint_index.invalidate

        def observing_invalidate():
            with self.observer.connect() as conn:
                seen.append(conn.execute(
                    sa.text("select count(*) from cards where id = 'sv1-1_en'")
                ).scalar())
            real_invalidate()

        fingerprint_index.invalidate = observing_invalidate
        try:
            upsert_card(self.db, {
                "id": "sv1-1_en", "tcg_card_id": "sv1-1", "name": "Card",
                "number": "1", "set_id": "sv1", "lang": "en", "is_custom": False,
                "images_small": "https://example.invalid/a.png",
                "images_large": "https://example.invalid/a.png",
            })
            self.db.commit()
        finally:
            fingerprint_index.invalidate = real_invalidate

        self.assertEqual(
            seen, [1],
            "the index was invalidated while the write was still uncommitted, so "
            "a rebuild racing it would cache the pre-commit catalogue",
        )


if __name__ == "__main__":
    unittest.main()
