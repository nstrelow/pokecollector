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
# its distance and its band), over every shortlist entry the production path
# showed for 7,098 degraded photographs.
#
# Two things decide it, not one. An earlier version split the shortlist into
# "rank 1" and "the rest" and gave every row sharing the best distance the
# rank-1 curve. Those are not the same population: that curve was measured on
# one row per photo, whichever won an arbitrary row-index tiebreak, while a
# shortlist's best distance is frequently shared. The result was that the
# twelve badges summed to more than 100% on 78.7% of shortlists -- mean 170%,
# worst observed 1008%.
#
# So the second condition is HOW MANY TILES SHARE THE BEST DISTANCE. A k-way
# tie is a k-way split of one piece of evidence: at most one of those tiles is
# the right artwork, so k tiles cannot each be 92% likely. Measured that way,
# over the shortlists the production path shows for 7,098 degraded photographs
# (percentages are P(this tile is the right artwork); "--" is fewer than 30
# observations and is not stored):
#
#   distance        0      2      4      6      8     10     12     14     16
#   k = 1       98.9%  96.0%  90.9%  84.0%  71.3%  57.1%  44.1%  27.3%    --
#   k = 2       50.0%  48.1%  46.7%  45.4%  41.3%  32.9%  26.0%  22.5%    --
#   k = 3       32.3%  32.3%  30.6%  30.6%  22.9%  20.4%  16.5%  16.5%    --
#   k = 4       25.0%     --     --     --  18.3%  15.6%  13.0%   9.3%    --
#   k >= 5      12.3%  12.3%  11.9%  11.9%   9.5%   7.2%   5.9%   5.9%  3.9%
#   not leading  --     4.4%   4.1%   3.3%   2.1%   1.0%   0.8%   0.8%  0.6%
#
# k times the leading value never exceeds 100% anywhere in that table, which is
# the property the old one lacked.
#
# MEASURED ON THE GROUPED SHORTLIST. api/recognize_local.py collapses the
# language printings of one card into a single tile, so k counts distinct
# artworks the hash could not separate, not rows -- and the trailing values are
# far lower than they used to be precisely because a trailing tile is now a
# genuinely different picture rather than the same one again.
#
# Stored values are the closest non-increasing fit to the measurements
# (pool-adjacent-violators, weighted by observation count). The k = 2 and k = 4
# rows were already monotone; k = 1, 3 and 5 each had one upward wobble on a
# few hundred observations, and a badge that scores a further card higher than
# a nearer one is worse than a slightly smoothed one.
#
# Where a row's observations run out it defers to the next WIDER tie rather
# than clamping to its own last value: a unique leader at distance 16 was seen
# twice, both times wrong, and clamping would have printed 27% for it.
#
# Only even distances occur -- both hashes have exactly 32 set bits, so their
# XOR has even weight -- so odd inputs interpolate. Re-measure with
# pokecard-bench/calibrate_distance.py if the hash or the shortlist changes.
MAX_TIE_BAND = 5

_LEADING_TILE_PROBABILITY = {
    1: {0: 0.989, 2: 0.960, 4: 0.909, 6: 0.840, 8: 0.713,
        10: 0.571, 12: 0.441, 14: 0.273},
    2: {0: 0.500, 2: 0.481, 4: 0.467, 6: 0.454, 8: 0.413,
        10: 0.329, 12: 0.260, 14: 0.225},
    3: {0: 0.323, 2: 0.323, 4: 0.306, 6: 0.306, 8: 0.229,
        10: 0.204, 12: 0.165, 14: 0.165},
    4: {0: 0.250, 8: 0.183, 10: 0.156, 12: 0.130, 14: 0.093},
    5: {0: 0.123, 2: 0.123, 4: 0.119, 6: 0.119, 8: 0.095,
        10: 0.072, 12: 0.059, 14: 0.059, 16: 0.039},
}
_TRAILING_TILE_PROBABILITY = {
    2: 0.044, 4: 0.041, 6: 0.033, 8: 0.021, 10: 0.010,
    12: 0.008, 14: 0.008, 16: 0.006, 18: 0.002,
}


