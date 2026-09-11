"""Optional dense embeddings for offline recognition.

The 64-bit perceptual hash in `card_fingerprint` is the ceiling on this feature,
not its floor. Measured against the same 42,254-card index and the same 7,098
degraded photographs, and then against the eleven real phone photos that are
the only honest benchmark here:

                                  artwork in 12   artwork @1   printing @1
    64-bit hash (what ships)             90.67%       69.06%        59.40%
    this module's embedding              99.90%       91.18%        81.52%

    exact printing found, real photos     2 of 6       6 of 6

Every failure class the hash cannot fix -- gold foil photographed at an angle,
a price sticker over the artwork, a card photographed upside down -- is fixed.
The hash is kept and fused back in rather than replaced: two language printings
of one card are different renders, so their hashes differ slightly even where
their embeddings do not, and fusing recovers 10.3 points of exact-printing
rank-1 that the embedding alone leaves on the table.

WHAT THIS COSTS, and why it is optional
---------------------------------------
onnxruntime is ~70MB of site-packages and the model another 88MB (24MB
quantised), against a 707MB backend image, and a scan goes from ~117ms to
~540ms. That is a real price on a self-hosted box, and the upstream project
values not adding dependencies. So none of it is required: with onnxruntime
absent or LOCAL_SCANNER_MODEL unset, `available()` is False, nothing imports
onnxruntime, no column is written, and offline recognition behaves exactly as
it did. A row with no embedding is ranked on its hash alone, so the two can
coexist while a backfill catches up.

    pip install onnxruntime
    LOCAL_SCANNER_MODEL=/models/dinov2-small.onnx

The model is DINOv2-small (`facebook/dinov2-small`, Apache-2.0, so compatible
with this project's AGPL-3.0), exported to ONNX. Any model producing a
`(1, tokens, dim)` `last_hidden_state` from a `(1, 3, 224, 224)` `pixel_values`
input will load, but the stored vectors are only comparable to each other --
changing the model invalidates every stored embedding, which is what
`image_embedding_source` and the `EMBEDDING_VERSION` below exist to detect.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading

import numpy as np
from PIL import Image

from services.card_fingerprint import _open

logger = logging.getLogger(__name__)

MODEL_PATH_ENV = "LOCAL_SCANNER_MODEL"
THREADS_ENV = "LOCAL_SCANNER_MODEL_THREADS"

# ImageNet statistics, which is what DINOv2 was trained against. Part of the
# stored vectors' definition: changing them invalidates every one of them.
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_SIDE = 224

# A vector is the CLS token and the mean of the patch tokens, each
# L2-normalised, concatenated, and normalised again. Measured on the
# 7,098-photo set: CLS alone reaches 99.07% / 86.01%, the patch mean alone is
# clearly worse at 96.07% / 81.19%, and the two together beat either at
# 99.17% / 87.22%. The model computes all 257 tokens either way, so the second
# descriptor is free.
#
# Its length is therefore twice the model's hidden size -- 768 for
# DINOv2-small -- and is deliberately NOT a constant here. Pinning it would
# reject a perfectly good larger model with a misleading "malformed" warning,
# and the index has a better rule available to it anyway: every row must agree
# with every other row, which is the property that actually matters.
#
# Bumped whenever anything above changes the meaning of a stored vector.
# Recorded in `image_embedding_source` so a backfill re-queues rows embedded by
# an older definition, exactly as `image_phash_source` does for the hash.
EMBEDDING_VERSION = 1

# Separates the model tag from the artwork URL inside that column. A space is
# safe: it cannot occur in a URL, so the marker can never be ambiguous.
MARKER_SEPARATOR = " "

_lock = threading.Lock()
_session = None
_session_path: str | None = None
_unavailable_reason: str | None = None


def model_path() -> str:
    return (os.environ.get(MODEL_PATH_ENV) or "").strip()


def _threads() -> int:
    try:
        value = int(os.environ.get(THREADS_ENV, "").strip())
    except (TypeError, ValueError):
        return 2
    return value if value > 0 else 2


def _load_session():
    """The ONNX session, or None. Never raises; never retries a hard failure.

    Import is inside the function on purpose: onnxruntime is an optional extra
    and importing it at module scope would make this module unimportable on
    every install that has not opted in.
    """
    global _session, _session_path, _unavailable_reason
    path = model_path()
    if not path:
        return None
    with _lock:
        if _session is not None and _session_path == path:
            return _session
        if _unavailable_reason is not None and _session_path == path:
            return None
        _session_path = path
        try:
            import onnxruntime as ort  # noqa: PLC0415 - optional dependency
        except ImportError:
            _unavailable_reason = (
                "onnxruntime is not installed; offline recognition will use the "
                "perceptual hash alone"
            )
            logger.info("card embedding: %s", _unavailable_reason)
            return None
        if not os.path.isfile(path):
            _unavailable_reason = f"{MODEL_PATH_ENV}={path!r} is not a file"
            logger.warning("card embedding: %s", _unavailable_reason)
            return None
        try:
            options = ort.SessionOptions()
            options.intra_op_num_threads = _threads()
            options.inter_op_num_threads = 1
            _session = ort.InferenceSession(
                path, options, providers=["CPUExecutionProvider"]
            )
        except Exception:
            _unavailable_reason = f"{path!r} could not be loaded as an ONNX model"
            logger.exception("card embedding: %s", _unavailable_reason)
            _session = None
            return None
        _unavailable_reason = None
        logger.info(
            "card embedding: loaded %s on %d thread(s)", path, _threads()
        )
        return _session


def available() -> bool:
    """Whether embeddings can be computed at all in this process."""
    return _load_session() is not None


def reset() -> None:
    """Drop the cached session. For tests and for a changed model path."""
    global _session, _session_path, _unavailable_reason
    with _lock:
        _session = None
        _session_path = None
        _unavailable_reason = None


def model_tag() -> str:
    """Short, stable identifier for "which model, by which definition"."""
    digest = hashlib.sha256(model_path().encode("utf-8")).hexdigest()[:12]
    return f"v{EMBEDDING_VERSION}:{digest}"


def source_marker(url: str) -> str:
    """Provenance for a stored embedding: this picture, by this definition.

    The URL alone is not enough. `image_phash_source` only has to answer "is
    this hash of this picture", because the hash's definition has never
    changed; an embedding's definition is a model file, and a different model
    produces vectors that are meaningless against the stored ones while looking
    perfectly valid. Folding the model in makes a model swap re-queue every row
    the same way a rotated artwork URL does, without anyone remembering to.

    Deliberately a PREFIX plus the URL rather than a hash of both, so the
    backfill's "is this row stale" test stays one SQL expression -- a database
    cannot recompute a SHA-256 of three values, and a staleness rule that has
    to be evaluated in Python over 45,000 rows is not a rule, it is a scan.
    """
    return f"{model_tag()}{MARKER_SEPARATOR}{url}"


def _normalise(vector: np.ndarray) -> np.ndarray:
    return (vector / (np.linalg.norm(vector) + 1e-9)).astype(np.float32)


def _tokens(img: Image.Image) -> np.ndarray | None:
    session = _load_session()
    if session is None:
        return None
    try:
        pixels = np.asarray(
            img.convert("RGB").resize((_SIDE, _SIDE), Image.Resampling.BICUBIC),
            dtype=np.float32,
        )
        pixels = ((pixels / 255.0) - _MEAN) / _STD
        output = session.run(
            None, {"pixel_values": pixels.transpose(2, 0, 1)[None]}
        )[0][0]
        if output.ndim != 2 or output.shape[0] < 2:
            logger.warning(
                "card embedding: model returned %s, expected (tokens, dim)",
                output.shape,
            )
            return None
        return _normalise(
            np.concatenate([
                _normalise(output[0]),
                _normalise(output[1:].mean(axis=0)),
            ])
        )
    except Exception:
        logger.debug("card embedding: embedding an image failed", exc_info=True)
        return None


def pack(vector: np.ndarray) -> bytes:
    """The one way an embedding becomes the bytes stored and compared.

    float16, because the accuracy cost is unmeasurable and the saving is not:
    over the 42,254-card index, fp16 scores 99.10% against fp32's 99.07% -- and
    halves both the column and the resident index.
    """
    return np.asarray(vector, dtype=np.float16).tobytes()


def unpack(blob: bytes | memoryview | None, dims: int | None = None) -> np.ndarray | None:
    """A stored embedding, or None if the bytes are not one.

    `dims` is the length every other row in the index agreed on; the first row
    read passes None and sets it. A blob of the wrong length is a row embedded
    by a different model, which is meaningless against the rest and has to be
    skipped rather than reshaped into something plausible.
    """
    if not blob:
        return None
    raw = bytes(blob)
    if len(raw) < 4 or len(raw) % 2:
        return None
    if dims is not None and len(raw) != dims * 2:
        return None
    return np.frombuffer(raw, dtype=np.float16).astype(np.float32)


def embed_reference(image_bytes: bytes) -> bytes | None:
    """Embed a catalogue render. Does not crop, for the same reason the hash
    does not: detection fires on a tight 400x400 TCGdex render and trims a
    fifth of its area, desynchronising the index from itself."""
    img = _open(image_bytes)
    if img is None:
        return None
    vector = _tokens(img)
    return None if vector is None else pack(vector)


def embed_photo_views(image_bytes: bytes) -> list[np.ndarray]:
    """Every view of a user's photograph worth scoring, best guess first.

    The crop and the whole frame, exactly as `photo_hash_variants` does for the
    hash, and for the same measured reason: scoring both and keeping whichever
    lands closer is worth +2.71 points of rank-1 on the synthetic set, and it
    is what moves a card photographed upside down from rank 6 to rank 3 on the
    real ones. The card detector cannot be trusted to frame every shop photo,
    and the frame is a free second opinion.

    Merging views is safe here in a way it is not for the hash. Taking a
    minimum over Hamming variants lowers every row's distance, so the noise
    floor falls with the true card -- that is why crop-and-rotation search was
    measured and rejected. Cosine similarity over a discriminative embedding
    does not behave that way: the same experiment gains 2.71 points instead of
    losing rank. "More variants is not a strategy" is a property of a 64-bit
    Hamming space, not a law.
    """
    from services.card_fingerprint import find_card_box

    img = _open(image_bytes)
    if img is None:
        return []
    views = [img]
    try:
        box = find_card_box(img)
    except Exception:
        logger.debug("card embedding: card detection failed", exc_info=True)
        box = None
    if box is not None:
        views.insert(0, img.crop(box))
    vectors = [_tokens(view) for view in views]
    return [v for v in vectors if v is not None]
