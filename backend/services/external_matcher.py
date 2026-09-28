"""Client for an external card matcher (the ``external`` scanner provider).

An external matcher is a separate service that identifies a card from a photo
and returns ranked catalogue candidates. It is not an LLM: there is no prompt,
no extracted text, and no capability probe. ``GET /health`` is the probe.

The contract (see docs/scanner-providers.md) is::

    POST {EXTERNAL_MATCHER_URL}/identify?session_lang=en|de&debug=1
         multipart "file"            -> {state, confident, hint, print_id,
                                         candidates[], language, number, twin,
                                         flags, geometry, timings_ms, trace_id}
    GET  {EXTERNAL_MATCHER_URL}/health  -> {ok, bundle_version, ...}
    GET  {EXTERNAL_MATCHER_URL}/bundle  -> supported sets
    GET  {EXTERNAL_MATCHER_URL}/ref/{print_id}                -> reference image
    GET  {EXTERNAL_MATCHER_URL}/trace/{trace_id}/{plane|overlay}.webp

The URL and the optional bearer token are administrator configuration from the
environment only, for the same server-side-request-forgery reason as
OPENAI_BASE_URL: users never supply an endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

EXTERNAL = "external"
DEFAULT_LABEL = "pokescan"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 300.0
# Artefacts and reference images are small; they must never hold a scan up.
ARTEFACT_TIMEOUT_SECONDS = 5.0
MAX_ARTEFACT_BYTES = 8 * 1024 * 1024
MAX_CANDIDATES = 20
SESSION_LANGUAGES = ("en", "de")
TRACE_ARTEFACT_KINDS = ("plane", "overlay")
MAX_RETRY_AFTER_SECONDS = 6 * 60 * 60

STATES = (
    "IDENTIFIED",
    "CONFIRM_LANGUAGE",
    "AMBIGUOUS",
    "POSSIBLY_UNSUPPORTED_SET",
    "NOT_IN_CATALOG",
    "NO_CARD",
    "CARD_BACK",
    "TOO_BLURRY",
)

# The fields an LLM extraction fills. An external matcher reads no text, so all
# of them stay null except the language; the review UI already renders nulls.
RECOGNIZED_FIELDS = (
    "name",
    "name_en",
    "number",
    "number_local",
    "number_total",
    "set_code",
    "regulation_mark",
    "card_type",
    "hp",
    "language",
    "artist",
)

# Relative, so it works behind whatever host pokecollector is served from. The
# matcher itself is a LAN address with a bearer token that a browser can
# neither reach (mixed content, private network) nor authenticate against.
REF_PROXY_PREFIX = "/api/cards/recognize/matcher/ref/"

PRINT_ID_PATTERN = re.compile(r"[a-z]{2}(?:-[a-z]{2})?:[A-Za-z0-9._!?+-]{1,80}")
TRACE_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,100}")
_LANG_PATTERN = re.compile(r"[a-z]{2}(?:-[a-z]{2})?")


class ExternalMatcherError(RuntimeError):
    """Base class; the message is safe to show to the scan owner."""


class MatcherNotConfiguredError(ExternalMatcherError):
    pass


class MatcherUnavailableError(ExternalMatcherError):
    """Connect failure or 502/503/504: the queue backs off and retries."""

    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class MatcherTimeoutError(ExternalMatcherError):
    """The matcher accepted the request but did not answer in time."""


class MatcherRejectedError(ExternalMatcherError):
    """A 4xx: retrying the same photo will not help."""


class MatcherResponseError(ExternalMatcherError):
    """A 5xx other than 502-504, or a body that is not the contract."""


# --------------------------------------------------------------------------- config


def matcher_url() -> str | None:
    value = (os.environ.get("EXTERNAL_MATCHER_URL") or "").strip().rstrip("/")
    return value or None


def matcher_enabled() -> bool:
    return matcher_url() is not None


def matcher_token() -> str:
    return (os.environ.get("EXTERNAL_MATCHER_TOKEN") or "").strip()


def matcher_label() -> str:
    """Administrator-controlled display label, never raw markup."""
    configured = " ".join((os.environ.get("EXTERNAL_MATCHER_LABEL") or "").split())
    if not configured or len(configured) > 60 or any(ord(char) < 32 for char in configured):
        return DEFAULT_LABEL
    return configured


def matcher_timeout() -> float:
    raw = (os.environ.get("EXTERNAL_MATCHER_TIMEOUT") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(value, MAX_TIMEOUT_SECONDS)


def normalize_session_lang(value) -> str | None:
    """Return en/de, None for empty input, and raise ValueError otherwise."""
    code = str(value or "").strip().lower()
    if not code:
        return None
    if code not in SESSION_LANGUAGES:
        raise ValueError("Unsupported scan session language.")
    return code


def _base_url() -> str:
    url = matcher_url()
    if not url:
        raise MatcherNotConfiguredError("No external card matcher is configured.")
    return url


def _headers() -> dict:
    token = matcher_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    try:
        value = float(raw) if raw else None
    except ValueError:
        return None
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


# --------------------------------------------------------------------------- HTTP


def _make_client(timeout: float) -> httpx.AsyncClient:
    """Seam for tests (httpx.MockTransport); redirects are never followed."""
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


async def _request(
    method: str,
    path: str,
    *,
    timeout: float | None = None,
    client: httpx.AsyncClient | None = None,
    **kwargs,
) -> httpx.Response:
    """One request with a single retry on connection failure.

    Status classification is left to the caller except for the transient
    gateway codes, which always mean "come back later".
    """
    url = f"{_base_url()}{path}"
    timeout = matcher_timeout() if timeout is None else timeout
    owns_client = client is None
    client = client or _make_client(timeout)
    try:
        for attempt in range(2):
            try:
                resp = await client.request(
                    method, url, headers=_headers(), timeout=timeout, **kwargs
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise MatcherUnavailableError(
                    "The external card matcher could not be reached."
                ) from exc
            except httpx.TimeoutException as exc:
                raise MatcherTimeoutError(
                    f"The external card matcher did not answer within {timeout:g} s."
                ) from exc
            except httpx.RequestError as exc:
                raise MatcherUnavailableError(
                    "The connection to the external card matcher failed."
                ) from exc
            if resp.status_code in {502, 503, 504}:
                raise MatcherUnavailableError(
                    "The external card matcher is temporarily unavailable.",
                    retry_after_seconds=_retry_after(resp),
                )
            return resp
    finally:
        if owns_client:
            await client.aclose()
    raise MatcherUnavailableError("The external card matcher could not be reached.")


def _raise_for_status(resp: httpx.Response) -> None:
    status = resp.status_code
    if status in {401, 403}:
        raise MatcherRejectedError(
            "The external card matcher rejected the credentials "
            "(check EXTERNAL_MATCHER_TOKEN)."
        )
    if status == 413:
        raise MatcherRejectedError("The external card matcher rejected the photo as too large.")
    if 400 <= status < 500:
        raise MatcherRejectedError(
            f"The external card matcher rejected the photo ({status})."
        )
    if status >= 500:
        raise MatcherResponseError(f"The external card matcher failed ({status}).")


def _json_object(resp: httpx.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError as exc:
        raise MatcherResponseError("The external card matcher returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise MatcherResponseError("The external card matcher returned an unexpected response.")
    return payload


async def identify(
    image_bytes: bytes,
    mime: str | None,
    session_lang: str | None = None,
    *,
    debug: bool = True,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """POST one photo and return the contract payload, validated for shape."""
    params = {"debug": "1" if debug else "0"}
    if session_lang in SESSION_LANGUAGES:
        params["session_lang"] = session_lang
    mime = mime or "image/jpeg"
    extension = {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
    resp = await _request(
        "POST",
        "/identify",
        params=params,
        files={"file": (f"scan.{extension}", image_bytes, mime)},
        client=client,
    )
    _raise_for_status(resp)
    payload = _json_object(resp)
    if not isinstance(payload.get("state"), str) or not isinstance(
        payload.get("candidates", []), list
    ):
        raise MatcherResponseError("The external card matcher response is missing its state.")
    payload.setdefault("candidates", [])
    return payload


async def health(*, client: httpx.AsyncClient | None = None) -> dict:
    resp = await _request("GET", "/health", timeout=min(matcher_timeout(), 10.0), client=client)
    _raise_for_status(resp)
    return _json_object(resp)


async def bundle(*, client: httpx.AsyncClient | None = None) -> dict:
    resp = await _request("GET", "/bundle", timeout=min(matcher_timeout(), 10.0), client=client)
    _raise_for_status(resp)
    return _json_object(resp)


async def _fetch_image(path: str) -> tuple[bytes, str] | None:
    """Best-effort bounded image GET; every failure is None."""
    try:
        resp = await _request("GET", path, timeout=ARTEFACT_TIMEOUT_SECONDS)
    except ExternalMatcherError:
        return None
    if resp.status_code != 200:
        return None
    content_type = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type not in {"image/webp", "image/jpeg", "image/png"}:
        return None
    data = resp.content
    if not data or len(data) > MAX_ARTEFACT_BYTES:
        return None
    return data, content_type


def valid_print_id(print_id: str) -> bool:
    return bool(PRINT_ID_PATTERN.fullmatch(str(print_id or "")))


def valid_trace_id(trace_id) -> bool:
    return isinstance(trace_id, str) and bool(TRACE_ID_PATTERN.fullmatch(trace_id))


async def fetch_ref_image(print_id: str) -> tuple[bytes, str] | None:
    if not valid_print_id(print_id):
        return None
    return await _fetch_image(f"/ref/{quote(print_id, safe=':')}")


async def fetch_trace_artefact(trace_id: str, kind: str) -> bytes | None:
    if kind not in TRACE_ARTEFACT_KINDS or not valid_trace_id(trace_id):
        return None
    result = await _fetch_image(f"/trace/{quote(trace_id, safe='')}/{kind}.webp")
    return result[0] if result else None


# --------------------------------------------------------------------------- mapping


def _split_print_id(print_id: str) -> tuple[str | None, str | None]:
    if ":" not in str(print_id or ""):
        return None, None
    lang, card_id = str(print_id).split(":", 1)
    return lang.strip().lower() or None, card_id.strip() or None


def _text(value, limit: int = 200) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def to_matches(payload: dict, db: Session | None) -> list[dict]:
    """Map contract candidates to pokecollector's scan-match shape.

    ``id`` is the composite catalogue key (``<tcg_card_id>_<lang>``) and
    ``tcg_card_id`` the bare TCGdex id, exactly like match_card_info(), so the
    review page and resolve-and-add need no provider-specific handling. Ids the
    local catalogue lacks (for example Japanese hits on an en,de sync) are still
    returned; resolve-and-add imports them through ensure_card_exists().
    """
    mapped: list[dict] = []
    seen: set[str] = set()
    for raw in (payload.get("candidates") or [])[:MAX_CANDIDATES]:
        if not isinstance(raw, dict):
            continue
        print_id = _text(raw.get("print_id"), 100) or ""
        split_lang, split_card_id = _split_print_id(print_id)
        tcg_card_id = _text(raw.get("tcg_card_id"), 100) or split_card_id
        lang = (_text(raw.get("lang"), 10) or split_lang or "").lower()
        if not tcg_card_id or not _LANG_PATTERN.fullmatch(lang):
            continue
        card_key = f"{tcg_card_id}_{lang}"
        if card_key in seen:
            continue
        seen.add(card_key)
        if not print_id:
            print_id = f"{lang}:{tcg_card_id}"
        set_id = _text(raw.get("set_id"), 60) or (
            tcg_card_id.rsplit("-", 1)[0] if "-" in tcg_card_id else None
        )
        score = _float(raw.get("score"))
        margin = _float(raw.get("margin"))
        mapped.append({
            "id": card_key,
            "tcg_card_id": tcg_card_id,
            "name": _text(raw.get("name")),
            "card_type": None,
            "set": set_id,
            "set_id": set_id,
            "number": _text(raw.get("number"), 30),
            "rarity": _text(raw.get("rarity"), 80),
            "image": (
                f"{REF_PROXY_PREFIX}{quote(print_id, safe=':')}"
                if valid_print_id(print_id)
                else None
            ),
            "image_hd": None,
            "lang": lang,
            "print_id": print_id,
            "in_catalogue": False,
            "_score": score,
            "_margin": margin,
            "_match_percent": (
                max(0, min(100, round(score * 100))) if score is not None else None
            ),
        })
    if db is not None and mapped:
        _enrich_from_catalogue(db, mapped)
    return mapped


def _enrich_from_catalogue(db: Session, matches: list[dict]) -> None:
    """Prefer the local card image and set name where the catalogue has them."""
    from models import Card, Set

    try:
        rows = (
            db.query(
                Card.id,
                Card.name,
                Card.supertype,
                Card.images_small,
                Card.images_large,
                Card.rarity,
            )
            .filter(Card.id.in_([match["id"] for match in matches]))
            .all()
        )
        cards = {row.id: row for row in rows}
        set_keys = {(match["set_id"], match["lang"]) for match in matches if match["set_id"]}
        sets = {}
        if set_keys:
            set_rows = (
                db.query(Set)
                .filter(Set.tcg_set_id.in_({set_id for set_id, _lang in set_keys}))
                .all()
            )
            sets = {(row.tcg_set_id, row.lang): row for row in set_rows}
    except Exception:
        logger.warning("Could not enrich external matcher candidates", exc_info=True)
        return
    for match in matches:
        row = cards.get(match["id"])
        if row is not None:
            match["in_catalogue"] = True
            match["name"] = match["name"] or row.name
            match["rarity"] = match["rarity"] or row.rarity
            match["card_type"] = row.supertype
            if row.images_small:
                match["image"] = row.images_small
                match["image_hd"] = row.images_large
        local_set = sets.get((match["set_id"], match["lang"]))
        if local_set is not None:
            match["set"] = local_set.name
            match["set_abbreviation"] = local_set.abbreviation
            match["printed_total"] = local_set.printed_total or None


def matcher_blob(payload: dict) -> dict:
    """The stored debug blob: the full payload minus the (mapped) candidates."""
    return {key: value for key, value in payload.items() if key != "candidates"}


def build_scan_result(
    payload: dict,
    db: Session | None,
    session_lang: str | None = None,
) -> dict:
    """Turn a contract payload into the result dict the scan queue stores."""
    matches = to_matches(payload, db)
    state = str(payload.get("state") or "")
    confident = bool(payload.get("confident")) and bool(matches)
    language = matches[0]["lang"] if matches else session_lang
    recognized = {field: None for field in RECOGNIZED_FIELDS}
    recognized["language"] = language
    # Also kept on `recognized`, the part of the result the queue persists and
    # serves with the item, so the review page can tell the source apart
    # without fetching the debug blob.
    recognized["_source"] = EXTERNAL
    recognized["_identity_decision"] = state
    recognized["_identity_confident"] = confident
    recognized["_hint"] = _text(payload.get("hint"))
    blob = matcher_blob(payload)
    blob["session_lang"] = session_lang
    return {
        "recognized": recognized,
        "matches": matches,
        "_source": EXTERNAL,
        "_identity_decision": state,
        "_identity_confident": confident,
        "_matcher": blob,
    }


async def recognize_with_matcher(
    db: Session,
    image_bytes: bytes,
    content_type: str | None,
    *,
    session_lang: str | None = None,
    trace=None,
) -> dict:
    """identify() + mapping + trace recording, shared by the queue and /recognize.

    Raises the ExternalMatcherError family; callers translate those into their
    own failure types.
    """
    try:
        payload = await identify(image_bytes, content_type, session_lang, debug=True)
    except ExternalMatcherError as exc:
        if trace is not None:
            trace.record_error(str(exc))
        raise
    result = build_scan_result(payload, db, session_lang)
    if trace is not None and getattr(trace, "enabled", False):
        trace.record_matcher(payload, session_lang=session_lang)
        trace.record_candidates(result["matches"])
        top = result["matches"][0]["tcg_card_id"] if result["matches"] else None
        trace.record_decision(
            f"external:{result['_identity_decision'] or 'unknown'}",
            top if result["_identity_confident"] else None,
        )
        trace_id = payload.get("trace_id")
        if valid_trace_id(trace_id):
            for kind in TRACE_ARTEFACT_KINDS:
                try:
                    data = await fetch_trace_artefact(trace_id, kind)
                except Exception:
                    data = None
                if data:
                    trace.add_artefact(kind, data)
        result["_matcher"]["_pokecollector_trace_id"] = trace.trace_id
    return result
