"""The optional dense-embedding path: off by default, honest when it is on.

No onnxruntime anywhere in here. The module is an optional extra, and a test
suite that needed it installed would be testing a different installation from
the one most people run -- so the session is faked, which also makes every
assertion about the surrounding machinery rather than about a model's opinion.
"""
import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import numpy as np
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api.auth import get_current_user
    from api.recognize_local import router
    from database import Base, get_db
    from models import Card, Setting, User
    from services import card_embedding, fingerprint_index
    from services.card_fingerprint import fingerprint_reference, fuse_similarity, rank_fused

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False

HIDDEN = 8
# 224*224 must divide by this for the fake's reshape; 8 does, 5 does not.
TOKENS = 8


class _FakeSession:
    """A stand-in ONNX session whose output is a function of the pixels.

    Deterministic and continuous: two similar images give two similar vectors,
    which is the only property anything under test relies on.
    """

    def __init__(self, hidden=HIDDEN, tokens=TOKENS):
        self.hidden = hidden
        self.tokens = tokens
        self.calls = 0

    def run(self, _outputs, feeds):
        self.calls += 1
        pixels = feeds["pixel_values"]
        assert pixels.shape == (1, 3, 224, 224), pixels.shape
        blocks = pixels.reshape(1, 3, self.tokens, -1).mean(axis=3)[0]
        out = np.zeros((1, self.tokens, self.hidden), dtype=np.float32)
        for token in range(self.tokens):
            for dim in range(self.hidden):
                out[0, token, dim] = blocks[dim % 3, token] * (1 + dim * 0.1)
        return [out]


