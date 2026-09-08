import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PIL import Image
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import Card, Setting, User
    from services import fingerprint_backfill, fingerprint_index

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _png(seed):
    image = Image.new("RGB", (60, 84))
    image.putdata([
        ((x * 7 + seed * 31) % 256, (x * 3) % 256, (x // 5 + seed * 13) % 256)
        for x in range(60 * 84)
    ])
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


@unittest.skipUnless(DEPS_AVAILABLE, "Backfill dependencies are not installed")
class FingerprintBackfillTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.owner = User(username="backfill-owner", hashed_password="x", is_active=True)
        self.db.add_all([
            self.owner,
            Setting(key="tcgdex_sync_languages", value="en,de"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
        ])
        self.db.commit()
        fingerprint_index.reset()

    def tearDown(self):
        fingerprint_index.reset()
        self.db.close()
        self.engine.dispose()

    def _add(self, card_id, **kwargs):
        values = dict(
            id=card_id, tcg_card_id=card_id, name=card_id, number="1",
            set_id="sv1", lang="en", is_custom=False, is_digital=False,
            images_small=f"https://example.invalid/{card_id}.png",
            image_phash=None,
        )
        values.update(kwargs)
        self.db.add(Card(**values))
        self.db.commit()

    def _run(self, responses, **kwargs):
        """Run a backfill with the network replaced by a lookup table."""
        def fake_download(client, url, pacer, attempts=4):
            return responses[url]

        with patch.object(fingerprint_backfill, "download_image", fake_download):
            return fingerprint_backfill.run_backfill(
                self.db, rps=1000, workers=2, **kwargs
            )

    # --- what gets fingerprinted -------------------------------------------

    def test_custom_cards_are_never_fingerprinted(self):
        """Settled product rule: a custom card must not enter the shared index.

        Not filtering them here would mean computing and storing a hash that the
        index then has to remember to throw away.
        """
        self._add("plain_en")
        self._add("mine_custom", is_custom=True, custom_owner_id=self.owner.id)
        self._add("theirs_custom", is_custom=True, custom_owner_id=None,
                  is_shared_template=True)
        self._add("hidden_fr", lang="fr")

        rows = fingerprint_backfill.pending_cards(self.db)
        self.assertEqual([row.id for row in rows], ["plain_en"])

    def test_the_limit_is_applied_in_sql_not_after_loading_everything(self):
        for i in range(25):
            self._add(f"sv1-{i:03d}_en")

        statements = []

        def record(conn, cursor, statement, *rest):
            statements.append(statement)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            rows = fingerprint_backfill.pending_cards(self.db, limit=5)
        finally:
            event.remove(self.engine, "before_cursor_execute", record)
        selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertTrue(selects)
        self.assertIn("LIMIT", selects[-1].upper())
        self.assertEqual(len(rows), 5)
        # Deterministic, so a resumed run does not reshuffle the queue.
        self.assertEqual([row.id for row in rows],
                         [f"sv1-{i:03d}_en" for i in range(5)])

    def test_cards_that_already_have_a_fingerprint_are_skipped(self):
        self._add("done_en", image_phash=b"\x01" * 8)
        self._add("todo_en")
        self.assertEqual(
            [row.id for row in fingerprint_backfill.pending_cards(self.db)],
            ["todo_en"],
        )
        self.assertEqual(
            len(fingerprint_backfill.pending_cards(self.db, refresh=True)), 2
        )

    # --- storing ------------------------------------------------------------

    def test_a_successful_run_stores_hashes_and_signals_other_processes(self):
        self._add("a_en")
        self._add("b_en")
        responses = {
            "https://example.invalid/a_en.png": fingerprint_backfill.Download(_png(1)),
            "https://example.invalid/b_en.png": fingerprint_backfill.Download(_png(2)),
        }
        before = fingerprint_index.read_version(self.db)
        result = self._run(responses)

        self.assertEqual(result["stored"], 2)
        stored = {card.id: card.image_phash for card in self.db.query(Card)}
        self.assertEqual(len(stored["a_en"]), 8)
        self.assertNotEqual(stored["a_en"], stored["b_en"])
        self.assertNotEqual(fingerprint_index.read_version(self.db), before)

    def test_an_unreachable_cdn_never_destroys_an_existing_fingerprint(self):
        """A refresh must not turn a network blip into data loss."""
        self._add("a_en", image_phash=b"\x07" * 8)
        responses = {
            "https://example.invalid/a_en.png": fingerprint_backfill.Download(None),
        }
        result = self._run(responses, refresh=True)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["cleared"], 0)
        self.assertEqual(self.db.query(Card).one().image_phash, b"\x07" * 8)

    def test_a_refresh_clears_the_fingerprint_of_artwork_that_is_gone(self):
        """A hash pointing at an image that no longer exists is worse than none.

        Left in place it keeps competing for rank 1 against every real photo,
        and nothing would ever remove it.
        """
        self._add("a_en", image_phash=b"\x07" * 8)
        responses = {
            "https://example.invalid/a_en.png":
                fingerprint_backfill.Download(None, absent=True),
        }
        result = self._run(responses, refresh=True)

        self.assertEqual(result["cleared"], 1)
        self.assertIsNone(self.db.query(Card).one().image_phash)

    def test_a_cdn_placeholder_is_detected_and_discarded(self):
        """One "image unavailable" render shared by many cards is not a match.

        Every affected card would get the identical hash and they would collide
        at distance 0 at the top of every shortlist. Distinct real renders are
        never byte-identical, so a repeated identical download is the signal.
        """
        count = fingerprint_backfill.PLACEHOLDER_REPEATS + 4
        placeholder = _png(99)
        responses = {}
        for i in range(count):
            self._add(f"ph-{i:03d}_en")
            responses[f"https://example.invalid/ph-{i:03d}_en.png"] = \
                fingerprint_backfill.Download(placeholder)
        self._add("real_en")
        responses["https://example.invalid/real_en.png"] = \
            fingerprint_backfill.Download(_png(5))

        result = self._run(responses)

        self.assertEqual(result["placeholders"], count)
        self.assertEqual(result["stored"], 1)
        remaining = {
            card.id for card in self.db.query(Card)
            if card.image_phash is not None
        }
        self.assertEqual(remaining, {"real_en"})

    def test_nothing_to_do_is_not_an_error(self):
        result = fingerprint_backfill.run_backfill(self.db)
        self.assertEqual(result["considered"], 0)
        self.assertEqual(result["stored"], 0)


@unittest.skipUnless(DEPS_AVAILABLE, "Backfill dependencies are not installed")
class PacerTests(unittest.TestCase):
    def test_requests_are_paced_globally(self):
        pacer = fingerprint_backfill.Pacer(rps=1000)
        slept = []
        with patch.object(fingerprint_backfill.time, "sleep", slept.append):
            for _ in range(5):
                pacer.wait()
        # Five slots at 1ms apart: the pacer hands out increasing times rather
        # than letting every worker fire at once.
        self.assertGreaterEqual(pacer._next_slot, 0.004)

    def test_the_default_rate_is_polite(self):
        self.assertLessEqual(fingerprint_backfill.DEFAULT_RPS, 5.0)


if __name__ == "__main__":
    unittest.main()
