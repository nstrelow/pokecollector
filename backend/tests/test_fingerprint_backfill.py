import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import httpx
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


class _VirtualTime:
    """Stands in for the `time` module inside fingerprint_backfill.

    Only advances when something sleeps, so a test can pin how long a run
    "took" without actually taking that long, and without patching the real
    stdlib clock out from under the rest of the process.
    """

    def __init__(self, start=1000.0):
        self._now = start
        self._lock = __import__("threading").Lock()

    def monotonic(self):
        return self._now

    def sleep(self, seconds):
        with self._lock:
            self._now += max(0.0, seconds)


class _FrozenTime:
    """A clock that never moves, so sleeping is free and slot handout is exact."""

    def __init__(self, at=1000.0):
        self._at = at

    def monotonic(self):
        return self._at

    def sleep(self, seconds):
        pass


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

    def _url(self, card_id):
        return f"https://example.invalid/{card_id}.png"

    def _add(self, card_id, **kwargs):
        values = dict(
            id=card_id, tcg_card_id=card_id, name=card_id, number="1",
            set_id="sv1", lang="en", is_custom=False, is_digital=False,
            images_small=self._url(card_id),
            image_phash=None,
        )
        values.update(kwargs)
        self.db.add(Card(**values))
        self.db.commit()

    def _run(self, responses, **kwargs):
        """Run a backfill with the network replaced by a lookup table."""
        def fake_download(client, url, pacer, attempts=4):
            return responses[url]

        kwargs.setdefault("shuffle", False)
        with patch.object(fingerprint_backfill, "download_image", fake_download):
            return fingerprint_backfill.run_backfill(
                self.db, rps=1000, workers=2, **kwargs
            )

    def _card(self, card_id="a_en"):
        return self.db.query(Card).filter(Card.id == card_id).one()

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

    def test_the_queue_is_ordered_randomly_so_a_bad_block_cannot_hold_it_up(self):
        """Ordering by id is how the job starved.

        A block of low-id cards that fails transiently every run sits at the
        head of the same window every hour and nothing behind it is ever
        reached. Random order gives every pending row the same chance.
        """
        for i in range(60):
            self._add(f"sv1-{i:03d}_en")

        windows = [
            [row.id for row in fingerprint_backfill.pending_cards(self.db, limit=10)]
            for _ in range(6)
        ]
        in_order = [f"sv1-{i:03d}_en" for i in range(10)]
        self.assertNotEqual(windows, [in_order] * 6,
                            "the window is still the first 10 by id every time")
        # Different runs reach different cards.
        self.assertGreater(len({card for window in windows for card in window}), 10)

        # And the deterministic sweep is still available for the CLI.
        self.assertEqual(
            [row.id for row in fingerprint_backfill.pending_cards(
                self.db, limit=5, shuffle=False)],
            [f"sv1-{i:03d}_en" for i in range(5)],
        )

    def test_cards_that_already_have_a_fingerprint_are_skipped(self):
        self._add("done_en", image_phash=b"\x01" * 8,
                  image_phash_source=self._url("done_en"))
        self._add("todo_en")
        self.assertEqual(
            [row.id for row in fingerprint_backfill.pending_cards(self.db)],
            ["todo_en"],
        )
        self.assertEqual(
            len(fingerprint_backfill.pending_cards(self.db, refresh=True)), 2
        )

    # --- staleness is a property of the row, not of the writers -------------

    def test_a_fingerprint_from_a_different_url_is_queued_for_recomputation(self):
        """The corruption item 1 describes, and why it is now self-healing.

        Three catalogue writers rotated images_small field-by-field without
        clearing image_phash. Because the queue only ever selected
        "image_phash IS NULL", the survivor was never recomputed and went on
        matching photos of the OLD artwork at distance 0 forever. Comparing the
        stored provenance against the current URL makes the row itself say it is
        stale, so no writer has to remember.
        """
        self._add("rotated_en", image_phash=b"\x01" * 8,
                  image_phash_source="https://example.invalid/OLD.png")
        self._add("fresh_en", image_phash=b"\x02" * 8,
                  image_phash_source=self._url("fresh_en"))

        pending = {row.id for row in fingerprint_backfill.pending_cards(self.db)}
        self.assertEqual(pending, {"rotated_en"})

    def test_a_row_predating_the_provenance_column_is_queued(self):
        """image_phash_source IS NULL means "never attempted", not "clean"."""
        self._add("legacy_en", image_phash=b"\x01" * 8, image_phash_source=None)
        self.assertEqual(
            [row.id for row in fingerprint_backfill.pending_cards(self.db)],
            ["legacy_en"],
        )

    def test_a_stored_fingerprint_records_the_url_it_came_from(self):
        self._add("a_en")
        self._run({self._url("a_en"): fingerprint_backfill.Download(_png(1))})
        card = self._card()
        self.assertIsNotNone(card.image_phash)
        self.assertEqual(card.image_phash_source, self._url("a_en"))
        # And is therefore not pending any more.
        self.assertEqual(fingerprint_backfill.pending_cards(self.db), [])

    # --- negative caching ---------------------------------------------------

    def test_an_undecodable_render_is_not_retried_every_single_run(self):
        """The starvation item 4 describes.

        fingerprint_reference returns None for a corrupt render, nothing was
        written, and the card reappeared at the head of the very same window an
        hour later -- forever, re-downloading the identical bytes.
        """
        self._add("broken_en")
        responses = {
            self._url("broken_en"): fingerprint_backfill.Download(b"not an image"),
        }
        result = self._run(responses)

        self.assertEqual(result["stored"], 0)
        self.assertEqual(result["skipped"], 1)
        card = self._card("broken_en")
        self.assertIsNone(card.image_phash)
        self.assertEqual(card.image_phash_source, self._url("broken_en"))
        self.assertEqual(fingerprint_backfill.pending_cards(self.db), [])
        # But an explicit refresh still retries it, and so does a new URL.
        self.assertEqual(
            len(fingerprint_backfill.pending_cards(self.db, refresh=True)), 1
        )
        self.db.query(Card).update({"images_small": "https://example.invalid/new.png"})
        self.db.commit()
        self.assertEqual(
            [row.id for row in fingerprint_backfill.pending_cards(self.db)],
            ["broken_en"],
        )

    def test_an_unreachable_cdn_is_retried_rather_than_written_off(self):
        """A timeout says nothing about the URL, so it must not be cached.

        Otherwise one CDN outage permanently blacklists everything the run
        touched.
        """
        self._add("a_en")
        self._run({self._url("a_en"): fingerprint_backfill.Download(None)})
        card = self._card()
        self.assertIsNone(card.image_phash)
        self.assertIsNone(card.image_phash_source)
        self.assertEqual(
            [row.id for row in fingerprint_backfill.pending_cards(self.db)], ["a_en"]
        )

    # --- storing ------------------------------------------------------------

    def test_a_successful_run_stores_hashes_and_signals_other_processes(self):
        self._add("a_en")
        self._add("b_en")
        responses = {
            self._url("a_en"): fingerprint_backfill.Download(_png(1)),
            self._url("b_en"): fingerprint_backfill.Download(_png(2)),
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
        self._add("a_en", image_phash=b"\x07" * 8,
                  image_phash_source=self._url("a_en"))
        responses = {self._url("a_en"): fingerprint_backfill.Download(None)}
        result = self._run(responses, refresh=True)

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["cleared"], 0)
        self.assertEqual(self._card().image_phash, b"\x07" * 8)

    def test_a_refresh_clears_the_fingerprint_of_artwork_that_is_gone(self):
        """A hash pointing at an image that no longer exists is worse than none.

        Left in place it keeps competing for rank 1 against every real photo,
        and nothing would ever remove it.
        """
        self._add("a_en", image_phash=b"\x07" * 8,
                  image_phash_source=self._url("a_en"))
        responses = {
            self._url("a_en"): fingerprint_backfill.Download(None, absent=True),
        }
        result = self._run(responses, refresh=True)

        self.assertEqual(result["cleared"], 1)
        self.assertIsNone(self._card().image_phash)

    def test_only_a_refresh_may_clear_a_fingerprint_for_a_missing_image(self):
        """`refresh` is the whole authority to delete, not decoration.

        Outside a refresh the run is additive: a 404 records that the URL is
        dead so it is not fetched hourly, but it may not take an existing hash
        with it.
        """
        self._add("a_en", image_phash=b"\x07" * 8,
                  image_phash_source="https://example.invalid/OLD.png")
        responses = {
            self._url("a_en"): fingerprint_backfill.Download(None, absent=True),
        }
        result = self._run(responses, refresh=False)

        self.assertEqual(result["cleared"], 0)
        self.assertEqual(result["skipped"], 1)
        card = self._card()
        self.assertEqual(card.image_phash, b"\x07" * 8)
        self.assertEqual(card.image_phash_source, self._url("a_en"))

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
            responses[self._url(f"ph-{i:03d}_en")] = \
                fingerprint_backfill.Download(placeholder)
        self._add("real_en")
        responses[self._url("real_en")] = fingerprint_backfill.Download(_png(5))

        result = self._run(responses)

        self.assertEqual(result["placeholders"], count)
        self.assertEqual(result["stored"], 1)
        remaining = {
            card.id for card in self.db.query(Card)
            if card.image_phash is not None
        }
        self.assertEqual(remaining, {"real_en"})

    def test_exactly_the_repeat_threshold_is_still_treated_as_real_artwork(self):
        """The boundary is a real decision: PLACEHOLDER_REPEATS copies are kept.

        Nine identical renders is a placeholder; eight is the largest run of
        genuinely identical artwork the catalogue is allowed to contain, and
        discarding it would silently delete real coverage.
        """
        count = fingerprint_backfill.PLACEHOLDER_REPEATS
        shared = _png(77)
        responses = {}
        for i in range(count):
            self._add(f"dup-{i:03d}_en")
            responses[self._url(f"dup-{i:03d}_en")] = \
                fingerprint_backfill.Download(shared)

        result = self._run(responses)
        self.assertEqual(result["placeholders"], 0)
        self.assertEqual(result["stored"], count)
        self.assertEqual(
            self.db.query(Card).filter(Card.image_phash.isnot(None)).count(), count
        )

    def test_a_discarded_placeholder_is_not_downloaded_again_next_hour(self):
        count = fingerprint_backfill.PLACEHOLDER_REPEATS + 2
        placeholder = _png(42)
        responses = {}
        for i in range(count):
            self._add(f"ph-{i:03d}_en")
            responses[self._url(f"ph-{i:03d}_en")] = \
                fingerprint_backfill.Download(placeholder)

        self._run(responses)
        self.assertEqual(fingerprint_backfill.pending_cards(self.db), [])
        self.assertEqual(
            self.db.query(Card).filter(Card.image_phash_source.isnot(None)).count(),
            count,
        )

    def test_nothing_to_do_is_not_an_error(self):
        result = fingerprint_backfill.run_backfill(self.db)
        self.assertEqual(result["considered"], 0)
        self.assertEqual(result["stored"], 0)

    # --- bounded runs -------------------------------------------------------

    def test_a_run_stops_at_its_wall_clock_budget(self):
        """One "hourly" batch must not run for seventeen hours.

        download_image is ~127s worst case per card, so 2,000 cards over 4
        workers outlasts the interval many times over, and APScheduler's
        coalesce=True then throws the skipped runs away silently.
        """
        for i in range(40):
            self._add(f"sv1-{i:03d}_en")

        clock = _VirtualTime()
        attempted = []

        def slow_download(client, url, pacer, attempts=4):
            attempted.append(url)
            clock.sleep(5.0)  # each download "takes" five seconds
            return fingerprint_backfill.Download(_png(len(attempted)))

        with patch.object(fingerprint_backfill, "download_image", slow_download), \
             patch.object(fingerprint_backfill, "time", clock):
            result = fingerprint_backfill.run_backfill(
                self.db, rps=1000, workers=1, shuffle=False, time_budget=30.0
            )

        self.assertEqual(result["considered"], 40)
        self.assertTrue(result["stopped_early"])
        self.assertIn("time budget", result["stopped_early"])
        self.assertLess(len(attempted), 40)
        # Whatever it did manage is committed, not thrown away.
        self.assertEqual(
            self.db.query(Card).filter(Card.image_phash.isnot(None)).count(),
            result["stored"],
        )
        self.assertGreater(result["stored"], 0)
        # And the rest is simply still pending for the next run.
        self.assertEqual(
            len(fingerprint_backfill.pending_cards(self.db)), 40 - result["stored"]
        )

    def test_a_run_gives_up_when_the_cdn_is_plainly_not_answering(self):
        """A circuit breaker, so a dead CDN costs one sample and not a whole hour."""
        total = fingerprint_backfill.BREAKER_MIN_ATTEMPTS * 3
        for i in range(total):
            self._add(f"sv1-{i:03d}_en")
        attempted = []

        def dead(client, url, pacer, attempts=4):
            attempted.append(url)
            return fingerprint_backfill.Download(None)

        with patch.object(fingerprint_backfill, "download_image", dead):
            result = fingerprint_backfill.run_backfill(
                self.db, rps=1000, workers=1, shuffle=False
            )

        self.assertTrue(result["stopped_early"])
        self.assertIn("failure rate", result["stopped_early"])
        self.assertLess(len(attempted), total)
        self.assertLess(
            len(attempted), fingerprint_backfill.BREAKER_MIN_ATTEMPTS * 2
        )

    def test_a_healthy_run_is_never_cut_short_by_the_breaker(self):
        total = fingerprint_backfill.BREAKER_MIN_ATTEMPTS * 2
        responses = {}
        for i in range(total):
            self._add(f"sv1-{i:03d}_en")
            responses[self._url(f"sv1-{i:03d}_en")] = \
                fingerprint_backfill.Download(_png(i))

        result = self._run(responses)
        self.assertIsNone(result["stopped_early"])
        self.assertEqual(result["stored"], total)

    def test_the_read_transaction_is_closed_before_the_downloads_start(self):
        """The SELECT used to stay open for the whole run.

        flush() only fired every COMMIT_BATCH *pending* rows and failures never
        became pending, so a mostly-failing run held one pooled connection idle
        in transaction for hours, pinning Postgres's xmin horizon and blocking
        autovacuum on `cards`.
        """
        for i in range(3):
            self._add(f"sv1-{i:03d}_en")

        in_transaction = []

        def observe(client, url, pacer, attempts=4):
            in_transaction.append(self.db.in_transaction())
            return fingerprint_backfill.Download(None)

        with patch.object(fingerprint_backfill, "download_image", observe):
            fingerprint_backfill.run_backfill(
                self.db, rps=1000, workers=1, shuffle=False
            )
        self.assertTrue(in_transaction)
        self.assertEqual(in_transaction, [False] * len(in_transaction))

    def test_a_slow_run_commits_on_the_clock_and_not_only_on_the_count(self):
        """Waiting for 200 pending rows can mean waiting for hours.

        COMMIT_BATCH counts *pending* rows and failures never became pending,
        so a slow or mostly-failing run held one pooled connection open with
        uncommitted work for the whole batch. Time has to be able to trigger a
        commit too.
        """
        clock = _VirtualTime()
        for i in range(6):
            self._add(f"sv1-{i:03d}_en")

        def slow_download(client, url, pacer, attempts=4):
            clock.sleep(fingerprint_backfill.MAX_FLUSH_SECONDS / 2 + 1)
            return fingerprint_backfill.Download(_png(hash(url) % 1000))

        batches = []
        real_store = fingerprint_backfill._store

        def counting_store(db, updates):
            batches.append(len(updates))
            return real_store(db, updates)

        with patch.object(fingerprint_backfill, "download_image", slow_download), \
             patch.object(fingerprint_backfill, "_store", counting_store), \
             patch.object(fingerprint_backfill, "time", clock):
            result = fingerprint_backfill.run_backfill(
                self.db, rps=1000, workers=1, shuffle=False,
                time_budget=float("inf"),
            )

        self.assertEqual(result["stored"], 6)
        self.assertGreater(
            len(batches), 1,
            "everything was held back to a single commit at the end",
        )
        self.assertLess(batches[0], 6, "the first commit covered the whole run")

    def test_a_batch_is_stored_in_a_handful_of_statements_not_one_per_card(self):
        """2,000 round trips per scheduled run was the old cost."""
        total = 40
        responses = {}
        for i in range(total):
            self._add(f"sv1-{i:03d}_en")
            responses[self._url(f"sv1-{i:03d}_en")] = \
                fingerprint_backfill.Download(_png(i))

        updates = []

        def record(conn, cursor, statement, parameters, *rest):
            if statement.lstrip().upper().startswith("UPDATE CARDS"):
                updates.append(statement)

        event.listen(self.engine, "before_cursor_execute", record)
        try:
            result = self._run(responses)
        finally:
            event.remove(self.engine, "before_cursor_execute", record)

        self.assertEqual(result["stored"], total)
        self.assertLess(len(updates), total // 4)

    # --- download rules -----------------------------------------------------

    def _response(self, status, body=b"", headers=None):
        return httpx.Response(
            status, content=body, headers=headers or {},
            request=httpx.Request("GET", "https://example.invalid/x.png"),
        )

    def _download(self, responses, attempts=4):
        calls = []

        class FakeClient:
            def get(inner, url, timeout=None):
                calls.append(url)
                return responses[min(len(calls) - 1, len(responses) - 1)]

        with patch.object(fingerprint_backfill, "time", _VirtualTime()):
            result = fingerprint_backfill.download_image(
                FakeClient(), "https://example.invalid/x.png",
                fingerprint_backfill.Pacer(1000), attempts=attempts,
            )
        return result, calls

    def test_every_status_that_means_gone_is_treated_as_gone(self):
        """404 is not the only definite "no". A permissions or bad-request

        answer from the CDN is equally never going to become an image, and
        retrying it four times an hour forever is pure noise. Each of these is
        also licence to clear a stale fingerprint in a refresh, so narrowing the
        set silently changes what may be deleted.
        """
        for status in (400, 401, 403, 404, 410):
            with self.subTest(status=status):
                result, calls = self._download([self._response(status)])
                self.assertTrue(result.absent, f"{status} should mean absent")
                self.assertEqual(len(calls), 1, "a definite answer is not retried")

    def test_a_server_error_is_retried_and_never_reported_as_gone(self):
        result, calls = self._download([self._response(500)], attempts=3)
        self.assertFalse(result.absent)
        self.assertIsNone(result.content)
        self.assertEqual(len(calls), 3)

    def test_a_successful_download_returns_its_bytes(self):
        result, calls = self._download([self._response(200, b"imagebytes")])
        self.assertEqual(result.content, b"imagebytes")
        self.assertFalse(result.absent)
        self.assertEqual(len(calls), 1)


@unittest.skipUnless(DEPS_AVAILABLE, "Backfill dependencies are not installed")
class PacerTests(unittest.TestCase):
    """The pacer is the only thing standing between this app and hammering a
    free community CDN, so the tests have to pin the actual rate.

    The previous test asserted `pacer._next_slot >= 0.004`, which is trivially
    true: _next_slot comes from time.monotonic(), around 10^5 on any running
    system. Setting Pacer.interval = 0.0 -- no pacing whatsoever -- passed it.
    """

    def _elapsed_for(self, rps, calls):
        clock = _VirtualTime()
        pacer = fingerprint_backfill.Pacer(rps=rps)
        start = clock.monotonic()
        with patch.object(fingerprint_backfill, "time", clock):
            for _ in range(calls):
                pacer.wait()
        return clock.monotonic() - start

    def test_the_request_rate_is_what_it_says_it_is(self):
        """Wall-clock time actually spent, not "the counter went up".

        This is the assertion Pacer.interval = 0.0 has to fail: with no pacing
        the loop sleeps for nothing at all and elapsed stays 0.
        """
        for rps, calls in ((5.0, 20), (2.0, 10), (10.0, 50)):
            with self.subTest(rps=rps):
                elapsed = self._elapsed_for(rps, calls)
                # The first slot is free; the remaining N-1 are spaced 1/rps.
                self.assertAlmostEqual(elapsed, (calls - 1) / rps, places=6)

    def test_a_slower_rate_takes_proportionally_longer(self):
        fast = self._elapsed_for(10.0, 21)
        slow = self._elapsed_for(2.5, 21)
        self.assertAlmostEqual(slow / fast, 4.0, places=6)

    def test_pacing_cannot_be_switched_off(self):
        """A zero interval is not a fast pacer, it is no pacer."""
        self.assertGreater(fingerprint_backfill.Pacer(rps=5.0).interval, 0.0)
        # Even an absurd rate keeps a positive floor rather than free-running.
        self.assertGreater(fingerprint_backfill.Pacer(rps=10 ** 9).interval, 0.0)
        self.assertGreater(fingerprint_backfill.Pacer(rps=0.0).interval, 0.0)

    def test_slots_are_shared_across_worker_threads(self):
        """Four workers must issue rps requests a second between them, not 4x it.

        Asserted on the slot ledger rather than on elapsed time, because the
        slot handout is the part that is actually shared: whichever thread wins
        the lock, the Nth request in the whole run is scheduled N intervals
        after the first.
        """
        import threading

        clock = _FrozenTime()
        pacer = fingerprint_backfill.Pacer(rps=4.0)
        with patch.object(fingerprint_backfill, "time", clock):
            threads = [
                threading.Thread(target=lambda: [pacer.wait() for _ in range(5)])
                for _ in range(4)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        # 20 requests through one shared pacer at 4/s covers 20 slots of 0.25s.
        # Four independent pacers would have handed out 5 slots each and left
        # this at 1.25s.
        self.assertAlmostEqual(
            pacer._next_slot - clock.monotonic(), 20 / 4.0, places=6
        )

    def test_the_default_rate_is_polite(self):
        self.assertLessEqual(fingerprint_backfill.DEFAULT_RPS, 5.0)
        self.assertGreater(fingerprint_backfill.DEFAULT_RPS, 0.0)


if __name__ == "__main__":
    unittest.main()
