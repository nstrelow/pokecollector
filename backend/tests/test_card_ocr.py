"""Reading the printed collector number: off by default, additive when on.

No rapidocr and no OpenCV anywhere in here. The engine is faked, which keeps
the suite runnable on the installation most people have and makes every
assertion about the surrounding machinery rather than about a recogniser's
eyesight.
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
    from models import Card, Set, Setting, User
    from services import card_ocr, fingerprint_index
    from services.card_fingerprint import fingerprint_reference
    from services.card_visibility import indexable_card_filter

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


class _FakeEngine:
    """Stands in for RapidOCR: returns fixed text for every strip it is given."""

    def __init__(self, text=""):
        self.text = text
        self.calls = 0

    def __call__(self, array):
        self.calls += 1
        assert getattr(array, "ndim", 0) == 3, "the engine is handed an RGB array"
        boxes = [[None, part, 0.9] for part in self.text.split("|") if part]
        return boxes, None


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


def _jpeg(image):
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=92)
    return buf.getvalue()


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class OptionalityTests(unittest.TestCase):
    """An installation that did not opt in must not notice this exists."""

    def setUp(self):
        card_ocr.reset()
        self.addCleanup(card_ocr.reset)

    def test_off_unless_explicitly_enabled(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(card_ocr.OCR_ENABLED_ENV, None)
            self.assertFalse(card_ocr.enabled())
            self.assertFalse(card_ocr.available())
            self.assertEqual(card_ocr.read_printed_numbers(_jpeg(_image(1))), [])

    def test_the_usual_truthy_spellings_all_work(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with patch.dict(os.environ, {card_ocr.OCR_ENABLED_ENV: value}):
                self.assertTrue(card_ocr.enabled(), value)
        for value in ("0", "false", "no", "", "  "):
            with patch.dict(os.environ, {card_ocr.OCR_ENABLED_ENV: value}):
                self.assertFalse(card_ocr.enabled(), repr(value))

    def test_a_missing_package_is_reported_once_not_per_card(self):
        """45,000 rows must not each pay for the same failed import."""
        with patch.dict(os.environ, {card_ocr.OCR_ENABLED_ENV: "1"}):
            with patch.dict(sys.modules, {"rapidocr_onnxruntime": None}):
                for _ in range(5):
                    self.assertFalse(card_ocr.available())


class ThreadCapTests(unittest.TestCase):
    """Uncapped, onnxruntime floods the log with affinity failures per scan."""

    def test_the_embedding_thread_setting_caps_ocr_too(self):
        with patch.dict(os.environ, {card_ocr.THREADS_ENV: "2"}):
            self.assertEqual(card_ocr._engine_options(),
                             {"intra_op_num_threads": 2})

    def test_no_setting_means_no_opinion(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(card_ocr.THREADS_ENV, None)
            self.assertEqual(card_ocr._engine_options(), {})

    def test_a_nonsense_value_is_ignored_rather_than_fatal(self):
        """A bad env var must not be the reason a scan stops working."""
        for value in ("", "   ", "two", "0", "-1"):
            with patch.dict(os.environ, {card_ocr.THREADS_ENV: value}):
                self.assertEqual(card_ocr._engine_options(), {}, repr(value))


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class ReadingTests(unittest.TestCase):
    def setUp(self):
        card_ocr.reset()
        self.addCleanup(card_ocr.reset)

    def _read(self, text, image=None):
        engine = _FakeEngine(text)
        with patch.object(card_ocr, "_load_engine", lambda: engine):
            return card_ocr.read_printed_numbers(_jpeg(image or _image(2))), engine

    def test_it_stops_at_the_first_framing_that_reads_a_number(self):
        """Every call is ~1.7s of inference, so a found number ends the search."""
        reads, engine = self._read("017/084")
        self.assertEqual([str(r) for r in reads], ["17/84"])
        self.assertEqual(engine.calls, 1)

    def test_it_keeps_looking_while_nothing_is_legible(self):
        """A framing that reads nothing must not end the search -- that is what
        lets the upright rotation of a sideways card still be tried."""
        _, engine = self._read("nothing readable here")
        self.assertGreater(engine.calls, 1)

    def test_the_narrow_framing_is_actually_a_different_crop(self):
        """Two framings only pay for themselves if they differ; measured, their
        union reads 54 of 87 real photos against 50 and 49 alone."""
        from PIL import Image  # noqa: PLC0415

        view = Image.new("RGB", (1000, 1400))
        widths = [s.width for s in card_ocr._framings([view])]
        self.assertEqual(len(widths), 2)
        self.assertNotEqual(widths[0], widths[1])
        self.assertLess(widths[1], widths[0])

    def test_a_tiny_strip_is_upscaled_before_reading(self):
        """The benchmark's 420px synthetic photos put the number at ~8px tall
        and scored 13.3% -- a result about the images, not about OCR."""
        from PIL import Image  # noqa: PLC0415

        view = Image.new("RGB", (200, 280))
        for strip in card_ocr._framings([view]):
            self.assertGreaterEqual(strip.width, card_ocr._MIN_STRIP_WIDTH)

    def test_it_reads_a_collector_number(self):
        reads, _ = self._read("Illus. Someone|017/084|(c)2026 Pokemon")
        self.assertEqual([str(r) for r in reads], ["17/84"])

    def test_it_survives_the_recogniser_splitting_the_number(self):
        """"095 / 083" arrives in pieces or with the spacing mangled."""
        reads, _ = self._read("095 / 083 AR")
        self.assertEqual([str(r) for r in reads], ["95/83"])

    def test_a_pokedex_reference_is_not_a_collector_number(self):
        """Cards print "NO. 0369 Longevity Pokemon" beside the artwork.

        Upstream's LLM path read exactly that as the collector number and had
        to be taught to discard it. Here the slash requirement does the work,
        and this pins that it keeps doing it.
        """
        reads, _ = self._read("NO. 0369 Longevity Pokemon HT. 3'3\" WT. 51.6 lbs.")
        self.assertEqual(reads, [])

    def test_a_weakness_multiplier_is_not_a_collector_number(self):
        reads, _ = self._read("weakness x2 resistance -30 retreat")
        self.assertEqual(reads, [])

    def test_an_implausible_pair_is_dropped_before_any_query(self):
        # A set with 3 printed cards does not exist; this is mangled flavour text.
        self.assertFalse(card_ocr._plausible("3", "3"))
        # A secret rare numbered above its set's printed total is normal.
        self.assertTrue(card_ocr._plausible("205", "165"))
        self.assertTrue(card_ocr._plausible("17", "84"))

    def test_a_sideways_card_is_still_read(self):
        """Half of one real batch was shot with the card on its side.

        `photo_views` produces upright crops for those, and OCR reuses it so
        the two see the same framings.
        """
        card = _image(3)
        frame = Image.new("RGB", (card.width + 120, card.height + 100), (120, 120, 120))
        frame.paste(card, (60, 50))
        reads, engine = self._read("066/084", image=frame.rotate(90, expand=True))
        self.assertGreaterEqual(engine.calls, 1, "a rotated view must be offered")
        self.assertEqual([str(r) for r in reads], ["66/84"])

    def test_a_broken_engine_does_not_fail_the_scan(self):
        class Exploding:
            def __call__(self, array):
                raise RuntimeError("boom")

        with patch.object(card_ocr, "_load_engine", lambda: Exploding()):
            self.assertEqual(card_ocr.read_printed_numbers(_jpeg(_image(4))), [])

    def test_unreadable_bytes_are_not_an_error(self):
        engine = _FakeEngine("017/084")
        with patch.object(card_ocr, "_load_engine", lambda: engine):
            self.assertEqual(card_ocr.read_printed_numbers(b"not an image"), [])


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class ResolveTests(unittest.TestCase):
    """A read is only worth acting on once it names a card that exists."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.db.add_all([
            Setting(key="tcgdex_sync_languages", value="en,de"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
            Set(id="me05_en", tcg_set_id="me05", name="Pitch Black", lang="en",
                printed_total=84),
            Set(id="me05_de", tcg_set_id="me05", name="Dunkelnacht", lang="de",
                printed_total=84),
            Set(id="sv1_en", tcg_set_id="sv1", name="Scarlet", lang="en",
                printed_total=198),
        ])
        for cid, number, set_id, lang in (
            ("me05-017_en", "017", "me05", "en"),
            ("me05-017_de", "017", "me05", "de"),
            ("me05-021_en", "021", "me05", "en"),
            ("sv1-017_en", "017", "sv1", "en"),
        ):
            self.db.add(Card(id=cid, tcg_card_id=cid.split("_")[0], name=cid,
                             number=number, set_id=set_id, lang=lang,
                             is_custom=False, is_digital=False,
                             images_small=f"https://x/{cid}.png"))
        self.db.commit()
        self.addCleanup(self.db.close)

    def _resolve(self, local, total):
        read = card_ocr.PrintedNumber(local=local, total=total, source="")
        return card_ocr.resolve(self.db, [read], indexable_card_filter(self.db))

    def test_a_number_names_every_language_printing_of_one_card(self):
        ids = {cid for cid, _ in self._resolve("17", "84")}
        self.assertEqual(ids, {"me05-017_en", "me05-017_de"})

    def test_the_printed_total_is_what_makes_it_discriminating(self):
        """Card 017 exists in two sets; only one of them prints 84."""
        self.assertNotIn("sv1-017_en", {cid for cid, _ in self._resolve("17", "84")})
        self.assertEqual({cid for cid, _ in self._resolve("17", "198")}, {"sv1-017_en"})

    def test_a_read_that_names_nothing_resolves_to_nothing(self):
        """The real guard on a misread: it rarely names a card that exists."""
        self.assertEqual(self._resolve("999", "84"), [])
        self.assertEqual(self._resolve("17", "77"), [])

    def test_nothing_read_means_no_query(self):
        self.assertEqual(card_ocr.resolve(self.db, [], indexable_card_filter(self.db)), [])


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class MergeTests(unittest.TestCase):
    """What a read number is allowed to do to a shortlist, and what it is not."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="ocr", hashed_password="x", is_active=True)
        self.db.add_all([
            self.user,
            Setting(key="tcgdex_sync_languages", value="en"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
            Set(id="me05_en", tcg_set_id="me05", name="Pitch Black", lang="en",
                printed_total=84),
        ])
        self.db.commit()
        fingerprint_index.reset()
        card_ocr.reset()

        app = FastAPI()
        app.include_router(router, prefix="/api/cards")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        self._ready = patch.object(fingerprint_index, "READY_MIN_CARDS", 1)
        self._ready.start()
        self.addCleanup(self._ready.stop)
        self.addCleanup(self.client.close)
        self.addCleanup(self.db.close)
        self.addCleanup(fingerprint_index.reset)
        self.addCleanup(card_ocr.reset)

    def _add(self, card_id, number, image=None):
        """A catalogue row; `image=None` is a card with NO artwork at all."""
        self.db.add(Card(
            id=card_id, tcg_card_id=card_id.split("_")[0], name=card_id,
            number=number, set_id="me05", lang="en",
            is_custom=False, is_digital=False,
            images_small=f"https://x/{card_id}.png" if image is not None else None,
            image_phash=fingerprint_reference(_jpeg(image)) if image is not None else None,
        ))
        self.db.commit()
        fingerprint_index.reset()

    def _scan(self, image, read):
        engine = _FakeEngine(read)
        with patch.object(card_ocr, "_load_engine", lambda: engine):
            response = self.client.post(
                "/api/cards/recognize/local",
                files={"file": ("p.jpg", _jpeg(image), "image/jpeg")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _scan_fused(self, image, read):
        """A scan on the ACCURATE path, where a fused score exists per row.

        Every other test here runs the hash path, where `fused_scores` is None
        and the tile-margin arithmetic is skipped entirely -- which is precisely
        why a crash on that arithmetic reached production with a green suite.
        The scores are the real ranking's, turned into the mapping the embedding
        path returns; what matters is that the mapping covers only rows the
        fingerprint index holds, exactly as the real one does.
        """
        import api.recognize_local as rl  # noqa: PLC0415

        real = rl._match_photo

        def fused(*args, **kwargs):
            result = real(*args, **kwargs)
            if result is None:
                return None
            ranked, _ = result
            return ranked, {row: -float(distance) for row, distance in ranked}

        engine = _FakeEngine(read)
        with patch.object(card_ocr, "_load_engine", lambda: engine), \
                patch.object(rl, "_match_photo", fused):
            response = self.client.post(
                "/api/cards/recognize/local",
                files={"file": ("p.jpg", _jpeg(image), "image/jpeg")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_an_inserted_card_does_not_crash_the_accurate_path(self):
        """A card OCR inserted has no fused score -- it was never ranked by
        image, which is the entire reason the number had to find it. Reading a
        tile margin across it raised KeyError and returned a 500."""
        for n in range(1, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(50 + n))
        self._add("me05-084_en", "084", image=None)

        body = self._scan_fused(_image(53), "084/084")

        self.assertEqual(body["matches"][0]["id"], "me05-084_en")
        # No margin is measurable against an unscored row, so the badge must
        # decline to guess rather than report a number calibrated on something
        # else.
        self.assertIsNone(body["matches"][0].get("_match_percent"))

    def test_the_accurate_path_still_scores_a_normal_shortlist(self):
        """The guard must not have quietly turned the badge off for everyone."""
        for n in range(1, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(60 + n))
        body = self._scan_fused(_image(61), "")
        self.assertIsNotNone(body["matches"][0].get("_match_percent"))

    def test_a_read_number_promotes_the_card_it_names(self):
        target = _image(20)
        self._add("me05-001_en", "001", target)
        for n in range(2, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(30 + n))
        body = self._scan(_image(31), "005/084")
        self.assertEqual(body["matches"][0]["id"], "me05-005_en")
        self.assertEqual(body["_printed_numbers"], ["5/84"])

    def test_it_never_removes_a_candidate_the_artwork_found(self):
        """The whole safety argument. The recogniser misreads -- it reports a
        printed PBL as DCBL -- and a wrong number must not delete the right
        card."""
        target = _image(21)
        self._add("me05-001_en", "001", target)
        for n in range(2, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(40 + n))
        before = self._scan(target, "")["matches"]
        after = self._scan(target, "007/084")["matches"]
        self.assertEqual({m["id"] for m in before}, {m["id"] for m in after},
                         "the shortlist may be reordered, never shrunk")
        self.assertEqual(after[0]["id"], "me05-007_en")

    def test_it_finds_a_card_that_has_no_artwork_at_all(self):
        """The reason this exists. 22% of the catalogue is unreachable by image.

        The card is not in the fingerprint index -- it has no picture to index --
        so nothing but the printed number can put it in front of the user.
        """
        for n in range(1, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(50 + n))
        self._add("me05-070_en", "070", image=None)
        body = self._scan(_image(51), "070/084")
        self.assertEqual(body["matches"][0]["id"], "me05-070_en")
        self.assertIsNone(body["matches"][0]["image"])

    def test_a_number_that_names_nothing_changes_nothing(self):
        target = _image(22)
        self._add("me05-001_en", "001", target)
        for n in range(2, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(60 + n))
        plain = self._scan(target, "")["matches"]
        noise = self._scan(target, "999/084")
        self.assertEqual([m["id"] for m in plain], [m["id"] for m in noise["matches"]])
        self.assertEqual(noise["_printed_numbers"], [])

    def test_the_answer_stops_claiming_confidence_once_ocr_reordered_it(self):
        """The badge is calibrated on an image ranking, not on this one."""
        target = _image(23)
        self._add("me05-001_en", "001", target)
        for n in range(2, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(70 + n))
        self.assertFalse(self._scan(target, "004/084")["_identity_confident"])

    def test_an_installation_without_ocr_reports_nothing_read(self):
        target = _image(24)
        self._add("me05-001_en", "001", target)
        for n in range(2, 9):
            self._add(f"me05-{n:03d}_en", f"{n:03d}", _image(80 + n))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(card_ocr.OCR_ENABLED_ENV, None)
            body = self.client.post(
                "/api/cards/recognize/local",
                files={"file": ("p.jpg", _jpeg(target), "image/jpeg")},
            ).json()
        self.assertEqual(body["_printed_numbers"], [])
        self.assertEqual(body["matches"][0]["id"], "me05-001_en")


if __name__ == "__main__":
    unittest.main()
