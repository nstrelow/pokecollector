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
photographs. "Correct" has two meanings here and they differ by roughly nine
points, so both are always stated:

                                        artwork   exact printing
    correct in the top-12 shortlist      91.62%          90.60%
    correct at rank 1                    70.05%          61.50%

"Artwork" means the same picture, whatever language it was printed in; "exact
printing" means the same catalogue row (`id`, so language-specific). The gap is
almost entirely reprints and localisations of one illustration, which are
byte-different renders of the same art and therefore genuinely indistinguishable
to a perceptual hash. The shortlist exists so the user picks the printing.

Cropping is what makes any of it work. With the whole photograph hashed and no
`crop_to_card`, artwork-in-top-12 falls from 91.62% to 33.6%: hashing a whole
photograph fails as soon as the card does not fill the frame.

But *whether* to crop cannot be decided by a threshold on how much of the frame
the card fills, which is what the first version of this did. The first real
photograph ever put through it -- hand-held, card filling 0.768 of the frame --
was left uncropped and its true match landed at rank 48 of 44,466; cropped, the
same photo puts it at rank 1. No threshold fixes both that photo and the
synthetic set, so `photo_hash_variants` stops choosing and returns both
fingerprints, and `search_variants` ranks against both. That trades 0.66 points
of synthetic shortlist recall (91.62% -> 90.96%) for the badly framed photos a
single fingerprint loses outright, at slightly better confident precision.
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
# therefore an inert number pretending to be a safety bound.
#
# 12 is lower, and costs no measured decision, but be honest about how little it
# buys: a synthetic non-card upload lands 12 to 20 bits from its nearest of
# 39,254 catalogue rows, so the noise floor STARTS at exactly 12 and a
# distance-12 coincidence still clears this bound. MAX_DISTANCE excludes the
# upper four fifths of the floor, not the floor. What actually rejects nonsense
# is MIN_MARGIN, which fired on 0 of 400 synthetic non-card uploads.
#
# MIN_MARGIN stays at 5. Raising it to 8 removes 7 of the 11 wrong confident
# calls but also 455 of the 1,095 right ones, and a confident result is still
# presented for review alongside the shortlist, so coverage is worth more here
# than the last 0.4 points of precision. All 11 of those wrong calls were the
# right artwork in the wrong language: confident precision is 99.01% on the
# exact printing and 100% on the artwork.
MAX_DISTANCE = 12
MIN_MARGIN = 5

# Entries further than this are dropped from the shortlist entirely, so a
# shortlist can legitimately come back empty. Derived from the same run: across
# the 6,431 photos whose exact printing reached the top 12 (90.60% of 7,098),
# that row was never further than 20 bits away (median 4, p99 16), so 24 costs
# no recall at all.
#
# Be honest about what this does NOT do. At 64 bits over 42k rows there is a
# noise floor: a pure-noise upload still finds a nearest neighbour at distance
# 12-20, exactly where genuine hard matches live, so no distance cutoff can
# separate the two. This drops only grossly out-of-range entries. The guard
# against acting on nonsense is `is_confident`'s margin test, which fired on 0
# of 400 synthetic non-card uploads.
SHORTLIST_MAX_DISTANCE = 24

MAX_PIXELS = 50_000_000

# What a distance is worth as a probability, for the percentage the review UI
# prints on each candidate.
#
# Hamming distance is not a percentage and the obvious conversion is a lie:
# (HASH_BITS - d) / HASH_BITS calls a pure-noise match "78%", because a 64-bit
# hash over 42k rows puts a photo of nothing 12-20 bits from its nearest
# neighbour. So this is measured instead -- P(candidate is the right artwork |
# its distance), over every shortlist entry the production path showed for
# 7,098 degraded photographs.
#
# Split by rank, because the two are wildly different propositions and a single
# curve would be wrong in both directions. At distance 10 the leader is right
# 43% of the time and a tail entry 4%:
#
#   distance      0      2      4      6      8     10     12     14     16
#   rank 1    91.5%  84.4%  78.9%  75.5%  62.0%  43.1%  28.9%  15.2%   0.0%
#   rest      44.4%  43.5%  29.5%  21.9%   9.3%   4.4%   3.0%   2.0%   1.2%
#
# Only even distances occur, so odd inputs interpolate. Re-measure with
# pokecard-bench/calibrate_distance.py if the hash or the shortlist changes.
_LEADER_PROBABILITY = {
    0: 0.915, 2: 0.844, 4: 0.789, 6: 0.755, 8: 0.620,
    10: 0.431, 12: 0.289, 14: 0.152, 16: 0.02, 18: 0.01,
}
_RUNNER_UP_PROBABILITY = {
    0: 0.444, 2: 0.435, 4: 0.295, 6: 0.219, 8: 0.093,
    10: 0.044, 12: 0.030, 14: 0.020, 16: 0.012, 18: 0.003,
}


