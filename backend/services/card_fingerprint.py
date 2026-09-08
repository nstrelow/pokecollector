"""Offline card recognition by perceptual image fingerprint.

The scanner has always needed a vision LLM before it could do anything: the
prompt reads the printed name, the name is searched against the live TCGdex API,
and only then can the deterministic ranker in `api/recognize.py` do its work. If
no key is configured, or the name is unreadable, the scan fails outright.

This module provides the missing half. It fingerprints every catalogue image
once at sync time, then matches a photo against the whole catalogue with a
Hamming scan. On a 42k-card catalogue that search costs ~3ms and the whole index
is ~330KB in memory, so no narrowing step is needed before it.

It deliberately uses only numpy and Pillow, both already required by the backend,
so enabling it adds no dependency and no image weight.

Measured on a 42,254-card catalogue (en/de/ja/zh-tw) against 7,098 degraded
photographs, with the correct card ranked inside the returned shortlist:

    without cropping   33.6%
    with cropping      91.6%

The gap is entirely about framing: hashing a whole photograph fails as soon as
the card does not fill the frame, which is why `crop_to_card` exists.
"""
from __future__ import annotations

import io
import logging
import warnings
from functools import lru_cache

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

HASH_BITS = 64
HASH_BYTES = HASH_BITS // 8

# A photograph is only worth trusting when the nearest catalogue entry is both
# close and clearly closer than the runner-up. These mirror the thresholds the
# existing pHash tiebreak in api/recognize.py already uses.
MAX_DISTANCE = 20
MIN_MARGIN = 5

MAX_PIXELS = 50_000_000

# --- card detection -------------------------------------------------------
_WORK_WIDTH = 320
_ENERGY_FLOOR_PERCENTILE = 72.0
_ENERGY_TAIL = 0.02
_MIN_AREA_FRACTION = 0.06
_MIN_ASPECT, _MAX_ASPECT = 0.35, 1.35
# Above this fraction of the frame there is no background worth removing, and
# cropping further would only desynchronise the photo from the catalogue render.
_ALREADY_TIGHT = 0.72


@lru_cache(maxsize=1)
def _dct_matrix(size: int = 32) -> np.ndarray:
    """Unnormalised DCT-II matrix, matching imagehash.phash without SciPy."""
    positions = np.arange(size)
    frequencies = np.arange(size)[:, None]
    return 2 * np.cos(np.pi * frequencies * (2 * positions + 1) / (2 * size))


def _box_blur(a: np.ndarray, radius: int = 3) -> np.ndarray:
    """Separable moving average using prefix sums."""
    out = a
    for axis in (0, 1):
        n = out.shape[axis]
        window = min(2 * radius + 1, n if n % 2 else n - 1)
        if window < 3:
            continue
        pad = window // 2
        padded = np.pad(
            out, [(pad, pad) if i == axis else (0, 0) for i in range(2)], mode="edge"
        )
        zeros = np.zeros([1 if i == axis else padded.shape[i] for i in range(2)])
        cumulative = np.cumsum(np.concatenate([zeros, padded], axis=axis), axis=axis)
        lo = np.take(cumulative, np.arange(0, n), axis=axis)
        hi = np.take(cumulative, np.arange(window, window + n), axis=axis)
        out = (hi - lo) / window
    return out


def _energy_span(profile: np.ndarray, tail: float = _ENERGY_TAIL) -> tuple[int, int]:
    """Smallest span holding all but `tail` of the profile's mass at each end.

    Trimming by mass rather than thresholding matters: a card's artwork window
    is far more textured than its text half, so a threshold latches onto the
    artwork alone and crops away the bottom of the card.
    """
    total = float(profile.sum())
    if total <= 0:
        return 0, len(profile) - 1
    cumulative = np.cumsum(profile) / total
    lo = int(np.searchsorted(cumulative, tail))
    hi = int(np.searchsorted(cumulative, 1.0 - tail))
    return min(lo, len(profile) - 1), min(hi, len(profile) - 1)


