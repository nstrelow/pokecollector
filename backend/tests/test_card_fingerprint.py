import hashlib
import io
import os
import sys
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import card_fingerprint  # noqa: E402
from services.card_fingerprint import (  # noqa: E402
    HASH_BITS,
    HASH_BYTES,
    MAX_DISTANCE,
    SHORTLIST_MAX_DISTANCE,
    _work_size,
    crop_to_card,
    find_card_box,
    fingerprint_photo,
    fingerprint_reference,
    hash_bits,
    photo_hash_variants,
    is_confident,
    search,
)


def _distance(left, right):
    return int(np.count_nonzero(
        np.unpackbits(np.frombuffer(left, dtype=np.uint8))
        != np.unpackbits(np.frombuffer(right, dtype=np.uint8))
    ))


def _golden_image_bytes():
    """A fixed, committed input for the golden fingerprint vector.

    Built from raw pixel arithmetic and stored as lossless PNG rather than drawn
    with ImageDraw, so the input is byte-identical on any Pillow version and a
    failure can only mean the hash itself moved.
    """
    height, width = 320, 224
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    rows, cols = np.mgrid[0:height, 0:width]
    pixels[..., 0] = ((cols * 3 + rows) % 256).astype(np.uint8)
    pixels[..., 1] = ((rows * 5) % 256).astype(np.uint8)
    pixels[..., 2] = ((cols * cols // 7 + rows * 2) % 256).astype(np.uint8)
    for center_y, center_x, radius, value in (
        (80, 60, 40, 250), (200, 150, 55, 12), (260, 70, 30, 190)
    ):
        inside = (rows - center_y) ** 2 + (cols - center_x) ** 2 <= radius * radius
        pixels[inside] = value
    pixels[0:18, :] = 24
    pixels[300:320, :] = 240
    buf = io.BytesIO()
    Image.fromarray(pixels).save(buf, "PNG", optimize=False)
    return buf.getvalue()


def _card(width=245, height=337, seed=0):
    """A synthetic card with detail across its whole face, like a real one.

    Real cards carry contrast edge to edge -- border, name bar, artwork, text
    block, corner number. A fixture that is blank except for a middle panel
    would make the card detector look worse than it is, because it would find
    only that panel.
    """
    rng = np.random.default_rng(seed)
    hue = int(rng.integers(0, 200))
    img = Image.new("RGB", (width, height), (240, 232, 180))
    draw = ImageDraw.Draw(img)

    # Patterned border, so the card's own edge carries detail.
    for i in range(0, width, 7):
        draw.line([(i, 0), (i, 6)], fill=(hue, 90, 200 - hue), width=3)
        draw.line([(i, height - 7), (i, height - 1)], fill=(hue, 90, 200 - hue), width=3)
    for i in range(0, height, 7):
        draw.line([(0, i), (6, i)], fill=(hue, 140, 60), width=3)
        draw.line([(width - 7, i), (width - 1, i)], fill=(hue, 140, 60), width=3)

    # Name bar.
    draw.rectangle([12, 12, width - 12, 34], fill=(30, 30, 40))
    for i in range(7):
        x = 18 + i * 14
        draw.rectangle([x, 18, x + 9, 29], fill=(230, 220, 90))

    # Artwork panel. Big shapes at seeded positions, not fine noise: a
    # perceptual hash keys on large-scale light and dark structure, and random
    # noise averages to flat grey at 32x32 -- so a noise panel would make every
    # card hash the same.
    panel = (18, 40, width - 18, 40 + height // 2 - 20)
    draw.rectangle(panel, fill=(int(rng.integers(40, 220)),
                                int(rng.integers(40, 220)),
                                int(rng.integers(40, 220))))
    for _ in range(4):
        cx = int(rng.integers(panel[0], panel[2]))
        cy = int(rng.integers(panel[1], panel[3]))
        r = int(rng.integers(18, 55))
        shade = int(rng.integers(0, 255))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                     fill=(shade, 255 - shade, int(rng.integers(0, 255))))

    # Text block and a corner number.
    for i in range(7):
        y = height - 96 + i * 11
        draw.line([(18, y), (width - 30 - (i % 3) * 25, y)], fill=(35, 35, 35), width=3)
    draw.rectangle([width - 52, height - 24, width - 14, height - 12], fill=(20, 20, 20))
    return img


def _to_bytes(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _on_desk(card, pad_x=70, pad_y=55, shade=120):
    canvas = Image.new("RGB", (card.width + 2 * pad_x, card.height + 2 * pad_y),
                       (shade, shade, shade))
    canvas.paste(card, (pad_x, pad_y))
    return canvas


def _on_noisy_desk(card, pad=80, amplitude=25, seed=0):
    """A card on a surface with texture of its own -- wood grain, cloth, carpet."""
    rng = np.random.default_rng(seed)
    width, height = card.width + 2 * pad, card.height + 2 * pad
    speckle = (120 + rng.normal(0, amplitude, (height, width, 1)))
    canvas = Image.fromarray(speckle.clip(0, 255).astype(np.uint8).repeat(3, 2))
    canvas.paste(card, (pad, pad))
    return canvas


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_fixed_width(self):
        digest = fingerprint_reference(_to_bytes(_card()))
        self.assertIsInstance(digest, bytes)
        self.assertEqual(len(digest), HASH_BYTES)

    def test_same_card_at_two_resolutions_matches_closely(self):
        card = _card(seed=1)
        small = fingerprint_reference(_to_bytes(card))
        large = fingerprint_reference(
            _to_bytes(card.resize((card.width * 2, card.height * 2), Image.LANCZOS))
        )
        distance = np.count_nonzero(
            np.unpackbits(np.frombuffer(small, dtype=np.uint8))
            != np.unpackbits(np.frombuffer(large, dtype=np.uint8))
        )
        self.assertLessEqual(distance, MAX_DISTANCE)

    def test_the_stored_fingerprint_is_a_fixed_golden_vector(self):
        """cards.image_phash is persisted, so these bytes are a contract.

        Every stored fingerprint in every install was produced by this exact
        computation. If it changes -- a different DCT sub-block, a different
        resampling filter, a different luma path -- every row already in the
        database silently stops being comparable with freshly computed hashes,
        and nothing else in the suite would notice.

        The guard on the input is the hash of its PIXELS, not of the encoded
        PNG. The PNG bytes depend on Pillow's zlib settings, so a Pillow upgrade
        would have tripped this assertion as a false alarm and invited someone
        to "fix" the golden vector below.

        Do not "fix" that vector. Changing the hash requires a migration that
        clears and recomputes the column.
        """
        raw = _golden_image_bytes()
        with Image.open(io.BytesIO(raw)) as im:
            pixels = np.asarray(im.convert("RGB"))
        self.assertEqual(
            hashlib.sha256(pixels.tobytes()).hexdigest(),
            "4c32905ae44bd754b6558f25c9085aca8c60d08409c85eb02f77a79e9e7f0a0d",
            "the golden input image itself changed, so the vector below proves nothing",
        )
        self.assertEqual(fingerprint_reference(raw).hex(), "e311936e3c1d3f42")

    def test_the_decoded_image_is_rgb_and_independent_of_the_source_file(self):
        """_open's contract, which _hash_bits then relies on.

        Two separate promises. The RGB normalisation is part of the hash
        definition: every fingerprint in every database was computed after it,
        and it is the difference between the two luma paths for a YCbCr source.
        Independence from the file handle is what lets the trailing .copy() go:
        convert("RGB") already calls load() and returns a new image, so keeping
        the copy cost a second full-resolution buffer -- measured at +135MB of
        peak RSS on one 48-megapixel PNG -- for nothing.
        """
        palette = Image.fromarray(
            np.asarray(Image.open(io.BytesIO(_golden_image_bytes())).convert("RGB"))
        ).convert("P", palette=Image.ADAPTIVE)
        buf = io.BytesIO()
        palette.save(buf, "PNG")

        opened = card_fingerprint._open(buf.getvalue())
        self.assertIsNotNone(opened)
        self.assertEqual(opened.mode, "RGB")
        # Fully materialised: usable long after Image.open's context exited.
        self.assertEqual(np.asarray(opened).shape[2], 3)
        self.assertIsNotNone(opened.getpixel((0, 0)))

    def test_l_and_rgb_first_decodes_hash_identically(self):
        """The equivalence api/recognize.py's `hash_bits(mode="L")` relies on.

        That call exists purely to avoid materialising a throwaway RGB copy on
        the LLM scanner's hot path -- it must not change a single output bit.
        Sweeps source PIL mode (RGB, RGBA, L, LA, two P palette flavours, CMYK,
        1-bit, I, F) crossed with every container format that can hold them
        (PNG, TIFF, BMP, GIF-via-palette) plus lossy JPEG/WEBP re-encodes of an
        RGB source, matching the "pixel-identical for every mode but YCbCr"
        claim in `_hash_bits`'s docstring. YCbCr is excluded: Pillow's own
        encoders silently rewrite it to RGB on save, so there is no file this
        app's decoders would ever hand back in that mode to test against.
        """
        rng = np.random.default_rng(0)

        def textured(seed, size):
            w, h = size
            rows, cols = np.mgrid[0:h, 0:w]
            pixels = np.zeros((h, w, 3), dtype=np.uint8)
            pixels[..., 0] = ((cols * 3 + rows * 2 + seed * 37) % 256)
            pixels[..., 1] = ((rows * 5 + seed * 11) % 256)
            pixels[..., 2] = ((cols * cols // 5 + seed * 53) % 256)
            for _ in range(4):
                cy, cx = int(rng.integers(0, h)), int(rng.integers(0, w))
                r = int(rng.integers(10, max(11, min(w, h) // 2)))
                mask = (rows - cy) ** 2 + (cols - cx) ** 2 <= r * r
                pixels[mask] = int(rng.integers(0, 256))
            return Image.fromarray(pixels)

        compared = 0
        for seed in range(30):
            size = (60 + seed, 90 + seed * 2)
            base = textured(seed, size)
            for name, im, fmt in (
                ("RGB", base, "PNG"),
                ("RGBA", base.convert("RGBA"), "PNG"),
                ("L", base.convert("L"), "PNG"),
                ("LA", base.convert("LA"), "PNG"),
                ("P_web", base.convert("P", palette=Image.WEB), "PNG"),
                ("P_adaptive", base.convert("P", palette=Image.ADAPTIVE), "PNG"),
                ("CMYK", base.convert("CMYK"), "TIFF"),
                ("1", base.convert("1"), "PNG"),
                ("I", base.convert("I"), "TIFF"),
                ("F", base.convert("F"), "TIFF"),
                ("RGB_jpeg", base, "JPEG"),
                ("RGB_webp", base, "WEBP"),
            ):
                buf = io.BytesIO()
                im.save(buf, fmt)
                data = buf.getvalue()
                with self.subTest(mode=name, seed=seed):
                    rgb_first = card_fingerprint._hash_bits(
                        card_fingerprint._open(data, mode="RGB")
                    )
                    direct_l = card_fingerprint._hash_bits(
                        card_fingerprint._open(data, mode="L")
                    )
                    np.testing.assert_array_equal(rgb_first, direct_l)
                compared += 1
        self.assertGreater(compared, 250, "the sweep collapsed to almost nothing")

    def test_the_pixel_budget_boundary_admits_an_image_of_exactly_the_cap(self):
        """`> max_pixels`, not `>=`: the cap is a maximum, not a forbidden size."""
        with Image.open(io.BytesIO(_golden_image_bytes())) as im:
            exact = im.width * im.height
        with patch.object(card_fingerprint, "MAX_PIXELS", exact):
            self.assertIsNotNone(fingerprint_reference(_golden_image_bytes()))
        with patch.object(card_fingerprint, "MAX_PIXELS", exact - 1):
            self.assertIsNone(fingerprint_reference(_golden_image_bytes()))

    def test_a_failure_after_decoding_returns_none_instead_of_raising(self):
        """All three entry points document "or None if unusable" -- all three mean it.

        _open was wrapped in try/except but the hashing that follows it was not,
        so a MemoryError from the 32x32 resize on a hostile upload propagated to
        the caller from code whose contract says it returns None. api/recognize
        used to return None here.
        """
        raw = _golden_image_bytes()

        def boom(img):
            raise MemoryError("resize failed")

        with patch.object(card_fingerprint, "_hash_bits", boom):
            self.assertIsNone(fingerprint_reference(raw))
            self.assertIsNone(fingerprint_photo(raw))
            self.assertIsNone(hash_bits(raw))

    def test_the_scanner_tiebreak_uses_the_same_hash_definition(self):
        """api/recognize.py must not grow a second copy of this hash."""
        from api.recognize import _perceptual_hash

        raw = _golden_image_bytes()
        expected = tuple(bool(bit) for bit in hash_bits(raw))
        self.assertEqual(_perceptual_hash(raw), expected)

    def test_a_pixel_budget_is_enforced_before_decoding(self):
        with patch.object(card_fingerprint, "MAX_PIXELS", 100):
            self.assertIsNone(fingerprint_reference(_golden_image_bytes()))
        self.assertIsNotNone(fingerprint_reference(_golden_image_bytes()))

    def test_degraded_copies_rank_ahead_of_every_other_card(self):
        """What actually has to hold, stated honestly.

        The old version of this test asserted that two synthetic cards are more
        than MAX_DISTANCE apart, on one hand-picked pair of seeds. That claim is
        false in general: over 780 distinct pairs from seeds 0-39 the median
        distance is 16 and 30% of pairs fall INSIDE MAX_DISTANCE, so a threshold
        on absolute distance separates nothing here.

        Relative ranking does hold, and it is what the endpoint depends on: a
        rescaled or JPEG-crushed copy of a card must beat every other card in
        the index.
        """
        cards = [_card(seed=i) for i in range(24)]
        digests = [fingerprint_reference(_to_bytes(c)) for c in cards]
        index = np.frombuffer(b"".join(digests), dtype=np.uint8).reshape(
            len(digests), HASH_BYTES
        )

        same_card = []
        for position, card in enumerate(cards):
            doubled = card.resize((card.width * 2, card.height * 2), Image.LANCZOS)
            crushed = io.BytesIO()
            card.save(crushed, "JPEG", quality=45)
            for variant in (_to_bytes(doubled), crushed.getvalue()):
                digest = fingerprint_reference(variant)
                ranked = search(digest, index, limit=2)
                self.assertEqual(ranked[0][0], position)
                same_card.append(_distance(digests[position], digest))

        # And the degradation itself stays small in absolute terms.
        self.assertLessEqual(max(same_card), 6)

    def test_distinct_cards_are_not_separable_by_distance_alone(self):
        """Pinned deliberately, so the false claim cannot be reintroduced.

        A 64-bit pHash has a noise floor. Measured against the real 42k-card
        catalogue a pure-noise upload still lands 12-20 bits from its nearest
        neighbour, which is exactly where genuine hard matches live. Any future
        assertion that "different cards are more than MAX_DISTANCE apart" is
        wrong, and this records why.
        """
        digests = [fingerprint_reference(_to_bytes(_card(seed=i))) for i in range(24)]
        pairs = [
            _distance(digests[i], digests[j])
            for i in range(len(digests))
            for j in range(i + 1, len(digests))
        ]
        self.assertGreater(np.mean(np.array(pairs) <= MAX_DISTANCE), 0.1)

    def test_unreadable_bytes_return_none(self):
        self.assertIsNone(fingerprint_reference(b"not an image"))
        self.assertIsNone(fingerprint_photo(b""))


class CropTests(unittest.TestCase):
    def test_card_on_a_desk_is_located(self):
        card = _card(seed=4)
        box = find_card_box(_on_desk(card))
        self.assertIsNotNone(box)
        found_area = (box[2] - box[0]) * (box[3] - box[1])
        # Should recover most of the card without swallowing the whole frame.
        self.assertGreater(found_area, 0.4 * card.width * card.height)

    def test_the_box_lands_on_the_card_and_not_beside_it(self):
        """A box of the right size in the wrong place still crops the wrong thing.

        Detection is worth far more than the hash here, and every edge is
        recovered to within a few pixels, so this asserts placement tightly
        enough that a systematic shift cannot hide inside the tolerance.
        """
        for seed, pad_x, pad_y in ((7, 90, 70), (8, 200, 90), (9, 120, 160)):
            with self.subTest(seed=seed):
                card = _card(seed=seed)
                photo = _on_desk(card, pad_x=pad_x, pad_y=pad_y)
                box = find_card_box(photo)
                self.assertIsNotNone(box)
                expected = (pad_x, pad_y, pad_x + card.width, pad_y + card.height)
                tolerance = 0.015 * photo.width
                for edge, got, want in zip(("left", "top", "right", "bottom"),
                                           box, expected):
                    self.assertLess(
                        abs(got - want), tolerance,
                        f"{edge} edge off by {abs(got - want)}px "
                        f"(box={box}, card at {expected})",
                    )

    def test_a_textured_surface_does_not_swallow_the_card(self):
        """The background's own texture must not count as card.

        A card on grain or cloth is the normal case, not an edge case: without
        subtracting the background's energy level the box grows to the frame and
        the crop stops helping at all.
        """
        card = _card(seed=11)
        photo = _on_noisy_desk(card)
        box = find_card_box(photo)
        self.assertIsNotNone(box)
        expected = (80, 80, 80 + card.width, 80 + card.height)
        for edge, got, want in zip(("left", "top", "right", "bottom"),
                                   box, expected):
            self.assertLess(abs(got - want), 0.04 * photo.width,
                            f"{edge} edge off by {abs(got - want)}px (box={box})")

    def test_a_region_shaped_nothing_like_a_card_is_rejected(self):
        """Cards are roughly 0.7:1. A long strip is a pen, a ruler, a table edge."""
        card = _card(seed=12)
        for name, size, at in (
            ("wide", (420, 55), (140, 270)),
            ("tall", (50, 430), (320, 85)),
        ):
            with self.subTest(shape=name):
                canvas = Image.new("RGB", (700, 600), (120, 120, 120))
                canvas.paste(card.resize(size), at)
                self.assertIsNone(find_card_box(canvas))

    def test_a_subject_too_small_to_be_the_card_is_rejected(self):
        """A speck in a big frame is something on the desk, not the card."""
        canvas = Image.new("RGB", (1200, 1600), (120, 120, 120))
        canvas.paste(_card(seed=13).resize((60, 82)), (500, 700))
        self.assertIsNone(find_card_box(canvas))

    def test_the_analysed_size_is_bounded_on_both_axes(self):
        """Regression: a tall, narrow upload used to be UPSCALED.

        sanitize_image_bytes caps the longest edge at 2048 but not the aspect
        ratio. Scaling detection by width alone turned a 48KB 32x2048 JPEG into
        a 320x20480 float64 array -- 0.6s and 580MB of RSS for one request, on
        an app that runs a single uvicorn worker.
        """
        for width, height in ((32, 2048), (16, 4000), (2048, 32), (1500, 2000)):
            with self.subTest(size=(width, height)):
                work_width, work_height = _work_size(width, height)
                self.assertLessEqual(work_width, 320)
                self.assertLessEqual(work_height, 640)
        # Unchanged for anything up to 2:1, which is every real card photo.
        self.assertEqual(_work_size(1500, 2000), (320, 426))
        self.assertEqual(_work_size(245, 337), (320, 440))

    def test_a_tall_narrow_upload_is_handled_cheaply(self):
        sliver = Image.new("RGB", (32, 2048))
        sliver.putdata([(i % 256, (i * 7) % 256, (i * 13) % 256)
                        for i in range(32 * 2048)])
        sizes = []
        original = Image.Image.resize

        def spy(self, size, *args, **kwargs):
            sizes.append(size)
            return original(self, size, *args, **kwargs)

        with patch.object(Image.Image, "resize", spy):
            find_card_box(sliver)
        self.assertTrue(sizes)
        for size in sizes:
            self.assertLessEqual(size[0] * size[1], 320 * 640)

    def test_an_already_tight_image_is_left_alone(self):
        card = _card(seed=5)
        self.assertEqual(crop_to_card(card).size, card.size)

    def test_the_crop_threshold_is_the_benchmarked_value(self):
        """_ALREADY_TIGHT is tuned, not chosen. Pin it as a literal.

        Raising it to 0.90 was tried, to make a hand-held photo filling 0.768
        of the frame get cropped. It works for that photo and costs 12.34
        points of shortlist recall over the 7,098-photo set (91.62% -> 79.28%),
        because past ~0.72 the energy box trims into the card. It may only move
        together with a benchmark run over data/queries_multi.
        """
        self.assertEqual(card_fingerprint._ALREADY_TIGHT, 0.72)

    def test_the_already_tight_boundary_is_inclusive(self):
        """A box covering exactly _ALREADY_TIGHT of the frame is left alone.

        The threshold exists because cropping a photo the card already fills
        only desynchronises it from the catalogue render. At exactly the
        threshold there is by definition nothing worth removing, so cropping
        there would trim ~28% of the frame for no reason. Detection cannot be
        steered to hit the boundary exactly, so the box is supplied directly.
        """
        frame = Image.new("RGB", (100, 100))
        exact = (0, 0, 90, 80)  # 7200 / 10000 == _ALREADY_TIGHT
        self.assertAlmostEqual(
            (exact[2] - exact[0]) * (exact[3] - exact[1]) / 10000.0,
            card_fingerprint._ALREADY_TIGHT,
        )
        with patch.object(card_fingerprint, "find_card_box", lambda img: exact):
            self.assertEqual(crop_to_card(frame).size, (100, 100))

        just_under = (0, 0, 90, 79)  # 7110 / 10000, below the threshold
        with patch.object(card_fingerprint, "find_card_box", lambda img: just_under):
            self.assertEqual(crop_to_card(frame).size, (90, 79))

    def test_cropping_makes_a_desk_photo_match_the_reference(self):
        """The whole point of the crop: framing must not break recognition."""
        card = _card(seed=6)
        reference = fingerprint_reference(_to_bytes(card))
        photo = _to_bytes(_on_desk(card), "JPEG")

        cropped = fingerprint_photo(photo)
        with Image.open(io.BytesIO(photo)) as im:
            uncropped = fingerprint_reference(_to_bytes(im.convert("RGB")))

        def distance(x, y):
            return np.count_nonzero(
                np.unpackbits(np.frombuffer(x, dtype=np.uint8))
                != np.unpackbits(np.frombuffer(y, dtype=np.uint8))
            )

        self.assertLess(distance(reference, cropped), distance(reference, uncropped))


class BlurTests(unittest.TestCase):
    def test_the_moving_average_window_is_exactly_seven_wide(self):
        """radius=3 means a 7-tap window, centred.

        The box blur sets the scale at which the detector reads texture. A
        window one tap narrower still blurs, still passes every end-to-end
        assertion, and quietly shifts every energy profile by half a pixel.
        """
        impulse = np.zeros((1, 21))
        impulse[0, 10] = 7.0
        blurred = card_fingerprint._box_blur(impulse, radius=3)

        spread = np.flatnonzero(blurred[0] > 1e-9)
        self.assertEqual(spread.tolist(), list(range(7, 14)))
        self.assertAlmostEqual(float(blurred[0, 10]), 1.0)
        # Centred, so the mass lands symmetrically around the impulse.
        self.assertAlmostEqual(float(blurred[0, 7]), float(blurred[0, 13]))
        self.assertAlmostEqual(float(blurred[0].sum()), 7.0)

    def test_the_default_radius_is_the_one_the_detector_is_tuned_at(self):
        """find_card_box calls _box_blur with no radius, so the default is the

        tuning. The test above passes radius=3 explicitly and therefore says
        nothing about it: changing the default to 1 narrows the smoothing the
        energy profile is built from, and the crop tests are far too tolerant to
        notice a texture scale three times finer.
        """
        impulse = np.zeros((1, 21))
        impulse[0, 10] = 7.0
        default = card_fingerprint._box_blur(impulse)
        self.assertEqual(
            np.flatnonzero(default[0] > 1e-9).tolist(), list(range(7, 14))
        )
        np.testing.assert_allclose(
            default, card_fingerprint._box_blur(impulse, radius=3)
        )

    def test_the_smoothing_scale_is_what_separates_card_from_carpet(self):
        """Why radius matters, not just that it has a value.

        A textured background is high-frequency; the card is a large structure.
        Smoothing at the wrong scale lets the background's own grain survive
        into the energy profile, and the box grows to swallow the frame. This is
        the failure a narrower window causes and the end-to-end crop assertions
        do not catch.
        """
        rng = np.random.default_rng(3)
        speckle = rng.normal(0, 1.0, (1, 401))
        wide = card_fingerprint._box_blur(speckle, radius=3)
        narrow = card_fingerprint._box_blur(speckle, radius=1)
        self.assertLess(float(np.std(wide)), float(np.std(narrow)) * 0.85)


class SearchTests(unittest.TestCase):
    def _index(self, digests):
        return np.array(
            [np.frombuffer(d, dtype=np.uint8) for d in digests], dtype=np.uint8
        )

    def test_search_returns_nearest_first(self):
        digests = [fingerprint_reference(_to_bytes(_card(seed=i))) for i in range(6)]
        index = self._index(digests)
        ranked = search(digests[3], index, limit=3)
        self.assertEqual(ranked[0][0], 3)
        self.assertEqual(ranked[0][1], 0)
        self.assertLessEqual(ranked[0][1], ranked[1][1])

    def test_hopeless_matches_are_dropped_from_the_shortlist(self):
        """An empty shortlist has to be reachable, or the UI branch is dead."""
        digests = [fingerprint_reference(_to_bytes(_card(seed=i))) for i in range(6)]
        index = self._index(digests)
        self.assertEqual(search(digests[2], index, limit=6, max_distance=0),
                         [(2, 0)])
        self.assertEqual(search(digests[2], index, limit=6, max_distance=-1), [])
        # The default cutoff keeps a genuine self-match.
        ranked = search(digests[2], index, limit=6,
                        max_distance=SHORTLIST_MAX_DISTANCE)
        self.assertEqual(ranked[0], (2, 0))

    def test_the_shortlist_distance_boundary_is_inclusive(self):
        """24 is the furthest entry the shortlist keeps, not the first it drops.

        Pinned as a literal 24, not `SHORTLIST_MAX_DISTANCE`: a test that reads
        the cutoff back off the constant stays green no matter what the
        constant is changed to (an independent mutation run showed 24 -> 64
        surviving the whole suite), when the point is to prove where today's
        line actually sits.
        """
        query = bytes(HASH_BYTES)
        exactly_24 = bytes([0xFF, 0xFF, 0xFF, 0x00, 0x00, 0x00, 0x00, 0x00])
        exactly_25 = bytes([0xFF, 0xFF, 0xFF, 0x01, 0x00, 0x00, 0x00, 0x00])
        self.assertEqual(_distance(query, exactly_24), 24)
        self.assertEqual(_distance(query, exactly_25), 25)

        index = self._index([exactly_24, exactly_25])
        ranked = search(query, index, limit=2, max_distance=24)
        self.assertEqual(ranked, [(0, 24)])

    def test_the_shortlist_is_exactly_the_twelve_nearest_in_a_fixed_order(self):
        """Ranking must be total and reproducible, not whatever argpartition left.

        search selects with argpartition, which returns its `limit` smallest in
        no order at all and does not even pick a predictable subset when rows
        tie. Sorting the selection by distance alone is not enough: on a
        realistic 9,000-row index that disagrees with the intended
        (distance, row) order on essentially every query, so identical artwork
        -- reprints and promos, which the catalogue really does contain --
        would swap rank between rebuilds.
        """
        rng = np.random.default_rng(4)
        index = rng.integers(0, 256, (9000, HASH_BYTES), dtype=np.uint8)
        query = bytes(rng.integers(0, 256, HASH_BYTES, dtype=np.uint8))

        ranked = search(query, index, limit=12)
        every = sorted(
            (int(distance), row)
            for row, distance in enumerate(card_fingerprint.distances(query, index))
        )
        self.assertEqual(ranked, [(row, distance) for distance, row in every[:12]])

    def test_identical_rows_always_rank_in_catalogue_order(self):
        target = bytes([0] * HASH_BYTES)
        far = bytes([0xFF, 0xFF, 0xFF, 0, 0, 0, 0, 0])
        index = self._index([far] + [target] * 50 + [far] * 4000)
        ranked = search(target, index, limit=3)
        self.assertEqual([row for row, _ in ranked], [1, 2, 3])
        self.assertEqual([distance for _, distance in ranked], [0, 0, 0])

    def test_search_on_empty_index_is_safe(self):
        empty = np.empty((0, HASH_BYTES), dtype=np.uint8)
        self.assertEqual(search(fingerprint_reference(_to_bytes(_card())), empty), [])

    def test_the_composite_ranking_key_survives_a_catalogue_sized_index(self):
        """The distance is shifted 32 bits, not 16, and the catalogue is 58,634.

        search packs (distance, row index) into one int64 so argpartition can
        rank by distance with ties broken by row. Sixteen bits of room for the
        row index is enough for 65,536 rows and then silently wrong: a row past
        that overflows into the distance field and outranks genuinely closer
        cards. The live catalogue is already 58,634 rows, so this is not a
        theoretical bound -- it is one set away.
        """
        rows = 70000
        query = bytes(HASH_BYTES)
        index = np.zeros((rows, HASH_BYTES), dtype=np.uint8)
        index[:, 0] = 0xFF          # everything is 8 bits away by default
        index[0][0] = 0b1           # ...except row 0, which is 1 bit away
        index[rows - 1][0] = 0      # ...and the last row, an exact match

        ranked = search(query, index, limit=3)
        self.assertEqual(
            ranked[0], (rows - 1, 0),
            "the exact match past row 65,535 must still rank first",
        )
        self.assertEqual(ranked[1], (0, 1))

        # Ties across the boundary still resolve to the lower row index.
        tied = np.zeros((rows, HASH_BYTES), dtype=np.uint8)
        tied[:, 0] = 0xFF
        for row in (3, 65535, 65536, rows - 1):
            tied[row][0] = 0
        self.assertEqual(
            [row for row, _ in search(query, tied, limit=4)],
            [3, 65535, 65536, rows - 1],
        )

    def test_confidence_needs_distance_and_margin(self):
        self.assertTrue(is_confident([(0, 2), (1, 14)]))
        # Close to the top match, so the two cannot be told apart.
        self.assertFalse(is_confident([(0, 2), (1, 4)]))
        # Clear margin, but nothing is actually similar.
        self.assertFalse(is_confident([(0, 30), (1, 60)]))

    def test_the_tuned_constants_have_not_drifted(self):
        """Pin the module's tuned constants to literals, not just each other.

        A test that reads its expected value back off `card_fingerprint.X`
        stays green no matter what `X` is changed to -- it is testing that the
        module agrees with itself, not that the tuned value survived. An
        independent mutation run showed MIN_MARGIN 5 -> 3 survives the whole
        suite, and it drops confident precision from 99.0% to 96.5% (see the
        module-level comment above MIN_MARGIN for the full sweep).
        """
        self.assertEqual(card_fingerprint.MIN_MARGIN, 5)
        self.assertEqual(MAX_DISTANCE, 12)
        self.assertEqual(SHORTLIST_MAX_DISTANCE, 24)

    def test_the_margin_boundary_is_inclusive(self):
        """5 is the smallest margin that counts, not the first that fails.

        The sweep behind it reads "MIN_MARGIN 5 -> 15.6% confident at 99.0%
        precision", and those figures were measured with a margin of exactly 5
        accepted. Excluding it silently moves the operating point to the
        MIN_MARGIN=6 row and quietly drops confident answers. The boundary
        itself is a literal, not `card_fingerprint.MIN_MARGIN`, so a change to
        the constant fails this test instead of silently moving it.
        """
        self.assertTrue(is_confident([(0, 2), (1, 7)]))
        self.assertFalse(is_confident([(0, 2), (1, 6)]))

    def test_the_distance_boundary_is_inclusive(self):
        self.assertTrue(is_confident([(0, 12), (1, 52)]))
        self.assertFalse(is_confident([(0, 13), (1, 53)]))

    def test_a_match_in_the_noise_floor_is_never_confident(self):
        """The distance bound has to bind somewhere useful.

        A 64-bit hash over a 42k catalogue puts even a photo of nothing at all
        12-20 bits from its nearest row. A wide margin at that distance is a
        coincidence, not a match, so MAX_DISTANCE must sit below the floor --
        it was 20, which admitted the whole of it.
        """
        self.assertFalse(is_confident([(0, 16), (1, 30)]))
        self.assertFalse(is_confident([(0, 14), (1, 40)]))
        self.assertTrue(is_confident([(0, 10), (1, 40)]))


class PhotoVariantTests(unittest.TestCase):
    """Both sides of the crop decision are searched, not just the chosen one."""

    def test_a_photo_with_background_yields_the_crop_and_the_frame(self):
        photo = _to_bytes(_on_desk(_card(seed=11)), "JPEG")
        variants = card_fingerprint.photo_hash_variants(photo)
        self.assertEqual(len(variants), 2)
        self.assertEqual(len(set(variants)), 2)

    def test_the_leading_variant_is_what_fingerprint_photo_returns(self):
        """The two entry points must not disagree about which crop leads."""
        for seed, wrap in ((12, _on_desk), (13, lambda c: c)):
            with self.subTest(seed=seed):
                photo = _to_bytes(wrap(_card(seed=seed)), "JPEG")
                variants = card_fingerprint.photo_hash_variants(photo)
                self.assertEqual(fingerprint_photo(photo), variants[0])

    def test_a_photo_needing_a_crop_leads_with_the_crop(self):
        photo = _to_bytes(_on_desk(_card(seed=14)), "JPEG")
        with Image.open(io.BytesIO(photo)) as im:
            frame = im.convert("RGB")
            box = find_card_box(frame)
            self.assertIsNotNone(box)
            cropped = card_fingerprint._pack_bits(
                card_fingerprint._hash_bits(frame.crop(box))
            )
        self.assertEqual(card_fingerprint.photo_hash_variants(photo)[0], cropped)

    def test_an_unreadable_photo_yields_no_variants(self):
        self.assertEqual(card_fingerprint.photo_hash_variants(b""), [])
        self.assertEqual(card_fingerprint.photo_hash_variants(b"not an image"), [])
        self.assertIsNone(fingerprint_photo(b""))

    def test_detection_failure_is_swallowed_as_it_was_before(self):
        photo = _to_bytes(_card(seed=15), "JPEG")
        with patch.object(
            card_fingerprint, "find_card_box", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(card_fingerprint.photo_hash_variants(photo), [])
            self.assertIsNone(fingerprint_photo(photo))


class MatchLeaderCountTests(unittest.TestCase):
    """Which tiles are leaders, and how many of them there are."""

    def test_nothing_is_the_leader_when_nothing_is_strictly_closest(self):
        """Two tiles at the same distance are two the hash cannot separate.

        Which of them lands at rank 1 is decided by row index, which carries no
        evidence at all, so neither gets the odds of a tile that actually won.
        """
        self.assertEqual(
            card_fingerprint.match_leader_counts([(0, 4), (1, 4), (2, 10)]),
            [2, 2, 0],
        )

    def test_a_strictly_closest_tile_leads_alone(self):
        self.assertEqual(
            card_fingerprint.match_leader_counts([(0, 2), (1, 4), (2, 4)]),
            [1, 0, 0],
        )

    def test_the_count_is_the_width_of_the_tie_not_a_flag(self):
        """A four-way tie has to divide the odds four ways, not two."""
        self.assertEqual(
            card_fingerprint.match_leader_counts(
                [(0, 6), (1, 6), (2, 6), (3, 6), (4, 8)]
            ),
            [4, 4, 4, 4, 0],
        )

    def test_a_lone_candidate_leads(self):
        self.assertEqual(card_fingerprint.match_leader_counts([(0, 8)]), [1])

    def test_an_empty_shortlist_has_no_leaders(self):
        self.assertEqual(card_fingerprint.match_leader_counts([]), [])


class MatchProbabilityTests(unittest.TestCase):
    """The percentage printed on a candidate is measured, not derived."""

    def test_it_is_not_the_naive_distance_arithmetic(self):
        """(HASH_BITS - d) / HASH_BITS would call a noise match 78%.

        A 64-bit hash over a 42k catalogue puts a photo of nothing 12-20 bits
        from its nearest row, so any linear reading of distance reports most of
        the noise floor as a strong match. The whole point of the table is that
        it collapses there instead.
        """
        naive = round((HASH_BITS - 14) / HASH_BITS * 100)
        self.assertEqual(naive, 78)
        self.assertLess(card_fingerprint.match_probability(14, leaders=1), 40)
        self.assertLess(card_fingerprint.match_probability(14, leaders=0), 10)

    def test_the_leader_and_the_tail_are_not_the_same_proposition(self):
        for distance in (0, 4, 8, 10, 12):
            with self.subTest(distance=distance):
                self.assertGreater(
                    card_fingerprint.match_probability(distance, leaders=1),
                    card_fingerprint.match_probability(distance, leaders=0),
                )

    def test_a_wider_tie_is_worse_odds_for_every_tile_in_it(self):
        """The evidence does not multiply by being shared."""
        for distance in (0, 4, 8, 12):
            with self.subTest(distance=distance):
                values = [
                    card_fingerprint.match_probability(distance, leaders=k)
                    for k in range(1, card_fingerprint.MAX_TIE_BAND + 1)
                ]
                self.assertEqual(values, sorted(values, reverse=True))

    def test_tied_tiles_cannot_add_up_to_more_than_a_certainty(self):
        """The defect this table replaced: twelve badges summing to 1008%.

        At most one tile in a tie is the right artwork, so k tiles each showing
        P must satisfy k * P <= 100. The old table gave every tied row the
        rank-1 curve and broke this on 78.7% of real shortlists.
        """
        # Up to 24, not up to 8. A shortlist holds twelve tiles and all twelve
        # can tie; stopping at 8 passed while a twelve-way tie summed to 148%,
        # because the widest measured row is an aggregate over ties averaging
        # about eight and handing it to twelve tiles overstates every one.
        for k in range(1, 25):
            for distance in range(0, 26, 2):
                with self.subTest(k=k, distance=distance):
                    total = k * card_fingerprint.match_probability(
                        distance, leaders=k
                    )
                    self.assertLessEqual(total, 100)

    def test_a_tie_too_wide_to_have_been_measured_divides_the_band(self):
        widest = card_fingerprint.match_probability(
            0, leaders=card_fingerprint.MAX_TIE_BAND
        )
        doubled = card_fingerprint.match_probability(
            0, leaders=card_fingerprint.MAX_TIE_BAND * 2
        )
        self.assertLess(doubled, widest)
        self.assertGreaterEqual(doubled, 1, "a shown tile is never impossible")

    def test_it_falls_as_distance_grows(self):
        for leaders in (0, 1, 2, 3, 5, 9):
            with self.subTest(leaders=leaders):
                values = [
                    card_fingerprint.match_probability(d, leaders=leaders)
                    for d in range(0, 20, 2)
                ]
                self.assertEqual(values, sorted(values, reverse=True))

    def test_odd_distances_interpolate_between_the_measured_ones(self):
        for leaders in (0, 1, 2, 3):
            with self.subTest(leaders=leaders):
                low = card_fingerprint.match_probability(10, leaders=leaders)
                high = card_fingerprint.match_probability(8, leaders=leaders)
                middle = card_fingerprint.match_probability(9, leaders=leaders)
                self.assertLessEqual(low, middle)
                self.assertLessEqual(middle, high)

    def test_it_stays_a_percentage_at_and_beyond_both_ends(self):
        for distance in (-5, 0, 1, 24, 64, 1000):
            for leaders in (0, 1, 2, 5, 40):
                with self.subTest(distance=distance, leaders=leaders):
                    value = card_fingerprint.match_probability(
                        distance, leaders=leaders
                    )
                    self.assertIsInstance(value, int)
                    self.assertGreaterEqual(value, 1)
                    self.assertLessEqual(value, 100)

    def test_a_tie_wider_than_the_measured_bands_is_not_an_error(self):
        """Ties of 9 and more exist, and get less than the widest measured row.

        They cannot simply share it: that row is an aggregate over ties around
        eight wide, so giving it to forty tiles would have forty of them each
        claiming what eight of them earned.
        """
        widest = card_fingerprint.match_probability(
            4, leaders=card_fingerprint.MAX_TIE_BAND
        )
        wider = card_fingerprint.match_probability(4, leaders=40)
        self.assertLess(wider, widest)
        self.assertIsInstance(wider, int)

    def test_a_distance_a_band_never_saw_defers_to_a_wider_tie(self):
        """A unique leader at distance 16 was observed twice, both times wrong.

        Clamping to the band's own last value would print 27% for it. Deferring
        to the widest measured tie prints what a tile that far out is actually
        worth, without inventing a number for a cell with no observations.
        """
        self.assertLess(
            card_fingerprint.match_probability(16, leaders=1),
            card_fingerprint.match_probability(14, leaders=1),
        )
        self.assertEqual(
            card_fingerprint.match_probability(16, leaders=1),
            card_fingerprint.match_probability(16, leaders=card_fingerprint.MAX_TIE_BAND),
        )

    def test_a_perfect_hash_match_is_still_not_a_certainty(self):
        """Distance 0 means identical artwork, which reprints genuinely share."""
        self.assertLess(card_fingerprint.match_probability(0, leaders=1), 100)

    def test_a_unique_leader_is_no_longer_understated(self):
        """The old blended curve called a strictly-closest distance-0 match 92%.

        It was measured at 98.9%, and the understatement came from averaging it
        with the tied case rather than from any property of the match.
        """
        self.assertGreaterEqual(card_fingerprint.match_probability(0, leaders=1), 98)


class FusedMatchProbabilityTests(unittest.TestCase):
    """The badge for a ranking the embedding produced."""

    def test_it_rises_with_the_margin(self):
        values = [
            card_fingerprint.fused_match_probability(m, 1)
            for m in (0.0, 0.3, 0.6, 1.0, 2.0, 5.0, 50.0)
        ]
        self.assertEqual(values, sorted(values))

    def test_a_tile_with_nothing_to_say_says_nothing(self):
        """Two ways a tile earns silence, and both are about the measurement.

        Past margin 2, tiles 2 and 3 were wrong 840 times out of 840. And tiles
        past the third were only ever measured as one pooled bucket, under 0.9%
        at every margin, so there is no per-tile number to print. Rounding
        either up to a 1% floor across nine tiles is how a shortlist ends up
        claiming 101%, which is the arithmetic this branch exists to stop.
        """
        self.assertIsNone(card_fingerprint.fused_match_probability(6.0, 2))
        self.assertIsNotNone(card_fingerprint.fused_match_probability(6.0, 1))
        for margin in (0.0, 1.0, 6.0):
            self.assertIsNone(card_fingerprint.fused_match_probability(margin, 4))
            self.assertIsNone(card_fingerprint.fused_match_probability(margin, 12))

    def test_a_shortlist_nobody_can_separate_says_so(self):
        """Measured: below margin 0.25 the leader is right 47% of the time.

        That is the number worth printing. The tile is still shown, still first,
        and the user is told it is close to a coin flip.
        """
        self.assertLess(card_fingerprint.fused_match_probability(0.1, 1), 55)
        self.assertGreater(card_fingerprint.fused_match_probability(0.1, 2), 25)

    def test_a_clear_winner_is_not_called_a_certainty(self):
        self.assertLessEqual(card_fingerprint.fused_match_probability(99.0, 1), 99)

    def test_later_tiles_never_outrank_earlier_ones(self):
        for margin in (0.0, 0.5, 1.0, 2.0, 6.0):
            with self.subTest(margin=margin):
                values = [
                    card_fingerprint.fused_match_probability(margin, r) or 0
                    for r in range(1, 6)
                ]
                self.assertEqual(values, sorted(values, reverse=True))

    def test_the_measured_tiles_cannot_sum_past_a_certainty(self):
        """Only one tile can be the right artwork, at any margin."""
        for margin in (0.0, 0.2, 0.4, 0.8, 1.2, 2.0, 3.5, 10.0):
            with self.subTest(margin=margin):
                total = sum(
                    card_fingerprint.fused_match_probability(margin, r) or 0
                    for r in range(1, 13)
                )
                self.assertLessEqual(
                    total, 100,
                    "every tile in the shortlist, not just the measured three",
                )


class SidewaysCardTests(unittest.TestCase):
    """A card lying on its side, which defeated detection completely."""

    def _sideways(self, seed=5):
        """A card rotated 90 degrees inside a landscape frame."""
        card = _card(seed=seed)
        frame = _on_desk(card, pad_x=60, pad_y=50)
        return frame.rotate(90, expand=True)

    def test_the_detector_alone_cannot_see_a_sideways_card(self):
        """The bug, pinned. A card is 0.716 upright and about 1.4 on its side,
        and the box gate stops at 1.35, so detection returns nothing at all."""
        turned = self._sideways()
        self.assertGreater(turned.width, turned.height)
        self.assertIsNone(find_card_box(turned))
        self.assertLess(card_fingerprint._MAX_ASPECT, 1.39)

    def test_a_sideways_card_is_still_found_by_rotating_the_photo(self):
        """Rotating the IMAGE makes the card upright, and the UNCHANGED gate
        then accepts it. Measured on real photos at aspect 0.66-0.70."""
        views = card_fingerprint.photo_views(self._sideways())
        self.assertGreater(len(views), 1, "a rotated view must be offered")
        cropped = [v for v in views[1:] if v.height > v.width]
        self.assertTrue(cropped, "at least one view must be an upright card")
        aspect = cropped[0].width / cropped[0].height
        self.assertLess(aspect, card_fingerprint._MAX_ASPECT)

    def test_an_upright_photo_is_left_exactly_as_it_was(self):
        """The condition is "detection failed", so a working photo pays nothing.

        Every extra view lowers every catalogue row's score, not just the right
        one. Merging a 180-degree variant was measured and rejected for exactly
        that reason, so this must not quietly start doing it everywhere.
        """
        frame = _on_desk(_card(seed=6))
        self.assertIsNotNone(find_card_box(frame))
        views = card_fingerprint.photo_views(frame)
        self.assertEqual(len(views), 2, "crop and full frame, and nothing else")

    def test_the_hash_and_the_embedding_see_the_same_views(self):
        """One definition, so the two rankings cannot disagree about framing."""
        frame = _on_desk(_card(seed=7))
        self.assertEqual(
            len(card_fingerprint.photo_views(frame)),
            len(photo_hash_variants(_to_bytes(frame))),
        )

    def test_a_photo_with_no_card_at_all_still_returns_something(self):
        blank = Image.new("RGB", (400, 300), (128, 128, 128))
        views = card_fingerprint.photo_views(blank)
        self.assertTrue(views, "the frame itself is always a view")


class SearchVariantsTests(unittest.TestCase):
    """Merging can only pull a row up the shortlist, never push one out."""

    def _index(self, *hashes):
        return np.array(
            [np.frombuffer(h, dtype=np.uint8) for h in hashes], dtype=np.uint8
        )

    def test_a_row_is_scored_by_its_best_variant(self):
        near = bytes([0b00000000] * 8)
        far = bytes([0b11111111] * 8)
        index = self._index(near, far)
        # The first query is 8 bits from row 0; the second is exact.
        blurry = bytes([0b11111111] + [0] * 7)
        ranked = card_fingerprint.search_variants([blurry, near], index)
        self.assertEqual(ranked[0], (0, 0))

    def test_a_candidate_only_one_variant_finds_still_reaches_the_shortlist(self):
        target = bytes([0b00001111] * 8)
        decoy = bytes([0b10101010] * 8)
        index = self._index(decoy, target)
        # Query A sees only the decoy; query B is the target exactly.
        ranked = card_fingerprint.search_variants([decoy, target], index, limit=12)
        found = {row for row, _ in ranked}
        self.assertEqual(found, {0, 1})
        self.assertEqual(dict(ranked)[1], 0)

    def test_the_merge_does_not_depend_on_variant_order(self):
        index = self._index(
            bytes([0b00000000] * 8), bytes([0b00001111] * 8), bytes([0b11111111] * 8)
        )
        a, b = bytes([0b00000001] * 8), bytes([0b00001110] * 8)
        self.assertEqual(
            card_fingerprint.search_variants([a, b], index),
            card_fingerprint.search_variants([b, a], index),
        )

    def test_one_variant_is_exactly_the_single_query_search(self):
        index = self._index(
            bytes([0b00000000] * 8), bytes([0b00001111] * 8), bytes([0b11111111] * 8)
        )
        query = bytes([0b00000011] * 8)
        self.assertEqual(
            card_fingerprint.search_variants([query], index, limit=2),
            search(query, index, limit=2),
        )

    def test_the_distance_cutoff_still_applies_to_the_merged_ranking(self):
        index = self._index(bytes([0b00000000] * 8), bytes([0b11111111] * 8))
        query = bytes([0b00000000] * 8)
        ranked = card_fingerprint.search_variants(
            [query, query], index, max_distance=SHORTLIST_MAX_DISTANCE
        )
        self.assertEqual([row for row, _ in ranked], [0])

    def test_no_queries_and_an_empty_index_both_return_nothing(self):
        index = self._index(bytes(8))
        self.assertEqual(card_fingerprint.search_variants([], index), [])
        self.assertEqual(
            card_fingerprint.search_variants([bytes(8)], np.empty((0, 8), np.uint8)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
