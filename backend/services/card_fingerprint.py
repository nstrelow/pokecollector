"""Offline card recognition by perceptual image fingerprint.

The scanner has always needed a vision LLM before it could do anything: the
prompt reads the printed name, the name is searched against the live TCGdex API,
and only then can the deterministic ranker in `api/recognize.py` do its work. If
no key is configured, or the name is unreadable, the scan fails outright.

This module provides the missing half. It fingerprints every catalogue image
once, then matches a photo against the whole catalogue with a Hamming scan.

Where the time actually goes, per scan of a 1500x2000 photo against a
45,741-card index (one core of an i9-13900H; treat the ratios, not the absolute
numbers, as the finding):

    decode the upload                     ~27ms
    find_card_box                         ~17ms
    32x32 DCT hash                        ~10ms
    search() over the whole index          ~3ms   (3.0ms of it the Hamming scan)

The linear scan therefore needs no narrowing step in front of it -- but that is
because per-photo work dominates it by roughly fifteen to one, not because the
scan is free. `search` uses argpartition rather than a full sort for that 3ms;
a full argsort of 45k elements to keep 12 cost 5.7ms, nearly doubling it.

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
# close and clearly closer than the runner-up.
#
# These were originally copied from api/recognize.py, where they arbitrate among
# at most 8 LLM-shortlisted candidates. Re-derived here for a 42,254-row
# nearest-neighbour scan, sweeping both against 7,098 degraded photographs:
#
#   MIN_MARGIN     3       4       5       6       8      10
#   confident   28.5%   28.5%   15.6%   15.6%    9.2%    4.9%
#   precision   96.5%   96.5%   99.0%   99.0%   99.4%  100.0%
#
# The margin is the whole decision; MAX_DISTANCE barely participates. Every
# value from 10 to 24 gives the identical 15.58% at 99.01%, because no photo
# that clears the margin test is ever further than 10 bits away. 20 was
# therefore an inert number pretending to be a safety bound. 12 is tightened to
# where it can actually bind without costing a single measured decision, which
# matters for inputs the benchmark does not contain: a synthetic non-card upload
# lands at distance 12-20 from its nearest of 39,254 catalogue rows, so the old
# bound of 20 admitted the entire noise floor.
#
# MIN_MARGIN stays at 5. Raising it to 8 removes 7 of the 11 wrong confident
# calls but also 455 of the 1,095 right ones, and a confident result is still
# presented for review alongside the shortlist, so coverage is worth more here
# than the last 0.4 points of precision.
MAX_DISTANCE = 12
MIN_MARGIN = 5

# Entries further than this are dropped from the shortlist entirely, so a
# shortlist can legitimately come back empty. Derived from the same run: across
# the 6,431 photos whose correct card reached the top 12, that card was never
# further than 20 bits away (median 4, p99 16), so 24 costs no recall at all.
#
# Be honest about what this does NOT do. At 64 bits over 42k rows there is a
# noise floor: a pure-noise upload still finds a nearest neighbour at distance
# 12-20, exactly where genuine hard matches live, so no distance cutoff can
# separate the two. This drops only grossly out-of-range entries. The guard
# against acting on nonsense is `is_confident`'s margin test, which fired on 0
# of 400 synthetic non-card uploads.
SHORTLIST_MAX_DISTANCE = 24

MAX_PIXELS = 50_000_000

# --- card detection -------------------------------------------------------
_WORK_WIDTH = 320
# Detection runs on a downscale of the photo. Scaling by width alone turns a
# tall, narrow input into an *upscale*: sanitize_image_bytes caps the longest
# edge at 2048 but not the aspect ratio, so a 32x2048 upload became a
# 320x20480 float array (0.6s, 580MB peak) -- a cheap denial of service. Cap
# the working height too, so the analysed array is at most 320x640 whatever
# the input. Cards are ~1.4:1, so no realistic photo reaches this limit and
# behaviour for them is unchanged.
_WORK_MAX_HEIGHT = 2 * _WORK_WIDTH
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


def _work_size(w: int, h: int) -> tuple[int, int]:
    """Size to analyse a `w` x `h` photo at, bounded on both axes.

    Fits the frame inside _WORK_WIDTH x _WORK_MAX_HEIGHT. For anything up to
    2:1 portrait -- every real card photo -- this is exactly the old
    "width becomes 320" rule, so detection results are unchanged.
    """
    if _WORK_WIDTH / w <= _WORK_MAX_HEIGHT / h:
        return _WORK_WIDTH, max(8, int(h * (_WORK_WIDTH / w)))
    return max(8, int(w * (_WORK_MAX_HEIGHT / h))), _WORK_MAX_HEIGHT


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
    small_w, small_h = _work_size(w, h)
    small = img.convert("L").resize((small_w, small_h), Image.BILINEAR)
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

    box = (
        max(0, int(x0 / small_w * w)),
        max(0, int(y0 / small_h * h)),
        min(w, int((x1 + 1) / small_w * w)),
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
    """The one definition of this app's 64-bit pHash.

    api/recognize.py's `_perceptual_hash` delegates here, so the LLM scanner's
    tiebreak and the persisted `cards.image_phash` can never drift apart.

    Note the deliberate RGB-then-luma path: callers hand us an image that
    `_open` already converted to RGB, so a palette or CMYK source is normalised
    through RGB rather than converted straight to L. Pillow's direct CMYK->L is
    not a luma conversion at all, and the persisted column was computed this
    way, so RGB-first is the definition we keep.
    """
    pixels = np.asarray(
        img.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=float
    )
    transform = _dct_matrix(32)
    low = (transform @ pixels @ transform.T)[:8, :8]
    return (low > np.median(low)).flatten()


def hash_bits(
    image_bytes: bytes | None, max_pixels: int | None = None
) -> np.ndarray | None:
    """Decode `image_bytes` and return its 64 pHash bits, or None if unusable."""
    if not image_bytes:
        return None
    img = _open(image_bytes, max_pixels)
    if img is None:
        return None
    return _hash_bits(img)


def _open(image_bytes: bytes, max_pixels: int | None = None) -> Image.Image | None:
    # Resolved at call time, not bound as a default, so tests can patch the cap.
    max_pixels = MAX_PIXELS if max_pixels is None else max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as im:
                width, height = im.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
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


@lru_cache(maxsize=4)
def _row_ids(n: int) -> np.ndarray:
    """Cached 0..n-1, reused as the tiebreak half of the ranking key."""
    ids = np.arange(n, dtype=np.int64)
    ids.setflags(write=False)
    return ids


def distances(query: bytes, index: np.ndarray) -> np.ndarray:
    """Hamming distance from one fingerprint to every row of a packed index."""
    q = np.frombuffer(query, dtype=np.uint8)
    return _POPCOUNT[np.bitwise_xor(index, q)].sum(axis=1)


def search(
    query: bytes,
    index: np.ndarray,
    limit: int = 12,
    max_distance: int | None = None,
) -> list[tuple[int, int]]:
    """Return [(row, distance)] for the `limit` closest catalogue entries.

    Entries further than `max_distance` are dropped, so a photo of something
    that is not in the catalogue can legitimately return nothing instead of a
    dozen confident-looking strangers.
    """
    if index.size == 0:
        return []
    d = distances(query, index)
    n = int(d.shape[0])
    take = min(limit, n)
    # A full argsort of 45k elements to keep 12 costs more than the distance
    # scan itself. argpartition is O(n); the composite (distance, row) key
    # keeps ties resolved by the lower row index, exactly as the stable full
    # sort did, so ranking stays deterministic.
    key = d.astype(np.int64)
    key <<= 32
    key |= _row_ids(n)
    picked = np.argpartition(key, take - 1)[:take] if take < n else np.arange(n)
    order = picked[np.argsort(key[picked])]
    ranked = [(int(i), int(d[i])) for i in order]
    if max_distance is not None:
        ranked = [pair for pair in ranked if pair[1] <= max_distance]
    return ranked


def is_confident(ranked: list[tuple[int, int]]) -> bool:
    """Whether the best match is close enough, and clear enough of the rest."""
    if len(ranked) < 2:
        return bool(ranked) and ranked[0][1] <= MAX_DISTANCE
    best, runner_up = ranked[0][1], ranked[1][1]
    return best <= MAX_DISTANCE and (runner_up - best) >= MIN_MARGIN
