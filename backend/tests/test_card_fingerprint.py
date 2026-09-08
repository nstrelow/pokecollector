import io
import os
import sys
import unittest

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.card_fingerprint import (  # noqa: E402
    HASH_BYTES,
    MAX_DISTANCE,
    crop_to_card,
    find_card_box,
    fingerprint_photo,
    fingerprint_reference,
    is_confident,
    search,
)


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

    def test_different_cards_are_far_apart(self):
        a = fingerprint_reference(_to_bytes(_card(seed=2)))
        b = fingerprint_reference(_to_bytes(_card(seed=3)))
        distance = np.count_nonzero(
            np.unpackbits(np.frombuffer(a, dtype=np.uint8))
            != np.unpackbits(np.frombuffer(b, dtype=np.uint8))
        )
        self.assertGreater(distance, MAX_DISTANCE)

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

    def test_an_already_tight_image_is_left_alone(self):
        card = _card(seed=5)
        self.assertEqual(crop_to_card(card).size, card.size)

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

    def test_search_on_empty_index_is_safe(self):
        empty = np.empty((0, HASH_BYTES), dtype=np.uint8)
        self.assertEqual(search(fingerprint_reference(_to_bytes(_card())), empty), [])

    def test_confidence_needs_distance_and_margin(self):
        self.assertTrue(is_confident([(0, 2), (1, 14)]))
        # Close to the top match, so the two cannot be told apart.
        self.assertFalse(is_confident([(0, 2), (1, 4)]))
        # Clear margin, but nothing is actually similar.
        self.assertFalse(is_confident([(0, 30), (1, 60)]))


if __name__ == "__main__":
    unittest.main()