def match_leader_counts(shortlist: list[tuple[int, int]]) -> list[int]:
    """Per entry: how many entries share the best distance, or 0 if it doesn't.

    Feed this the shortlist as the user will see it -- grouped, one tile per
    artwork -- because that is the population `_LEADING_TILE_PROBABILITY` was
    measured on and the set of tiles whose badges have to add up.

    "Leader" has to mean *strictly* closest, and when nothing is strictly
    closest, nothing is the leader. Two tiles at the same distance are two the
    hash cannot tell apart, and which lands at rank 1 is decided by row index,
    which carries no evidence at all. Calling the tiebreak winner the leader
    invented a difference; calling both of them leaders inflated both.
    """
    if not shortlist:
        return []
    best = shortlist[0][1]
    leaders = sum(1 for _, distance in shortlist if distance == best)
    return [leaders if distance == best else 0 for _, distance in shortlist]


def match_probability(distance: int, *, leaders: int) -> int:
    """Measured chance this tile is the right artwork, as a percentage.

    `leaders` comes from `match_leader_counts`: 0 for a tile that is not at the
    best distance, otherwise how many tiles share it. See
    `_LEADING_TILE_PROBABILITY` for why the width of a tie is not a detail.

    Floored at 1 rather than rounded to 0. The lowest measured value is 0.2%,
    which is not zero, and a tile the UI is actively offering should not be
    labelled impossible.
    """
    if leaders <= 0:
        return _interpolate(_TRAILING_TILE_PROBABILITY, distance)
    band = min(leaders, MAX_TIE_BAND)
    table = _LEADING_TILE_PROBABILITY[band]
    if distance > max(table):
        if band < MAX_TIE_BAND:
            return match_probability(distance, leaders=band + 1)
        return _interpolate(_TRAILING_TILE_PROBABILITY, distance)
    value = _interpolate(table, distance)
    if leaders > MAX_TIE_BAND:
        # The widest row is "5 or more", measured across ties whose mean width
        # is about eight. Handing a twelve-way tie that aggregate would let
        # twelve tiles claim 12% each -- 148% between them, which is the same
        # defect this table replaced, just smaller. Divide the band's measured
        # mass across the tiles actually sharing it instead.
        value = round(value * MAX_TIE_BAND / leaders)
    return max(1, value)


# The same question for the fused ranking, which needs a different variable.
#
# `match_probability` is indexed by Hamming distance, and on the fused path the
# tile was chosen by the embedding, so its distance is whatever the hash thought
# of a card the hash could not find -- 18 to 22 for a batch of ordinary
# standard-layout cards. Live, that printed "1%" beside seven cards the scanner
# had just got right at tile 1. Distance is simply not what decided the order
# any more, so it cannot be what explains it.
#
# The margin between the leading tile's fused score and the next tile's is.
# Measured over 7,098 degraded photographs, on the GROUPED shortlist, jointly by
# margin and rank -- exactly one tile can be the right artwork, so reading the
# joint rather than two marginals makes "these cannot sum past 100%" a property
# of the measurement instead of a rule to enforce afterwards:
#
#   margin        n   tile 1   tile 2   tile 3   each of 4-12     sum
#   <0.25       755    47.3%    36.8%     7.7%          0.87%   99.6%
#   0.25-0.5    305    60.7%    34.4%     3.6%          0.15%  100.0%
#   0.5-0.75    247    80.2%    16.2%     2.8%          0.00%   99.2%
#   0.75-1      238    88.2%    10.5%     1.3%          0.00%  100.0%
#   1-1.5       461    95.4%     3.9%     0.4%          0.00%   99.8%
#   1.5-2       462    97.8%     1.5%     0.4%          0.02%  100.0%
#   2-3         840   100.0%     0.0%     0.0%          0.00%  100.0%
#   3-4         534   100.0%     0.0%     0.0%          0.00%  100.0%
#   >4        3,256   100.0%     0.0%     0.0%          0.00%  100.0%
#
# 65% of photographs land above margin 2, where the leading tile was right
# 4,630 times out of 4,630. The bottom bucket is the honest one: at a margin
# under 0.25 the leader is right less than half the time and the runner-up is
# right more than a third, which is a shortlist the user genuinely has to read.
#
# Stored against bucket MIDPOINTS and interpolated, so a hair of extra margin
# does not jump the badge from 61% to 80%.
_FUSED_MARGINS = (0.125, 0.375, 0.625, 0.875, 1.25, 1.75, 2.5, 3.5, 5.0)
_FUSED_PROBABILITY = {
    1: (0.473, 0.607, 0.802, 0.882, 0.954, 0.978, 0.99, 0.99, 0.99),
    2: (0.368, 0.344, 0.162, 0.105, 0.039, 0.015, 0.005, 0.005, 0.005),
    3: (0.077, 0.036, 0.028, 0.013, 0.004, 0.004, 0.002, 0.002, 0.002),
}

