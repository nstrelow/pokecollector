"""Optional: read the collector number printed on a card.

Artwork matching cannot answer two questions, and both of them matter more in
practice than anything left to gain on the image side.

**Cards with no artwork at all.** 12,893 rows -- 22% of the catalogue -- have a
name and a number and no picture, because TCGdex does not have one. They are
absent from the fingerprint index and no image method can ever reach them. On a
real 87-photo batch, ten were cards whose entire set holds no artwork, and the
scanner answered six of them at 99% because nothing in an image score
distinguishes "this card is absent" from "this card is hard" -- measured, see
PROJECT-NOTES sec.8. The printed number does distinguish it.

**Which printing.** Two language printings of one card are the same picture, so
the embedding cannot separate them and the perceptual hash barely can. The
number is identical across languages, which is exactly why it cannot settle
language on its own -- but combined with an artwork match it pins the rest.

WHAT A NUMBER IS WORTH

Measured against the live catalogue, grouping by `tcg_card_id` so language
printings of one card count once:

    a (number, printed total) pair identifies    1 card   64.7%
                                                 2 cards  22.3%
                                                 3 cards   7.4%
                                                 4+        5.6%

So a legible number narrows 45,000 cards to three or fewer 94% of the time.
That is a stronger identity signal than any image score in this system, which
is why the merge in api/recognize_local.py lets it promote candidates -- and
why it is never allowed to remove one, since OCR misreads and an image match
that disagrees may well be the right answer.

WHAT IT COSTS, and why it is optional

`opencv-python-headless` is ~153MB of site-packages; rapidocr itself is 14MB
with its models bundled, and it reuses the onnxruntime the embedding already
installs. Detection runs at a fixed input size, so cropping tighter does not
make it faster -- budget ~1s a photo against ~25ms for the whole fingerprint
pipeline. With LOCAL_SCANNER_OCR unset nothing here is imported and recognition
behaves exactly as it did.

    pip install rapidocr-onnxruntime opencv-python-headless
    LOCAL_SCANNER_OCR=1

Plain `opencv-python` will not do: it pulls GUI libraries and dies on `libxcb`
in a slim container.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass

import numpy as np
from PIL import Image

from services.card_fingerprint import _open, find_card_box

logger = logging.getLogger(__name__)

OCR_ENABLED_ENV = "LOCAL_SCANNER_OCR"

# Shared with services/card_embedding.py on purpose -- see `_engine_options`.
THREADS_ENV = "LOCAL_SCANNER_MODEL_THREADS"

# The strip the collector number sits in, as a fraction of the card's height.
# Generous on purpose: the number is bottom-left on modern cards and bottom-
# right on some older ones, and a sleeve or a slab edge shifts the crop.
_STRIP_TOP = 0.86

# Below this width the recogniser is reading a handful of pixels per glyph. The
# benchmark's own synthetic photos are 420-520px wide, which puts the number at
# roughly 8px tall and scored 13.3% -- a result about the images, not about OCR.
_MIN_STRIP_WIDTH = 700

# How much of the strip's width the second framing keeps, from the left. The
# collector number sits bottom-left on a modern card; 45% is wide enough to hold
# it plus the set code without pulling in the rarity symbol and regulation mark
# on the right, which is the clutter that costs the wide framing its reads.
_NUMBER_SIDE = 0.45

# A whole frame is read at this longest edge. The recogniser resizes to its own
# fixed input anyway, so sending 4000px of phone photo only wastes the decode;
# 1100 is what the verification pass that found 38 of 50 numbers used.
_READ_MAX_EDGE = 1100

# Rotations tried when the detector cannot box a card. Turning the frame is
# what rescues a card lying on its side; reading it upside down is what rescues
# one photographed the other way up, and neither is knowable in advance.
_SIDEWAYS = (90, -90)
_UPSIDE_DOWN = (0, 180)

# A box has to be CARD-shaped before its bottom strip means anything. A card is
# 0.716 wide-over-tall; `find_card_box` accepts 0.35 to 1.35 because for
# matching a loose box is still a better picture than the whole frame, but
# reading a strip is a claim about where the number physically sits, and that
# claim is only true of a real card. Measured on the 50-card scan: 6 of the 23
# boxes were 0.80 to 1.35, and the ones past this range were shelves and
# neighbouring cards whose "bottom strip" held no number at all.
_CARD_ASPECT = (0.55, 0.85)

# `NNN/TTT`. Deliberately requires the slash: a card also prints a National
# Pokedex reference like `NO. 0369` near the flavour text, which upstream's LLM
# path read as the collector number and had to be taught to discard
# (`_drop_pokedex_number` in api/recognize.py). It has no slash, so it cannot
# match this -- but see `_plausible` for the rest of that guard.
_NUMBER = re.compile(r"(\d{1,3})\s*/\s*(\d{1,3})")

_lock = threading.Lock()
_engine = None
_unavailable: str | None = None


@dataclass(frozen=True)
class PrintedNumber:
    """One `NNN/TTT` read off a card, with the text it came from."""

    local: str
    total: str
    source: str

    def __str__(self) -> str:
        return f"{self.local}/{self.total}"


def enabled() -> bool:
    return (os.environ.get(OCR_ENABLED_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _engine_options() -> dict:
    """Construction options, chiefly a cap on onnxruntime's thread pool.

    Left uncapped, onnxruntime sizes its pool to every core it can see and then
    tries to pin each thread to one. Inside a container whose CPU set is a
    subset of the host's, that pinning fails and every scan writes a screenful
    of `pthread_setaffinity_np failed ... Specify the number of threads
    explicitly` to the log. The embedding path already solved this with
    LOCAL_SCANNER_MODEL_THREADS, so the same knob is reused rather than
    inventing a second one -- both are onnxruntime, on the same box, competing
    for the same cores.
    """
    raw = (os.environ.get(THREADS_ENV) or "").strip()
    try:
        threads = int(raw)
    except ValueError:
        return {}
    return {"intra_op_num_threads": threads} if threads > 0 else {}


def _load_engine():
    """The OCR engine, or None. Never raises; never retries a hard failure."""
    global _engine, _unavailable
    if not enabled():
        return None
    with _lock:
        if _engine is not None:
            return _engine
        if _unavailable is not None:
            return None
        try:
            # Imported here, not at module scope, so an installation that has
            # not opted in never pays for 153MB of OpenCV it does not have.
            from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
        except ImportError:
            _unavailable = (
                "rapidocr-onnxruntime is not installed; the printed collector "
                "number will not be read"
            )
            logger.info("card ocr: %s", _unavailable)
            return None
        try:
            _engine = RapidOCR(**_engine_options())
        except Exception:
            _unavailable = "the OCR engine could not be constructed"
            logger.exception("card ocr: %s", _unavailable)
            return None
        logger.info("card ocr: engine ready")
        return _engine


def available() -> bool:
    """Whether a printed number can be read at all in this process."""
    return _load_engine() is not None


def reset() -> None:
    """Drop the cached engine. For tests and for a changed configuration."""
    global _engine, _unavailable
    with _lock:
        _engine = None
        _unavailable = None


def _plausible(local: str, total: str) -> bool:
    """Cheap sanity before a read is worth resolving against the catalogue.

    The real guard is the catalogue lookup itself -- a garbage read almost
    never lands on a (number, printed total) pair that exists. This only throws
    out reads that cannot be a collector number at all, so the caller is not
    querying on noise.
    """
    if not local or not total:
        return False
    try:
        low, high = int(local), int(total)
    except ValueError:
        return False
    # A card numbered above its set's printed total is normal -- secret rares
    # are exactly that -- but only just above. `205/165` is real; `3/3` from a
    # mangled height or a weakness multiplier is not worth a query.
    return 1 <= low <= high + 120 and high >= 20


def _resized(view: Image.Image, *, at_least: int = 0, at_most: int = 0) -> Image.Image:
    """The view scaled by its WIDTH, which is what glyph size depends on.

    Always width, never the longest edge. Card text runs horizontally, so how
    many pixels a digit gets is set by how many pixels the width has -- and the
    recogniser rescales to its own fixed input anyway. Scaling a portrait photo
    by its longest edge instead makes it 825 wide where 1100 was intended, and
    measured on the 50-card scan that alone cost two photos their number.
    """
    scale = 1.0
    if at_least and view.width < at_least:
        scale = at_least / max(view.width, 1)
    elif at_most and view.width > at_most:
        scale = at_most / view.width
    if scale == 1.0:
        return view
    return view.resize(
        (max(1, int(view.width * scale)), max(1, int(view.height * scale))),
        Image.Resampling.LANCZOS,
    )


def _card_shaped(box) -> bool:
    """Whether a detected box is proportioned like an upright card."""
    if box is None:
        return False
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return False
    return _CARD_ASPECT[0] <= width / height <= _CARD_ASPECT[1]


def _boxed(img: Image.Image):
    """The card crop from this image, or None -- never raising."""
    try:
        box = find_card_box(img)
    except Exception:
        logger.debug("card ocr: card detection failed", exc_info=True)
        return None
    return img.crop(box) if _card_shaped(box) else None


def _card_views(img: Image.Image) -> list[tuple[Image.Image, bool]]:
    """`(view, is_a_detected_card)` for every orientation worth reading.

    The flag is the whole point. A detected box is a tight card, so the number
    is in a known place and a strip of it is the cheap read. An undetected frame
    is NOT a card -- it is a photo containing one somewhere -- and slicing a
    strip off its bottom reads the table.
    """
    views: list[tuple[Image.Image, bool]] = []

    upright = _boxed(img)
    if upright is not None:
        views.append((upright, True))
    else:
        # A sideways card the detector can box once turned gives a tight crop,
        # and a tight crop is worth the cheap strip read.
        for angle in _SIDEWAYS:
            turned = _boxed(img.rotate(angle, expand=True))
            if turned is not None:
                views.append((turned, True))

    # And ALWAYS the whole frame last, however well the boxing went. A box can
    # be card-shaped and still be the wrong card: on the 50-card scan every card
    # was photographed lying on a stack of others, and the detector routinely
    # boxed a neighbour -- whose bottom strip holds a number, just not this
    # card's. Keeping this as the LAST resort, with the caller's early exit,
    # means a photo that reads from its strip never pays for it; the cost falls
    # only on photos that would otherwise have returned nothing at all. Measured
    # on that scan, it is the difference between 27 of 50 and 34 of 50.
    #
    # A landscape frame is turned onto its side rather than read flat, because
    # the card in it is lying sideways and its text runs vertically up the
    # image; a portrait frame only needs the other way up trying.
    turns = _SIDEWAYS if img.width > img.height else _UPSIDE_DOWN
    views += [(img.rotate(angle, expand=True) if angle else img, False)
              for angle in turns]
    return views


def _framings(img: Image.Image):
    """Every crop to hand the recogniser, in order, best guess first.

    For a detected card, two bottom strips: measured on 87 real photos they are
    complementary rather than redundant -- the whole strip alone read 50, the
    left 45% alone 49, and the two together 54. The detector runs at a fixed
    input size, so a 2185x449 strip is squeezed into it and the digits arrive
    small; cropping to where the number sits spends the same inference on
    roughly twice the pixels per glyph. Neither dominates: a card numbered
    bottom-right is only in the wide framing, a cluttered wide strip only
    legible in the narrow one.

    For an undetected frame, the whole thing, shrunk to a size that keeps the
    glyphs legible rather than cropped to a place the number is not.

    Ordering is by likelihood, not by cost -- every call costs the same ~1.7s
    whatever the crop -- so the only lever on latency is how many run, which is
    why the caller stops at the first framing that yields a number.
    """
    for view, is_card in _card_views(img):
        if not is_card:
            yield _resized(view, at_most=_READ_MAX_EDGE)
            continue
        width, height = view.size
        strip = view.crop((0, int(height * _STRIP_TOP), width, height))
        yield _resized(strip, at_least=_MIN_STRIP_WIDTH)
        yield _resized(strip.crop((0, 0, int(strip.width * _NUMBER_SIDE), strip.height)),
                       at_least=_MIN_STRIP_WIDTH)


def read_printed_numbers(image_bytes: bytes) -> list[PrintedNumber]:
    """Every `NNN/TTT` legible on this photo, best guess first.

    Returns candidates rather than one answer, and knows nothing about the
    catalogue. The caller resolves them, which is also the real filter on a
    misread: a wrong number rarely names a card that exists.
    """
    engine = _load_engine()
    if engine is None:
        return []
    img = _open(image_bytes)
    if img is None:
        return []

    found: list[PrintedNumber] = []
    seen: set[tuple[str, str]] = set()
    for strip in _framings(img):
        try:
            result, _ = engine(np.asarray(strip.convert("RGB")))
        except Exception:
            logger.debug("card ocr: reading a strip failed", exc_info=True)
            continue
        text = " ".join(line[1] for line in (result or []))
        # Spaces removed before matching: the recogniser routinely splits
        # "095/083" across boxes or drops the space around the slash.
        for match in _NUMBER.finditer(re.sub(r"\s+", "", text)):
            local, total = match.group(1), match.group(2)
            if not _plausible(local, total):
                continue
            key = (local.lstrip("0") or "0", total.lstrip("0") or "0")
            if key in seen:
                continue
            seen.add(key)
            found.append(PrintedNumber(local=key[0], total=key[1], source=text[:120]))
        # One legible framing is enough. Every call costs the same ~1.7s, so
        # running the rest after an answer is pure latency -- reading all of
        # them took the median photo to 3.7s for results the first had already
        # given. A framing that reads nothing costs nothing to move past, which
        # is what keeps this safe for a card lying on its side: the upside-down
        # rotation simply yields no number and the next framing is still tried.
        if found:
            break
    return found


def resolve(db, reads: list[PrintedNumber], indexable) -> list[tuple[str, PrintedNumber]]:
    """Catalogue rows whose number and set total match something we read.

    Joined against `sets.printed_total` rather than trusting a set code: the
    recogniser reads the code badly and consistently -- `DCBL` for a printed
    `PBL`, `DCM1S` for `M1S` -- while the printed total is plain digits beside
    the number it already read, and is what makes the pair discriminating.

    This is also the real check on a misread. A wrong number almost never lands
    on a pair the catalogue holds, so a read that resolves to nothing is simply
    dropped and the scan proceeds on artwork alone.
    """
    if not reads:
        return []
    from models import Card, Set  # noqa: PLC0415 - avoids a cycle at import time

    wanted = {(r.local, r.total): r for r in reads}
    numbers = {r.local for r in reads}
    totals = {int(r.total) for r in reads}
    rows = (
        db.query(Card.id, Card.number, Set.printed_total)
        .join(Set, (Set.tcg_set_id == Card.set_id) & (Set.lang == Card.lang))
        .filter(indexable)
        .filter(Set.printed_total.in_(totals))
        .all()
    )
    out: list[tuple[str, PrintedNumber]] = []
    for card_id, number, printed_total in rows:
        key = ((number or "").lstrip("0") or "0", str(printed_total or "").lstrip("0"))
        read = wanted.get(key)
        if read is not None and key[0] in numbers:
            out.append((card_id, read))
    return out