def find_card_box(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the card within a photo, in original coordinates.

    The card is the textured region; a table or desk is comparatively smooth.
    That assumption fails when other cards share the frame, in which case the
    box spans several of them -- callers should treat a low-confidence match as
    "ask the user" rather than trusting it.
    """
    w, h = img.size
    if w < 32 or h < 32:
        return None
    scale = _WORK_WIDTH / w
    small = img.convert("L").resize(
        (_WORK_WIDTH, max(8, int(h * scale))), Image.BILINEAR
    )
    a = np.asarray(small, dtype=float) / 255.0

    gy, gx = np.gradient(a)
    energy = _box_blur(np.hypot(gx, gy))

    # Subtract the background's own texture level so a noisy surface adds no mass.
    floor = np.percentile(energy, _ENERGY_FLOOR_PERCENTILE)
    energy = np.clip(energy - floor, 0.0, None)
    if energy.sum() <= 0:
        return None

    x0, x1 = _energy_span(energy.sum(axis=0))
    y0, y1 = _energy_span(energy.sum(axis=1))
    if x1 <= x0 or y1 <= y0:
        return None

    small_h = small.size[1]
    box = (
        max(0, int(x0 / _WORK_WIDTH * w)),
        max(0, int(y0 / small_h * h)),
        min(w, int((x1 + 1) / _WORK_WIDTH * w)),
        min(h, int((y1 + 1) / small_h * h)),
    )
    bw, bh = box[2] - box[0], box[3] - box[1]
    if bw <= 0 or bh <= 0:
        return None
    if (bw * bh) / float(w * h) < _MIN_AREA_FRACTION:
        return None
    if not (_MIN_ASPECT <= bw / bh <= _MAX_ASPECT):
        return None
    return box


def crop_to_card(img: Image.Image) -> Image.Image:
    """Crop a photo to the card, or leave it alone if it is already card-only."""
    box = find_card_box(img)
    if box is None:
        return img
    w, h = img.size
    area = (box[2] - box[0]) * (box[3] - box[1]) / float(w * h)
    if area >= _ALREADY_TIGHT:
        return img
    return img.crop(box)


# --- fingerprints ---------------------------------------------------------

def _hash_bits(img: Image.Image) -> np.ndarray:
    pixels = np.asarray(
        img.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=float
    )
    transform = _dct_matrix(32)
    low = (transform @ pixels @ transform.T)[:8, :8]
    return (low > np.median(low)).flatten()


def _open(image_bytes: bytes) -> Image.Image | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as im:
                width, height = im.size
                if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                    return None
                return im.convert("RGB").copy()
    except Exception:
        return None


def fingerprint_reference(image_bytes: bytes) -> bytes | None:
    """Fingerprint a catalogue render.

    Deliberately does NOT crop. Catalogue images are already tight crops, and
    running detection over them trims roughly a fifth of their area, which
    desynchronises the index from itself and costs more accuracy than it buys.
    """
    img = _open(image_bytes)
    if img is None:
        return None
    return np.packbits(_hash_bits(img).astype(np.uint8)).tobytes()


def fingerprint_photo(image_bytes: bytes) -> bytes | None:
    """Fingerprint a user photograph, cropping to the card first."""
    img = _open(image_bytes)
    if img is None:
        return None
    return np.packbits(_hash_bits(crop_to_card(img)).astype(np.uint8)).tobytes()


# --- search ---------------------------------------------------------------

_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def distances(query: bytes, index: np.ndarray) -> np.ndarray:
    """Hamming distance from one fingerprint to every row of a packed index."""
    q = np.frombuffer(query, dtype=np.uint8)
    return _POPCOUNT[np.bitwise_xor(index, q)].sum(axis=1)


def search(query: bytes, index: np.ndarray, limit: int = 12) -> list[tuple[int, int]]:
    """Return [(row, distance)] for the `limit` closest catalogue entries."""
    if index.size == 0:
        return []
    d = distances(query, index)
    take = min(limit, d.shape[0])
    order = np.argsort(d, kind="stable")[:take]
    return [(int(i), int(d[i])) for i in order]


def is_confident(ranked: list[tuple[int, int]]) -> bool:
    """Whether the best match is close enough, and clear enough of the rest."""
    if len(ranked) < 2:
        return bool(ranked) and ranked[0][1] <= MAX_DISTANCE
    best, runner_up = ranked[0][1], ranked[1][1]
    return best <= MAX_DISTANCE and (runner_up - best) >= MIN_MARGIN