# Tiles past the third never carry a number. They were measured as one pooled
# bucket, not individually, and that bucket is under 0.9% at every margin -- so
# there is no per-tile claim to print. Rounding it up to 1% across nine tiles is
# also precisely how a shortlist ends up asserting 101%, which is the arithmetic
# this whole table exists to make impossible.
LAST_SCORED_TILE = 3


def fused_match_probability(margin: float, rank: int) -> int | None:
    """Measured chance this tile is the right artwork, for a fused ranking.

    ASSUMES THE RIGHT CARD IS IN THE CATALOGUE. Every query in the benchmark
    this was measured on has its answer present, so the number says nothing
    about a photo of a card the catalogue does not hold -- and roughly a fifth
    of the catalogue has no artwork at all. Measured: masking the true card out
    of the index barely moves the margin (AUC 0.606), so a clean photo of an
    absent card still reports 99%. Absolute similarity separates the two cases
    on synthetic data (AUC 0.776) and NOT on real photographs, where present
    and absent cards both sit around 0.59. Reading the printed collector number
    is the only thing that settles it; see PROJECT-NOTES sec.8 and sec.10.

    `margin` is the leading tile's fused score minus the next tile's -- one
    number describing the whole shortlist, which is why every rank reads it.
    `rank` is 1-based, and past `LAST_SCORED_TILE` the answer is None.

    None, not 1, when the measurement rounds below a percent. The hash path
    floors at 1% because its lowest measured value is 0.2% and "0%" would be a
    stronger claim than the data supports. Here the data is different: past
    margin 2, tiles 2 and 3 were wrong 840 times out of 840, and rounding that
    up to 1% on eleven tiles is how a shortlist ends up claiming 110%. A tile
    with nothing to say says nothing, and the review UI already renders no badge
    for a candidate without one.

    Capped at 99 rather than the measured 100. Four thousand consecutive correct
    answers is not proof of certainty, and the shortlist exists precisely
    because the user is the one who confirms.
    """
    if rank > LAST_SCORED_TILE:
        return None
    table = _FUSED_PROBABILITY[max(rank, 1)]
    if margin <= _FUSED_MARGINS[0]:
        value = table[0]
    elif margin >= _FUSED_MARGINS[-1]:
        value = table[-1]
    else:
        value = table[-1]
        for i in range(len(_FUSED_MARGINS) - 1):
            low, high = _FUSED_MARGINS[i], _FUSED_MARGINS[i + 1]
            if low <= margin <= high:
                weight = (margin - low) / (high - low)
                value = table[i] + (table[i + 1] - table[i]) * weight
                break
    percent = round(value * 100)
    return percent if percent >= 1 else None


def _interpolate(table: dict[int, float], distance: int) -> int:
    """A measured curve read at `distance`, as a whole percentage."""
    known = sorted(table)
    if distance <= known[0]:
        return max(1, round(table[known[0]] * 100))
    if distance >= known[-1]:
        return max(1, round(table[known[-1]] * 100))
    for lower, upper in zip(known, known[1:]):
        if lower <= distance <= upper:
            span = upper - lower
            weight = (distance - lower) / span
            blended = table[lower] + (table[upper] - table[lower]) * weight
            return max(1, round(blended * 100))
    return 1

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


# A card photographed on its side is not a rare accident -- half of one real
# 37-photo batch was shot that way -- and it defeats the detector completely.
# `find_card_box` accepts a box whose aspect is 0.35 to 1.35, because a card is
# 0.716 tall-ways; on its side it measures about 1.48 and is rejected outright.
# No box means no crop, so the whole landscape frame gets matched, and a card
# that ranks 1st upright ranked roughly 20,000th sideways.
#
# The fix is NOT to widen that gate. Rotating the IMAGE first makes the card
# upright again, and the unchanged detector then finds it at aspect 0.66-0.70 --
# comfortably inside the limits it already has. Widening the gate instead would
# start accepting genuinely wrong boxes on upright photos, and measured alone it
# rescued nothing (24,827 -> 15,244, still hopeless). Rotation is what rescues
# it: those two photos land at rank 2 and rank 6.
#
# Only when the upright detector FAILED, which is the whole point. Every extra
# view lowers every catalogue row's score, not just the right one, so the noise
# floor rises with the signal -- that is why merging a 180-degree variant was
# measured and rejected (Mew ex 4106 -> 8618, Veluza 6 -> 12). Keying on "no box
# was found" means a photo that already works is bit-for-bit unaffected: on the
# batch that prompted this, detection succeeded on 19 of 19 upright photos and
# failed on 18 of 18 sideways ones, so the condition separates them exactly.
# It is also better than keying on a landscape frame, because a card lying
# sideways inside a PORTRAIT frame fails detection too, and gets rescued as well.
_SIDEWAYS_ROTATIONS = (90, -90)