def _image(seed, size=(245, 337)):
    rng = np.random.default_rng(seed)
    width, height = size
    rows, cols = np.mgrid[0:height, 0:width]
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[..., 0] = ((cols * 3 + rows * 2 + seed * 37) % 256).astype(np.uint8)
    pixels[..., 1] = ((rows * 5 + seed * 11) % 256).astype(np.uint8)
    pixels[..., 2] = ((cols * cols // 5 + seed * 53) % 256).astype(np.uint8)
    for _ in range(5):
        cy, cx = int(rng.integers(0, height)), int(rng.integers(0, width))
        radius = int(rng.integers(25, 70))
        inside = (rows - cy) ** 2 + (cols - cx) ** 2 <= radius * radius
        pixels[inside] = int(rng.integers(0, 256))
    return Image.fromarray(pixels)


async def _noop():
    """An awaitable for patched fire-and-forget coroutines."""
    return 0


def _jpeg(image):
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=92)
    return buf.getvalue()


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class OptionalityTests(unittest.TestCase):
    """An installation that did not opt in must not notice this module exists."""

    def setUp(self):
        card_embedding.reset()
        self.addCleanup(card_embedding.reset)

    def test_no_model_configured_means_unavailable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(card_embedding.MODEL_PATH_ENV, None)
            self.assertFalse(card_embedding.available())
            self.assertIsNone(card_embedding.embed_reference(_jpeg(_image(1))))
            self.assertEqual(card_embedding.embed_photo_views(_jpeg(_image(1))), [])

    def test_a_missing_model_file_is_a_warning_not_a_crash(self):
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/nope/x.onnx"}):
            self.assertFalse(card_embedding.available())

    def test_onnxruntime_is_never_imported_when_no_model_is_configured(self):
        """The whole point of the extra is that it stays uninstalled."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(card_embedding.MODEL_PATH_ENV, None)
            with patch.dict(sys.modules, {"onnxruntime": None}):
                self.assertFalse(card_embedding.available())

    def test_a_hard_failure_is_not_retried_on_every_card(self):
        """45,000 rows must not each pay for the same missing file."""
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/nope/x.onnx"}):
            with patch("os.path.isfile", return_value=False) as isfile:
                for _ in range(5):
                    card_embedding.available()
                self.assertLessEqual(isfile.call_count, 1)


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class ProvenanceTests(unittest.TestCase):
    """image_embedding_source has to catch a changed model, not just a URL."""

    def setUp(self):
        card_embedding.reset()
        self.addCleanup(card_embedding.reset)

    def test_the_marker_changes_with_the_artwork(self):
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}):
            self.assertNotEqual(
                card_embedding.source_marker("https://x/one.png"),
                card_embedding.source_marker("https://x/two.png"),
            )

    def test_the_marker_changes_with_the_model(self):
        """A different model's vectors are meaningless against the stored ones.

        They are the same shape and the same dtype, so nothing downstream can
        notice; the only place it can be caught is here.
        """
        url = "https://x/one.png"
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}):
            first = card_embedding.source_marker(url)
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/b.onnx"}):
            second = card_embedding.source_marker(url)
        self.assertNotEqual(first, second)

    def test_the_marker_still_contains_the_url_for_sql_to_compare(self):
        """`stale_embedding_filter` is one expression only because of this."""
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}):
            marker = card_embedding.source_marker("https://x/one.png")
        self.assertTrue(marker.endswith(
            card_embedding.MARKER_SEPARATOR + "https://x/one.png"
        ))


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class VectorStorageTests(unittest.TestCase):
    def test_a_vector_survives_a_round_trip(self):
        vector = np.linspace(-1, 1, 16).astype(np.float32)
        restored = card_embedding.unpack(card_embedding.pack(vector))
        np.testing.assert_allclose(restored, vector, atol=1e-3)

    def test_float16_is_the_stored_width(self):
        """32MB against 65MB at catalogue scale, for 0.03 points of recall."""
        self.assertEqual(len(card_embedding.pack(np.zeros(256, np.float32))), 512)

    def test_a_vector_of_the_wrong_length_is_refused(self):
        blob = card_embedding.pack(np.zeros(16, np.float32))
        self.assertIsNotNone(card_embedding.unpack(blob, 16))
        self.assertIsNone(card_embedding.unpack(blob, 32))

    def test_junk_is_refused_rather_than_reshaped(self):
        for blob in (None, b"", b"abc", b"x"):
            self.assertIsNone(card_embedding.unpack(blob))


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class FusionTests(unittest.TestCase):
    """Combining a similarity and a distance without inventing a weight."""

    def test_both_signals_move_the_result(self):
        similarity = np.array([0.9, 0.5, 0.1], dtype=np.float32)
        distances = np.array([10, 2, 30], dtype=np.int64)
        fused = fuse_similarity(similarity, distances)
        # Row 1 loses on the embedding and wins on the hash; row 0 the reverse.
        self.assertGreater(fused[1], fused[2])
        self.assertGreater(fused[0], fused[2])

    def test_a_row_with_no_embedding_competes_on_its_hash_alone(self):
        """Not on a zero vector it never earned, which would bury it."""
        similarity = np.array([0.9, 0.0, 0.8], dtype=np.float32)
        distances = np.array([20, 0, 20], dtype=np.int64)
        embedded = np.array([True, False, True])
        fused = fuse_similarity(similarity, distances, embedded)
        self.assertGreater(fused[1], fused[0])
        self.assertGreater(fused[1], fused[2])

    def test_a_flat_signal_does_not_divide_by_zero(self):
        fused = fuse_similarity(
            np.ones(4, np.float32), np.zeros(4, np.int64)
        )
        self.assertTrue(np.all(np.isfinite(fused)))

    def test_the_shortlist_still_reports_hamming_distances(self):
        """The confidence label and the percentage are defined on bits.

        A fused score has no bit meaning, so it decides the ORDER and nothing
        else; everything downstream still reads the distance it always did.
        """
        scores = np.array([0.1, 0.9, 0.5], dtype=np.float64)
        distances = np.array([14, 2, 8], dtype=np.int64)
        self.assertEqual(rank_fused(scores, distances, limit=3),
                         [(1, 2), (2, 8), (0, 14)])

    def test_the_distance_cutoff_still_applies(self):
        scores = np.array([0.9, 0.5], dtype=np.float64)
        distances = np.array([40, 2], dtype=np.int64)
        self.assertEqual(
            rank_fused(scores, distances, limit=2, max_distance=24), [(1, 2)]
        )

    def test_ties_are_broken_by_row_so_ranking_is_reproducible(self):
        scores = np.array([0.5, 0.5, 0.5], dtype=np.float64)
        distances = np.array([4, 4, 4], dtype=np.int64)
        self.assertEqual([row for row, _ in rank_fused(scores, distances, limit=3)],
                         [0, 1, 2])


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class EmbeddedIndexTests(unittest.TestCase):
    """The index only uses embeddings when using them is an improvement."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([
            Setting(key="tcgdex_sync_languages", value="en"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
        ])
        self.db.commit()
        fingerprint_index.reset()
        card_embedding.reset()
        self.addCleanup(self.db.close)
        self.addCleanup(fingerprint_index.reset)
        self.addCleanup(card_embedding.reset)

    def _card(self, card_id, seed, embedding=True, dims=16):
        vector = np.full(dims, float(seed) / 100.0, dtype=np.float32)
        vector[seed % dims] += 1.0
        self.db.add(Card(
            id=card_id, tcg_card_id=card_id, name=card_id, number="1",
            set_id="sv1", lang="en", is_custom=False, is_digital=False,
            images_small=f"https://example.invalid/{card_id}.png",
            image_phash=fingerprint_reference(_jpeg(_image(seed))),
            image_embedding=card_embedding.pack(vector) if embedding else None,
        ))

    def _load(self, embedded_rows, total=400, dims=16):
        for i in range(total):
            self._card(f"c{i:04d}", i, embedding=i < embedded_rows, dims=dims)
        self.db.commit()
        with patch.object(card_embedding, "available", return_value=True):
            return fingerprint_index.get(self.db)

    def test_embeddings_are_ignored_when_the_model_is_not_configured(self):
        for i in range(400):
            self._card(f"c{i:04d}", i)
        self.db.commit()
        snapshot = fingerprint_index.get(self.db)
        self.assertIsNone(snapshot.embeddings)

    def test_too_few_embedded_cards_is_not_an_index(self):
        """Whitening fitted on a handful of rows is noise with a matrix shape."""
        snapshot = self._load(embedded_rows=fingerprint_index.MIN_EMBEDDED_CARDS - 1)
        self.assertIsNone(snapshot.embeddings)

    def test_a_mostly_embedded_catalogue_builds_the_accurate_index(self):
        snapshot = self._load(embedded_rows=400)
        self.assertIsNotNone(snapshot.embeddings)
        self.assertEqual(snapshot.embeddings.shape[0], len(snapshot.rows))
        self.assertTrue(snapshot.embedded.all())

    def test_rows_the_backfill_has_not_reached_are_marked_not_buried(self):
        """A half-built embedding index must not hide every unembedded row."""
        snapshot = self._load(embedded_rows=300, total=400)
        self.assertIsNotNone(snapshot.embeddings)
        self.assertEqual(int(snapshot.embedded.sum()), 300)
        self.assertEqual(len(snapshot.embedded), len(snapshot.rows))

    def test_whitening_reduces_the_stored_width(self):
        snapshot = self._load(embedded_rows=400, dims=64)
        self.assertEqual(
            snapshot.embeddings.shape[1],
            min(fingerprint_index.WHITENING_DIMS, 64),
        )

    def test_an_embedding_from_another_model_is_skipped_not_reshaped(self):
        """Different length, same dtype: nothing downstream could notice."""
        for i in range(400):
            self._card(f"c{i:04d}", i, dims=16)
        self.db.add(Card(
            id="oddball", tcg_card_id="oddball", name="odd", number="1",
            set_id="sv1", lang="en", is_custom=False, is_digital=False,
            images_small="https://example.invalid/odd.png",
            image_phash=fingerprint_reference(_jpeg(_image(999))),
            image_embedding=card_embedding.pack(np.zeros(32, np.float32)),
        ))
        self.db.commit()
        with patch.object(card_embedding, "available", return_value=True):
            snapshot = fingerprint_index.get(self.db)
        odd = [i for i, row in enumerate(snapshot.rows) if row["id"] == "oddball"]
        self.assertEqual(len(odd), 1, "the row itself still belongs in the index")
        self.assertFalse(bool(snapshot.embedded[odd[0]]))


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class EmbeddedEndpointTests(unittest.TestCase):
    """What the scan endpoint does once the accurate path is live."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="e", hashed_password="x", is_active=True)
        self.db.add_all([
            self.user,
            Setting(key="tcgdex_sync_languages", value="en"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
        ])
        self.db.commit()
        fingerprint_index.reset()
        card_embedding.reset()

        app = FastAPI()
        app.include_router(router, prefix="/api/cards")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

        self.session = _FakeSession()
        self._patches = [
            patch.object(card_embedding, "_load_session", lambda: self.session),
            patch.object(fingerprint_index, "READY_MIN_CARDS", 1),
            patch.object(fingerprint_index, "MIN_EMBEDDED_CARDS", 4),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        self.addCleanup(self.client.close)
        self.addCleanup(self.db.close)
        self.addCleanup(fingerprint_index.reset)
        self.addCleanup(card_embedding.reset)

    def _add(self, card_id, image):
        self.db.add(Card(
            id=card_id, tcg_card_id=card_id.split("_")[0], name=card_id,
            number="1", set_id="sv1", lang=card_id.split("_")[-1],
            is_custom=False, is_digital=False,
            images_small=f"https://example.invalid/{card_id}.png",
            image_phash=fingerprint_reference(_jpeg(image)),
            image_embedding=card_embedding.embed_reference(_jpeg(image)),
        ))
        self.db.commit()

    def _post(self, image):
        return self.client.post(
            "/api/cards/recognize/local",
            files={"file": ("p.jpg", _jpeg(image), "image/jpeg")},
        )

    def test_the_accurate_path_still_finds_the_right_card(self):
        target = _image(7)
        self._add("hit_en", target)
        for seed in range(20, 28):
            self._add(f"miss{seed}_en", _image(seed))
        body = self._post(target).json()
        self.assertEqual(body["matches"][0]["id"], "hit_en")

    def test_it_never_claims_confidence_on_an_uncalibrated_gate(self):
        """MIN_MARGIN is a number of bits, measured on a hash-ordered list.

        On a fused ranking the first two entries are not ordered by distance,
        so the difference between their distances means nothing. Until a margin
        on the fused score is derived, the answer is "here is a much better
        shortlist", not "I am sure".
        """
        target = _image(8)
        self._add("sure_en", target)
        for seed in range(30, 38):
            self._add(f"other{seed}_en", _image(seed))
        body = self._post(target).json()
        self.assertEqual(body["matches"][0]["id"], "sure_en")
        self.assertFalse(body["_identity_confident"])
        self.assertIsNone(body["_identity_decision"])

    def test_the_hash_path_still_claims_confidence(self):
        """The change above must not quietly disable the gate everywhere."""
        target = _image(9)
        for p in self._patches:
            p.stop()
        try:
            with patch.object(fingerprint_index, "READY_MIN_CARDS", 1):
                self._add("plain_en", target)
                for seed in range(40, 48):
                    self._add(f"o{seed}_en", _image(seed))
                fingerprint_index.reset()
                body = self._post(target).json()
        finally:
            for p in self._patches:
                p.start()
        self.assertTrue(body["_identity_confident"])

    def test_the_badge_is_calibrated_on_the_margin_not_the_distance(self):
        """A distance curve must not label a ranking that ignored distance.

        Live, the embedding found seven of ten cards at tile 1 and the badge
        said "1%" beside them, because `match_probability` reads a table indexed
        by Hamming distance and the embedding had picked tiles whose distance
        was 18 to 22. That is the defect this branch started with, inverted:
        understating a correct answer rather than overstating a wrong one, and
        just as corrosive -- a number that says 1% about the right card teaches
        the user to stop reading it.
        """
        target = _image(12)
        self._add("clear_en", target)
        for seed in range(70, 78):
            self._add(f"q{seed}_en", _image(seed))
        # The margin is between TILES, so there have to be two. These fixtures
        # are unrelated synthetic images, so most sit past the hash cutoff and
        # would be dropped, leaving a lone tile with nothing to measure against.
        with patch("api.recognize_local.SHORTLIST_MAX_DISTANCE", 64):
            matches = self._post(target).json()["matches"]
        self.assertEqual(matches[0]["id"], "clear_en")
        leader = matches[0]["_match_percent"]
        self.assertIsInstance(leader, int)
        self.assertLessEqual(leader, 99, "no measurement here supports certainty")
        for other in matches[1:]:
            if other["_match_percent"] is not None:
                self.assertLess(other["_match_percent"], leader)

    def test_a_fused_shortlist_does_not_claim_more_than_a_certainty(self):
        """Exactly one tile can be the right artwork, so they share 100%."""
        target = _image(14)
        self._add("share_en", target)
        for seed in range(120, 130):
            self._add(f"s{seed}_en", _image(seed))
        with patch("api.recognize_local.SHORTLIST_MAX_DISTANCE", 64):
            matches = self._post(target).json()["matches"]
        shown = [m["_match_percent"] for m in matches if m["_match_percent"] is not None]
        self.assertTrue(shown, "the leading tile must carry a number")
        self.assertLessEqual(sum(shown), 100)

    def test_a_lone_tile_carries_no_margin_and_so_no_number(self):
        """One tile is not a comparison, and the badge is calibrated on one."""
        target = _image(15)
        self._add("only_en", target)
        for seed in range(140, 148):
            self._add(f"o{seed}_en", _image(seed))
        matches = self._post(target).json()["matches"]
        self.assertEqual(len(matches), 1, "the cutoff should leave one tile here")
        self.assertIsNone(matches[0]["_match_percent"])

    def test_the_hash_path_still_prints_its_measured_percentage(self):
        """Going quiet on one path must not silence the calibrated one."""
        target = _image(13)
        for p in self._patches:
            p.stop()
        try:
            with patch.object(fingerprint_index, "READY_MIN_CARDS", 1):
                self._add("loud_en", target)
                for seed in range(80, 88):
                    self._add(f"l{seed}_en", _image(seed))
                fingerprint_index.reset()
                matches = self._post(target).json()["matches"]
        finally:
            for p in self._patches:
                p.start()
        self.assertEqual(matches[0]["id"], "loud_en")
        self.assertIsInstance(matches[0]["_match_percent"], int)
        self.assertEqual(matches[0]["_confidence"], "high")

    def test_both_the_crop_and_the_whole_frame_are_scored(self):
        """Worth +2.71 points of rank-1, and it rescues the badly framed ones."""
        target = _image(10)
        self._add("framed_en", target)
        for seed in range(50, 58):
            self._add(f"f{seed}_en", _image(seed))
        before = self.session.calls
        self._post(target)
        self.assertGreaterEqual(self.session.calls - before, 2)

    def test_a_photo_the_model_cannot_read_falls_back_to_the_hash(self):
        """Degrades to the old path rather than failing the scan -- but says so.

        The two rankings are 99.9% against 90.7% shortlist recall, and 9 of 10
        against 2 of 10 on real photographs. A model that quietly stopped
        loading would make the scanner much worse with nothing anywhere saying
        why, which is the failure this logs and reports rather than hides.
        """
        target = _image(11)
        self._add("fallback_en", target)
        for seed in range(60, 68):
            self._add(f"g{seed}_en", _image(seed))
        with patch.object(card_embedding, "embed_photo_views", return_value=[]):
            with self.assertLogs("api.recognize_local", level="WARNING") as logs:
                response = self._post(target)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["matches"][0]["id"], "fallback_en")
        self.assertEqual(body["_ranked_by"], "hash")
        self.assertTrue(any("perceptual hash alone" in line for line in logs.output))

    def test_the_answer_says_which_ranking_produced_it(self):
        target = _image(16)
        self._add("named_en", target)
        for seed in range(150, 158):
            self._add(f"n{seed}_en", _image(seed))
        self.assertEqual(self._post(target).json()["_ranked_by"], "embedding")

    def test_the_review_images_are_warmed_for_the_offline_shortlist_too(self):
        """This shortlist needs the warm cache more than the provider's does.

        It claims less and is built to be compared against the user's own photo
        in the full-screen viewer, so the candidate a reviewer opens IS the
        answer rather than a second opinion.
        """
        target = _image(17)
        self._add("warm_en", target)
        for seed in range(160, 168):
            self._add(f"w{seed}_en", _image(seed))
        with patch("api.recognize_local.prewarm_candidate_images") as prewarm:
            prewarm.return_value = _noop()
            self._post(target)
        self.assertEqual(prewarm.call_count, 1)
        warmed = prewarm.call_args[0][0]
        self.assertTrue(warmed and warmed[0]["id"] == "warm_en")


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class BackfillEmbeddingTests(unittest.TestCase):
    """The embedding rides along with the hash, on one download."""

    def setUp(self):
        from services import fingerprint_backfill

        self.backfill = fingerprint_backfill
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([
            Setting(key="tcgdex_sync_languages", value="en"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
        ])
        self.db.commit()
        card_embedding.reset()
        fingerprint_index.reset()
        self.addCleanup(self.db.close)
        self.addCleanup(card_embedding.reset)
        self.addCleanup(fingerprint_index.reset)

    def _card(self, card_id, **kwargs):
        values = dict(
            id=card_id, tcg_card_id=card_id, name=card_id, number="1",
            set_id="sv1", lang="en", is_custom=False, is_digital=False,
            images_small=f"https://example.invalid/{card_id}.png",
        )
        values.update(kwargs)
        self.db.add(Card(**values))
        self.db.commit()

    def test_nothing_is_pending_for_embedding_reasons_without_a_model(self):
        """An install that never opted in must not re-download its catalogue."""
        self._card(
            "done",
            image_phash=b"01234567",
            image_phash_source="https://example.invalid/done.png",
        )
        self.assertIsNone(self.backfill.stale_embedding_filter())
        self.assertEqual(self.backfill.pending_cards(self.db), [])

    def test_a_row_with_a_current_hash_is_still_pending_for_its_embedding(self):
        """Both derived values share one download; either being stale queues it."""
        url = "https://example.invalid/done.png"
        self._card("done", image_phash=b"01234567", image_phash_source=url)
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}):
            self.assertEqual(
                [row.id for row in self.backfill.pending_cards(self.db)], ["done"]
            )

    def test_a_row_embedded_by_this_model_is_not_pending(self):
        url = "https://example.invalid/done.png"
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}):
            self._card(
                "done", image_phash=b"01234567", image_phash_source=url,
                image_embedding=b"\x00\x00\x00\x00",
                image_embedding_source=card_embedding.source_marker(url),
            )
            self.assertEqual(self.backfill.pending_cards(self.db), [])

    def test_changing_the_model_re_queues_every_embedded_row(self):
        """The failure this exists to prevent is silent, not loud.

        A different model's vectors are the same shape and the same dtype, so a
        swapped model leaves an index that looks healthy and ranks nonsense.
        """
        url = "https://example.invalid/done.png"
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}):
            self._card(
                "done", image_phash=b"01234567", image_phash_source=url,
                image_embedding=b"\x00\x00\x00\x00",
                image_embedding_source=card_embedding.source_marker(url),
            )
            self.assertEqual(self.backfill.pending_cards(self.db), [])
        card_embedding.reset()
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/b.onnx"}):
            self.assertEqual(
                [row.id for row in self.backfill.pending_cards(self.db)], ["done"]
            )

    def test_a_rotated_artwork_re_queues_the_embedding_too(self):
        url = "https://example.invalid/done.png"
        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}):
            self._card(
                "done", image_phash=b"01234567", image_phash_source=url,
                image_embedding=b"\x00\x00\x00\x00",
                image_embedding_source=card_embedding.source_marker(url),
            )
            self.db.query(Card).filter(Card.id == "done").update(
                {"images_small": "https://example.invalid/new.png"}
            )
            self.db.commit()
            self.assertEqual(
                [row.id for row in self.backfill.pending_cards(self.db)], ["done"]
            )

    def test_one_download_writes_both_derived_values(self):
        session = _FakeSession()
        image = _jpeg(_image(3))
        self._card("card")
        rows = self.backfill.pending_cards(self.db)

        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}), \
                patch.object(card_embedding, "_load_session", lambda: session), \
                patch.object(
                    self.backfill, "download_image",
                    return_value=self.backfill.Download(image)
                ) as download:
            result = self.backfill.fingerprint_cards(self.db, rows, workers=1, rps=100)
            # Inside the patch: the marker is a function of the configured
            # model, so computing it outside would compare against a different
            # installation's answer.
            expected_marker = card_embedding.source_marker(
                "https://example.invalid/card.png"
            )

        self.assertEqual(result["stored"], 1)
        self.assertEqual(download.call_count, 1, "one picture, one request")
        card = self.db.query(Card).filter(Card.id == "card").one()
        self.assertIsNotNone(card.image_phash)
        self.assertIsNotNone(card.image_embedding)
        self.assertEqual(card.image_embedding_source, expected_marker)

    def test_an_install_with_no_model_stores_only_the_hash(self):
        image = _jpeg(_image(4))
        self._card("card")
        rows = self.backfill.pending_cards(self.db)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(card_embedding.MODEL_PATH_ENV, None)
            with patch.object(
                self.backfill, "download_image",
                return_value=self.backfill.Download(image)
            ):
                self.backfill.fingerprint_cards(self.db, rows, workers=1, rps=100)
        card = self.db.query(Card).filter(Card.id == "card").one()
        self.assertIsNotNone(card.image_phash)
        self.assertIsNone(card.image_embedding)
        self.assertIsNone(card.image_embedding_source)

    def test_a_placeholder_takes_its_embedding_out_with_its_hash(self):
        """The vector came from the same wrong bytes and is just as wrong."""
        session = _FakeSession()
        placeholder = _jpeg(_image(5))
        for i in range(self.backfill.PLACEHOLDER_REPEATS + 2):
            self._card(f"ph{i}")
        rows = self.backfill.pending_cards(self.db)

        with patch.dict(os.environ, {card_embedding.MODEL_PATH_ENV: "/m/a.onnx"}), \
                patch.object(card_embedding, "_load_session", lambda: session), \
                patch.object(
                    self.backfill, "download_image",
                    return_value=self.backfill.Download(placeholder)
                ):
            result = self.backfill.fingerprint_cards(
                self.db, rows, workers=1, rps=100
            )

        self.assertGreater(result["placeholders"], 0)
        for card in self.db.query(Card).all():
            self.assertIsNone(card.image_phash)
            self.assertIsNone(card.image_embedding)


if __name__ == "__main__":
    unittest.main()