def match_probability(distance: int, *, leader: bool) -> int:
    """Measured chance this candidate is the right artwork, as a percentage.

    `leader` distinguishes the top of the shortlist from the rest of it; see
    `_LEADER_PROBABILITY` for why that is not a detail.
    """
    table = _LEADER_PROBABILITY if leader else _RUNNER_UP_PROBABILITY
    known = sorted(table)
    if distance <= known[0]:
        return round(table[known[0]] * 100)
    if distance >= known[-1]:
        return round(table[known[-1]] * 100)
    for lower, upper in zip(known, known[1:]):
        if lower <= distance <= upper:
            span = upper - lower
            weight = (distance - lower) / span
            blended = table[lower] + (table[upper] - table[lower]) * weight
            return round(blended * 100)
    return 0

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
#
# Raising this was tried and is wrong, by a lot. Swept over the 7,098-photo
# multilingual set (artwork in the 12-item shortlist):
#
#   _ALREADY_TIGHT   0.60    0.66    0.72    0.78    0.84    0.90    0.96
#   artwork in 12  92.36%  92.25%  91.62%  87.01%  80.09%  79.28%  79.28%
#   confident       16.0%   16.0%   15.6%   13.1%    8.7%    6.9%    6.7%
#   precision      99.03%  99.03%  99.01%  98.71%  96.94%  95.88%  95.55%
#
# Cropping more is not "more careful framing"; past ~0.72 the energy box starts
# trimming into the card itself, and a 4% over-trim is enough to move a true
# match from rank 1 to rank 42. Lower is mildly better on this data, but this
# constant no longer decides what the scanner searches -- `photo_hash_variants`
# returns both sides regardless -- so it is left where it was benchmarked rather
# than retuned for a path nothing takes.
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

    Callers normally hand us an image that `_open` has already normalised to
    RGB, and for `fingerprint_reference`/`fingerprint_photo` that normalisation
    is part of the definition -- not because it changes the luma, but because
    every fingerprint already in every install's database was computed after
    it, and `convert("RGB")` also gives back a fully materialised image that no
    longer depends on the source file handle. It was previously claimed here
    that "Pillow's direct CMYK->L is not a luma conversion at all"; that is
    false. Measured on Pillow 12.3, `im.convert("L")` and
    `im.convert("RGB").convert("L")` are pixel-identical (max absolute
    difference 0) for RGB, CMYK, P (adaptive, web and transparency palettes),
    RGBA including fully transparent pixels, LA, L, 1, I, I;16, F and HSV. Only
    YCbCr differs, by at most 1.

    `hash_bits(mode="L")` -- used only by api/recognize.py's `_perceptual_hash`,
    whose output is never persisted -- hands this an image `_open` already
    decoded straight to L instead. Because of the equivalence above that is a
    memory optimisation, not a different definition: `img.convert("L")` here
    is then a same-mode copy and produces the identical bits, for every mode
    this app has been shown to receive except a source that is natively
    YCbCr (rare TIFF variants; never a photo upload or a TCGdex render in
    practice) -- see `test_l_and_rgb_first_decodes_hash_identically`.
    """
    pixels = np.asarray(
        img.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=float
    )
    transform = _dct_matrix(32)
    low = (transform @ pixels @ transform.T)[:8, :8]
    return (low > np.median(low)).flatten()


def _safe_hash_bits(img: Image.Image) -> np.ndarray | None:
    """`_hash_bits` with the same "unusable input is None, never an exception"
    contract as `_open`.

    Decoding is not the only step that can fail on a hostile or merely enormous
    upload: the 32x32 LANCZOS resize allocates, and MemoryError or a truncated
    -image OSError surfacing here used to escape to the caller from
    `hash_bits`, `fingerprint_reference` and `fingerprint_photo` alike, even
    though all three document a None return. One rule for both halves.
    """
    try:
        return _hash_bits(img)
    except Exception:
        logger.debug("fingerprint: hashing a decoded image failed", exc_info=True)
        return None


def hash_bits(
    image_bytes: bytes | None,
    max_pixels: int | None = None,
    *,
    mode: str = "RGB",
) -> np.ndarray | None:
    """Decode `image_bytes` and return its 64 pHash bits, or None if unusable.

    `mode` is "RGB" for every persisted use. api/recognize.py's
    `_perceptual_hash` -- an ephemeral in-memory tiebreak, never written to
    `cards.image_phash` -- passes `mode="L"` instead, which skips materialising
    a full RGB copy before `_hash_bits` reduces it to L anyway. See `_open` and
    `_hash_bits` for why that is memory-only and not a different hash.
    """
    if not image_bytes:
        return None
    img = _open(image_bytes, max_pixels, mode=mode)
    if img is None:
        return None
    return _safe_hash_bits(img)


def _open(
    image_bytes: bytes, max_pixels: int | None = None, *, mode: str = "RGB"
) -> Image.Image | None:
    """Decode to a materialised image in `mode`, or None if unusable or huge.

    No trailing `.copy()`. `im.convert(mode)` already calls `load()` and
    returns a new, independent image, so the copy was a second full-resolution
    buffer for nothing. Measured peak RSS for one 48-megapixel PNG through the
    full hash: +277MB converting straight to L, +415MB via RGB, +550MB via RGB
    plus the copy. api/recognize.py hashes up to 8 reference images in sequence
    and `fingerprint_cards` runs 4 of these concurrently, so the copy was worth
    roughly half a gigabyte of transient peak at MAX_REFERENCE_IMAGE_PIXELS.

    `mode` defaults to RGB because `fingerprint_reference` and
    `fingerprint_photo` -- whose output is persisted to `cards.image_phash` --
    must keep asking for exactly what every already-stored fingerprint was
    computed from. Only `hash_bits(mode="L")` passes anything else, recovering
    the +415MB-vs-277MB gap above for the one caller whose result is never
    persisted. See `_hash_bits` for why that does not change the hash.
    """
    # Resolved at call time, not bound as a default, so tests can patch the cap.
    max_pixels = MAX_PIXELS if max_pixels is None else max_pixels
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as im:
                width, height = im.size
                if width <= 0 or height <= 0 or width * height > max_pixels:
                    return None
                return im.convert(mode)
    except Exception:
        return None


def _pack_bits(bits: np.ndarray) -> bytes:
    """The one way 64 hash bits become the 8 bytes stored and compared."""
    return np.packbits(bits.astype(np.uint8)).tobytes()


def fingerprint_reference(image_bytes: bytes) -> bytes | None:
    """Fingerprint a catalogue render.

    Deliberately does NOT crop. Catalogue images are already tight crops, and
    running detection over them trims roughly a fifth of their area, which
    desynchronises the index from itself and costs more accuracy than it buys.
    """
    img = _open(image_bytes)
    if img is None:
        return None
    bits = _safe_hash_bits(img)
    if bits is None:
        return None
    return _pack_bits(bits)


def photo_hash_variants(image_bytes: bytes) -> list[bytes]:
    """Every fingerprint of one photograph worth searching, best guess first.

    Cropping to the detected card is what makes this work at all -- uncropped,
    shortlist recall collapses from 91.6% to 33.6%. But `crop_to_card` has to
    decide whether a photo still has background worth removing, and that
    decision is a threshold on a continuum. A photo landing near it is not a
    close call the threshold gets slightly wrong; it is the difference between
    rank 1 and rank 48, because a 64-bit hash of a card plus a fifth of a frame
    of fingers and tablecloth is not a noisy version of the card's hash, it is
    a different hash.

    So both sides are returned and both are searched, whichever side of
    `_ALREADY_TIGHT` the photo falls on. Order is `crop_to_card`'s answer first
    and is what `fingerprint_photo` reads; `search_variants` scores a row by its
    best distance over the list, so for the shortlist the order is immaterial.
    """
    img = _open(image_bytes)
    if img is None:
        return []
    try:
        box = find_card_box(img)
    except Exception:
        logger.debug("fingerprint: card detection failed", exc_info=True)
        return []

    candidates = [img]
    if box is not None:
        width, height = img.size
        area = (box[2] - box[0]) * (box[3] - box[1]) / float(width * height)
        cropped = img.crop(box)
        candidates = [img, cropped] if area >= _ALREADY_TIGHT else [cropped, img]

    hashes: list[bytes] = []
    for candidate in candidates:
        bits = _safe_hash_bits(candidate)
        if bits is None:
            continue
        packed = _pack_bits(bits)
        # A crop that changed nothing measurable is not a second opinion.
        if packed not in hashes:
            hashes.append(packed)
    return hashes


def fingerprint_photo(image_bytes: bytes) -> bytes | None:
    """The single leading fingerprint of a user photograph.

    Defined in terms of `photo_hash_variants` so the two cannot disagree about
    which crop leads.
    """
    variants = photo_hash_variants(image_bytes)
    return variants[0] if variants else None


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
    return _rank(distances(query, index), limit, max_distance)


def _rank(
    d: np.ndarray, limit: int, max_distance: int | None
) -> list[tuple[int, int]]:
    """The `limit` closest rows of a precomputed distance vector."""
    n = int(d.shape[0])
    take = min(limit, n)
    if take <= 0:
        return []
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


def search_variants(
    queries: list[bytes],
    index: np.ndarray,
    limit: int = 12,
    max_distance: int | None = None,
) -> list[tuple[int, int]]:
    """Rank one photo's fingerprint variants against the index as one shortlist.

    A row is scored by its *best* distance over the variants. That lowers every
    row's distance, not just the right one's, so the shortlist is not a superset
    of the single-query one: a decoy the second variant likes can displace the
    true card from twelve slots. Measured, that costs 0.66 points of shortlist
    recall on synthetic photos (91.62% -> 90.96%) and buys back the badly framed
    real ones, which the single query loses outright. Alternatives were measured
    and are worse: switching wholesale to whichever variant landed closer scores
    90.48%, and requiring it to land k bits closer falls away from there
    (86.86% at k=1, 72.57% at k=5).

    The scan is a linear pass over 338KB of packed hashes (~3ms) against ~25ms
    of JPEG decode that already dominates, so a second one is close to free.

    Confidence is judged on this merged ranking rather than on one variant. That
    is the conservative reading -- a wrong card that only one variant finds
    plausible still narrows the margin, and the margin is the whole gate -- and
    it measures out that way: confident precision 99.10% against the single
    query's 99.01%, on slightly more decisions (15.71% vs 15.58%).
    """
    if not queries:
        return []
    if index.size == 0:
        return []
    if len(queries) == 1:
        return _rank(distances(queries[0], index), limit, max_distance)
    # Accumulate in whatever dtype `distances` produces. It is unsigned (a
    # uint8 popcount summed along a row), and mixing that with a signed
    # accumulator makes numpy promote the pair to float64, which then refuses
    # to be written back into an integer `out=`.
    best = distances(queries[0], index)
    for query in queries[1:]:
        np.minimum(best, distances(query, index), out=best)
    return _rank(best, limit, max_distance)


def is_confident(ranked: list[tuple[int, int]]) -> bool:
    """Whether the best match is close enough, and clear enough of the rest.

    `ranked` must be a shortlist that was cut at SHORTLIST_MAX_DISTANCE over a
    real catalogue-sized index. A single surviving entry then means every other
    row was more than SHORTLIST_MAX_DISTANCE away -- an enormous margin -- so it
    is judged on distance alone. That reasoning does NOT hold when the index is
    tiny, because then a lone entry only means there was nothing else to rank
    against. Callers are responsible for not asking about an index too small to
    be meaningful; api/recognize_local.py refuses to answer at all until
    fingerprint_index.coverage() reports ready.
    """
    if len(ranked) < 2:
        return bool(ranked) and ranked[0][1] <= MAX_DISTANCE
    best, runner_up = ranked[0][1], ranked[1][1]
    return best <= MAX_DISTANCE and (runner_up - best) >= MIN_MARGIN