def photo_views(img: Image.Image) -> list[Image.Image]:
    """Every framing of one photograph worth matching, best guess first.

    Shared by the perceptual hash and the dense embedding so the two can never
    disagree about which parts of a photo are worth looking at.
    """
    try:
        box = find_card_box(img)
    except Exception:
        logger.debug("fingerprint: card detection failed", exc_info=True)
        return []

    if box is None:
        views = [img]
        for angle in _SIDEWAYS_ROTATIONS:
            turned = img.rotate(angle, expand=True)
            try:
                turned_box = find_card_box(turned)
            except Exception:
                logger.debug("fingerprint: detection failed on a rotation", exc_info=True)
                continue
            # Only the crop. A rotated FULL frame is the same pixels as the
            # upright full frame in a different arrangement, and a global
            # descriptor of it says nothing the original did not.
            if turned_box is not None:
                views.append(turned.crop(turned_box))
        return views

    width, height = img.size
    area = (box[2] - box[0]) * (box[3] - box[1]) / float(width * height)
    cropped = img.crop(box)
    return [img, cropped] if area >= _ALREADY_TIGHT else [cropped, img]


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
    candidates = photo_views(img)

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


def _zscore(values: np.ndarray) -> np.ndarray:
    """Centre and scale one query's scores so two signals can be added.

    Per query, not per corpus: a photograph that matches nothing has a
    different distance distribution from one that matches perfectly, and a
    fixed scale would let the easy photographs set the weight for the hard
    ones.
    """
    spread = float(values.std())
    if spread <= 1e-9:
        return np.zeros_like(values, dtype=np.float64)
    return (values.astype(np.float64) - float(values.mean())) / spread


def fuse_similarity(
    similarity: np.ndarray, distances_: np.ndarray, embedded: np.ndarray | None = None
) -> np.ndarray:
    """Combine an embedding's similarity with the hash's distance, higher-better.

    No weight. A per-query z-score sum reaches 99.94% / 89.98% on the
    7,098-photo set against a tuned weight's 99.93% / 90.14% -- indistinguish-
    able, with nothing fitted to the benchmark it is scored on. The tuned
    sweep's optimum also sat at 0.8-1.2 with a broad, flat top, which is what a
    parameter looks like when it did not need to exist.

    Keeping the hash is not sentiment. The embedding is far better at finding
    the artwork and no better at choosing between two language printings of it,
    because those are the same picture; their 64-bit hashes are of different
    renders and do differ slightly. Fusing recovers 10.3 points of exact-
    printing rank-1 (71.20% -> 81.52%) that the embedding alone leaves behind.

    `embedded` marks rows that actually have an embedding. A row without one
    gets the mean similarity -- a neutral prior, so it competes on its hash
    alone instead of being buried by a zero vector it never earned.
    """
    scores = np.asarray(similarity, dtype=np.float64).copy()
    if embedded is not None and not embedded.all():
        present = scores[embedded]
        scores[~embedded] = float(present.mean()) if present.size else 0.0
    return _zscore(scores) - _zscore(np.asarray(distances_))


def rank_fused(
    scores: np.ndarray, distances_: np.ndarray, limit: int = 12,
    max_distance: int | None = None,
) -> list[tuple[int, int]]:
    """The `limit` best rows of a fused score, reported with their distances.

    The shortlist still carries Hamming distances, because that is what the
    confidence gate, the `_confidence` label and the measured percentage are
    all defined on. `max_distance` still cuts on the distance for the same
    reason: a fused score has no bit meaning and no measured cutoff.
    """
    n = int(scores.shape[0])
    take = min(limit, n)
    if take <= 0:
        return []
    picked = (
        np.argpartition(-scores, take - 1)[:take] if take < n else np.arange(n)
    )
    order = picked[np.lexsort((_row_ids(n)[picked], -scores[picked]))]
    ranked = [(int(i), int(distances_[i])) for i in order]
    if max_distance is not None:
        ranked = [pair for pair in ranked if pair[1] <= max_distance]
    return ranked


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
