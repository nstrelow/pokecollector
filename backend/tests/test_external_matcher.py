"""The `external` scanner provider: client, mapping, queue and API integration.

HTTP is faked with httpx.MockTransport through external_matcher._make_client,
so no extra test dependency is needed. Fixtures under fixtures/matcher/ are
one §4.2 payload per matcher state (docs/POKESCANNER-PLAN.md).
"""

import asyncio
import datetime
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

try:
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api.auth import get_current_user
    from api.recognize import router as recognize_router
    from api.scan_jobs import router as scan_jobs_router
    from api.settings import router as settings_router
    from database import Base, get_db
    from models import (
        Card,
        CollectionItem,
        ScanJob,
        ScanJobItem,
        ScanQueueUserState,
        Set,
        User,
        UserSetting,
    )
    from services import external_matcher, scan_queue, scan_storage
    from services.scan_providers import (
        EXTERNAL,
        GEMINI,
        SCANNER_CAPABILITY_FULL,
        enabled_providers,
        get_provider,
        require_scanner_capability_mode,
        resolve_provider_name,
    )
    from services.scan_queue import claim_next_scan_item
    from services.scan_trace import ScanTrace

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False

FIXTURES = Path(__file__).parent / "fixtures" / "matcher"
MATCHER_URL = "http://matcher.test:8000"
STATE_FIXTURES = {
    "IDENTIFIED": "identified",
    "CONFIRM_LANGUAGE": "confirm_language",
    "AMBIGUOUS": "ambiguous",
    "POSSIBLY_UNSUPPORTED_SET": "possibly_unsupported_set",
    "NOT_IN_CATALOG": "not_in_catalog",
    "NO_CARD": "no_card",
    "CARD_BACK": "card_back",
    "TOO_BLURRY": "too_blurry",
}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _jpeg_bytes():
    image = Image.new("RGB", (80, 112), "#2848d9")
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


class FakeMatcher:
    """Route table for MockTransport; records every request it sees."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], object] = {}

    def on(self, method: str, path: str, response):
        self.routes[(method, path)] = response
        return self

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get((request.method, request.url.path))
        if response is None:
            return httpx.Response(404, json={"detail": "not found"})
        if callable(response):
            return response(request)
        return response

    def factory(self, timeout):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(self.handler), timeout=timeout
        )


class MatcherEnvMixin:
    env_extra: dict = {}

    def _start_env(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        env = {
            "SCAN_UPLOAD_DIR": self.temp_dir.name,
            "EXTERNAL_MATCHER_URL": MATCHER_URL,
            "EXTERNAL_MATCHER_TOKEN": "matcher-secret",
        }
        env.update(self.env_extra)
        self.env = patch.dict(os.environ, env)
        self.env.start()
        for name in ("SCAN_TRACE_DIR", "SCAN_TRACE_STORAGE_DIR", "EXTERNAL_MATCHER_LABEL",
                     "EXTERNAL_MATCHER_TIMEOUT"):
            if name not in env:
                os.environ.pop(name, None)
        self.fake = FakeMatcher()
        self.client_patch = patch.object(external_matcher, "_make_client", self.fake.factory)
        self.client_patch.start()
        # The single connect-error retry sleeps briefly; tests need not wait.
        self.sleep_patch = patch.object(external_matcher.asyncio, "sleep", AsyncMock())
        self.sleep_patch.start()

    def _stop_env(self):
        self.sleep_patch.stop()
        self.client_patch.stop()
        self.env.stop()
        self.temp_dir.cleanup()

    def _db(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()


# --------------------------------------------------------------------------- client


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class ExternalMatcherClientTests(MatcherEnvMixin, unittest.TestCase):
    def setUp(self):
        self._start_env()

    def tearDown(self):
        self._stop_env()

    def test_config_defaults(self):
        self.assertEqual(external_matcher.matcher_url(), MATCHER_URL)
        self.assertEqual(external_matcher.matcher_label(), "pokescan")
        self.assertEqual(external_matcher.matcher_timeout(), 20.0)
        with patch.dict(os.environ, {"EXTERNAL_MATCHER_TIMEOUT": "nonsense"}):
            self.assertEqual(external_matcher.matcher_timeout(), 20.0)
        with patch.dict(os.environ, {"EXTERNAL_MATCHER_TIMEOUT": "7.5"}):
            self.assertEqual(external_matcher.matcher_timeout(), 7.5)
        with patch.dict(os.environ, {"EXTERNAL_MATCHER_URL": "  "}):
            self.assertFalse(external_matcher.matcher_enabled())

    def test_identify_posts_file_with_token_session_lang_and_debug(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("identified")))
        payload = asyncio.run(
            external_matcher.identify(b"jpeg-bytes", "image/jpeg", "de")
        )
        self.assertEqual(payload["state"], "IDENTIFIED")
        request = self.fake.requests[0]
        self.assertEqual(request.headers["authorization"], "Bearer matcher-secret")
        self.assertEqual(request.url.params["session_lang"], "de")
        self.assertEqual(request.url.params["debug"], "1")
        self.assertIn(b"jpeg-bytes", request.content)
        self.assertIn(b'name="file"', request.content)

    def test_identify_omits_unsupported_session_lang_and_token_when_unset(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("no_card")))
        with patch.dict(os.environ, {"EXTERNAL_MATCHER_TOKEN": ""}):
            asyncio.run(external_matcher.identify(b"x", "image/jpeg", "fr"))
        request = self.fake.requests[0]
        self.assertNotIn("session_lang", request.url.params)
        self.assertNotIn("authorization", request.headers)

    def test_503_is_unavailable_with_retry_after(self):
        self.fake.on(
            "POST", "/identify", httpx.Response(503, headers={"Retry-After": "45"})
        )
        with self.assertRaises(external_matcher.MatcherUnavailableError) as ctx:
            asyncio.run(external_matcher.identify(b"x", "image/jpeg"))
        self.assertEqual(ctx.exception.retry_after_seconds, 45.0)

    def test_connect_error_retries_once_then_unavailable(self):
        calls = []

        def refuse(request):
            calls.append(request)
            raise httpx.ConnectError("refused", request=request)

        self.fake.on("POST", "/identify", refuse)
        with self.assertRaises(external_matcher.MatcherUnavailableError):
            asyncio.run(external_matcher.identify(b"x", "image/jpeg"))
        self.assertEqual(len(calls), 2)

    def test_connect_error_then_success(self):
        calls = []

        def flaky(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json=fixture("identified"))

        self.fake.on("POST", "/identify", flaky)
        payload = asyncio.run(external_matcher.identify(b"x", "image/jpeg"))
        self.assertEqual(payload["state"], "IDENTIFIED")
        self.assertEqual(len(calls), 2)

    def test_read_timeout_is_a_timeout_error_with_a_clear_message(self):
        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        self.fake.on("POST", "/identify", slow)
        with self.assertRaises(external_matcher.MatcherTimeoutError) as ctx:
            asyncio.run(external_matcher.identify(b"x", "image/jpeg"))
        self.assertIn("did not answer within 20 s", str(ctx.exception))

    def test_4xx_is_rejected_and_5xx_or_garbage_is_a_response_error(self):
        self.fake.on("POST", "/identify", httpx.Response(401))
        with self.assertRaises(external_matcher.MatcherRejectedError) as ctx:
            asyncio.run(external_matcher.identify(b"x", "image/jpeg"))
        self.assertIn("EXTERNAL_MATCHER_TOKEN", str(ctx.exception))
        self.assertNotIn("matcher-secret", str(ctx.exception))

        self.fake.on("POST", "/identify", httpx.Response(500))
        with self.assertRaises(external_matcher.MatcherResponseError):
            asyncio.run(external_matcher.identify(b"x", "image/jpeg"))

        self.fake.on("POST", "/identify", httpx.Response(200, content=b"<html>"))
        with self.assertRaises(external_matcher.MatcherResponseError):
            asyncio.run(external_matcher.identify(b"x", "image/jpeg"))

        self.fake.on("POST", "/identify", httpx.Response(200, json={"candidates": []}))
        with self.assertRaises(external_matcher.MatcherResponseError):
            asyncio.run(external_matcher.identify(b"x", "image/jpeg"))

    def test_not_configured(self):
        with patch.dict(os.environ, {"EXTERNAL_MATCHER_URL": ""}):
            with self.assertRaises(external_matcher.MatcherNotConfiguredError):
                asyncio.run(external_matcher.health())

    def test_health_and_bundle(self):
        self.fake.on("GET", "/health", httpx.Response(200, json={"ok": True, "bundle_version": "v12"}))
        self.fake.on("GET", "/bundle", httpx.Response(200, json={"sets": {"en": ["sv03.5"]}}))
        self.assertTrue(asyncio.run(external_matcher.health())["ok"])
        self.assertEqual(asyncio.run(external_matcher.bundle())["sets"]["en"], ["sv03.5"])

    def test_ref_and_trace_fetch_validate_ids(self):
        self.fake.on(
            "GET", "/ref/ja:SV2a-043",
            httpx.Response(200, content=b"RIFFwebp", headers={"content-type": "image/webp"}),
        )
        self.assertEqual(
            asyncio.run(external_matcher.fetch_ref_image("ja:SV2a-043")),
            (b"RIFFwebp", "image/webp"),
        )
        self.assertIsNone(asyncio.run(external_matcher.fetch_ref_image("../etc/passwd")))
        self.assertIsNone(asyncio.run(external_matcher.fetch_ref_image("en:a/b")))
        self.assertIsNone(asyncio.run(external_matcher.fetch_trace_artefact("t", "secret")))
        self.assertIsNone(asyncio.run(external_matcher.fetch_trace_artefact("../x", "plane")))
        # Pure dot ids would be normalised into a different path by httpx.
        for bad in ("..", ".", ".x", ":x", "-x"):
            self.assertFalse(external_matcher.valid_trace_id(bad), bad)
            self.assertIsNone(asyncio.run(external_matcher.fetch_trace_artefact(bad, "plane")))
        self.assertTrue(external_matcher.valid_trace_id("2026-09-28T21-04-11Z_ab12cd"))
        # Invalid ids never reach the transport; only the valid ref did.
        self.assertEqual(len(self.fake.requests), 1)


# --------------------------------------------------------------------------- mapping


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class ExternalMatcherMappingTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"EXTERNAL_MATCHER_URL": MATCHER_URL})
        self.env.start()
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.env.stop()

    def test_every_state_maps_to_the_scan_result_shape(self):
        for state, name in STATE_FIXTURES.items():
            with self.subTest(state=state):
                payload = fixture(name)
                result = external_matcher.build_scan_result(payload, self.db, "en")
                self.assertEqual(result["_source"], "external")
                self.assertEqual(result["_identity_decision"], state)
                self.assertEqual(
                    result["_identity_confident"],
                    bool(payload["confident"]) and bool(payload["candidates"]),
                )
                recognized = result["recognized"]
                for field in external_matcher.RECOGNIZED_FIELDS:
                    if field != "language":
                        self.assertIsNone(recognized[field], field)
                self.assertEqual(recognized["_source"], "external")
                self.assertEqual(recognized["_identity_decision"], state)
                self.assertEqual(len(result["matches"]), len(payload["candidates"]))
                self.assertNotIn("candidates", result["_matcher"])
                self.assertEqual(result["_matcher"]["state"], state)
                self.assertEqual(result["_matcher"]["session_lang"], "en")
                self.assertEqual(result["_matcher"]["flags"], payload["flags"])
                if payload["candidates"]:
                    self.assertEqual(
                        recognized["language"], payload["candidates"][0]["lang"]
                    )
                else:
                    # Nothing identified: fall back to the session language.
                    self.assertEqual(recognized["language"], "en")
                    self.assertEqual(result["matches"], [])

    def test_confident_states(self):
        confident = {
            state
            for state, name in STATE_FIXTURES.items()
            if external_matcher.build_scan_result(fixture(name), self.db)["_identity_confident"]
        }
        self.assertEqual(confident, {"IDENTIFIED", "CONFIRM_LANGUAGE"})

    def test_candidate_mapping_without_local_catalogue(self):
        matches = external_matcher.to_matches(fixture("identified"), self.db)
        first = matches[0]
        self.assertEqual(first["id"], "sv03.5-043_en")
        self.assertEqual(first["tcg_card_id"], "sv03.5-043")
        self.assertEqual(first["lang"], "en")
        self.assertEqual(first["set_id"], "sv03.5")
        self.assertEqual(first["number"], "043")
        self.assertEqual(first["name"], "Oddish")
        self.assertEqual(first["rarity"], "Common")
        self.assertEqual(first["_score"], 0.91)
        self.assertEqual(first["_margin"], 0.12)
        self.assertEqual(first["_match_percent"], 91)
        self.assertEqual(first["image"], "/api/cards/recognize/matcher/ref/en:sv03.5-043")
        self.assertFalse(first["in_catalogue"])
        self.assertEqual(matches[1]["id"], "sv03.5-043_de")

    def test_zh_tw_and_ja_ids_map_unchanged(self):
        zh = external_matcher.to_matches(fixture("identified_zh_tw"), self.db)[0]
        self.assertEqual(zh["id"], "SV3a-043_zh-tw")
        self.assertEqual(zh["tcg_card_id"], "SV3a-043")
        self.assertEqual(zh["lang"], "zh-tw")
        self.assertEqual(zh["set_id"], "SV3a")
        self.assertEqual(zh["image"], "/api/cards/recognize/matcher/ref/zh-tw:SV3a-043")
        ja = external_matcher.to_matches(fixture("identified_ja"), self.db)[0]
        self.assertEqual(ja["id"], "SV2a-043_ja")
        result = external_matcher.build_scan_result(fixture("identified_zh_tw"), self.db, "de")
        self.assertEqual(result["recognized"]["language"], "zh-tw")

    def test_local_catalogue_row_supplies_image_and_set(self):
        self.db.add(Card(
            id="sv03.5-043_en", tcg_card_id="sv03.5-043", name="Oddish", number="043",
            lang="en", is_custom=False, supertype="Pokémon",
            images_small="https://assets.tcgdex.net/en/sv/sv03.5/043/low.webp",
            images_large="https://assets.tcgdex.net/en/sv/sv03.5/043/high.webp",
        ))
        self.db.add(Set(id="sv03.5_en", tcg_set_id="sv03.5", name="151", lang="en",
                        abbreviation="MEW", printed_total=165))
        self.db.commit()
        first, second = external_matcher.to_matches(fixture("identified"), self.db)[:2]
        self.assertTrue(first["in_catalogue"])
        self.assertEqual(first["image"], "https://assets.tcgdex.net/en/sv/sv03.5/043/low.webp")
        self.assertEqual(first["image_hd"], "https://assets.tcgdex.net/en/sv/sv03.5/043/high.webp")
        self.assertEqual(first["set"], "151")
        self.assertEqual(first["set_abbreviation"], "MEW")
        self.assertEqual(first["printed_total"], 165)
        self.assertEqual(first["card_type"], "Pokémon")
        # The German print is not synced locally: matcher thumbnail.
        self.assertFalse(second["in_catalogue"])
        self.assertTrue(second["image"].startswith(external_matcher.REF_PROXY_PREFIX))

    def test_malformed_candidates_are_skipped_and_deduplicated(self):
        payload = {
            "state": "AMBIGUOUS",
            "confident": False,
            "candidates": [
                "junk",
                {"print_id": "no-colon"},
                {"print_id": "en:sv1-1", "score": "nan"},
                {"print_id": "en:sv1-1", "score": 0.3},
                {"tcg_card_id": "sv1-2", "lang": "EN", "score": 1.7},
            ],
        }
        matches = external_matcher.to_matches(payload, None)
        self.assertEqual([m["id"] for m in matches], ["sv1-1_en", "sv1-2_en"])
        self.assertIsNone(matches[0]["_score"])
        self.assertIsNone(matches[0]["_match_percent"])
        self.assertEqual(matches[1]["_match_percent"], 100)
        self.assertEqual(matches[1]["print_id"], "en:sv1-2")

    def test_session_lang_validation(self):
        self.assertIsNone(external_matcher.normalize_session_lang(None))
        self.assertIsNone(external_matcher.normalize_session_lang(" "))
        self.assertEqual(external_matcher.normalize_session_lang("DE"), "de")
        with self.assertRaises(ValueError):
            external_matcher.normalize_session_lang("ja")


# --------------------------------------------------------------------------- provider


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class ExternalProviderTests(MatcherEnvMixin, unittest.TestCase):
    def setUp(self):
        self._start_env()
        self._db()
        self.user = User(username="ext-provider", hashed_password="x", is_active=True)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self._stop_env()

    def test_external_is_enabled_and_the_default_when_configured(self):
        self.assertEqual(enabled_providers()[0], EXTERNAL)
        self.assertEqual(resolve_provider_name(self.db, self.user.id), EXTERNAL)
        provider = get_provider(self.db, self.user.id)
        self.assertEqual(provider.name, EXTERNAL)
        self.assertEqual(provider.model(), "pokescan")
        self.assertFalse(provider.requires_credential())
        self.assertEqual(provider.credential(self.db, self.user.id), "")
        self.assertFalse(provider.is_llm())
        self.assertEqual(
            require_scanner_capability_mode(self.db, self.user.id, EXTERNAL, "pokescan"),
            SCANNER_CAPABILITY_FULL,
        )

    def test_explicit_gemini_choice_is_respected(self):
        self.db.add(UserSetting(user_id=self.user.id, key="scanner_provider", value="gemini"))
        self.db.commit()
        self.assertEqual(resolve_provider_name(self.db, self.user.id), GEMINI)

    def test_without_url_external_is_absent_and_gemini_stays_default(self):
        with patch.dict(os.environ, {"EXTERNAL_MATCHER_URL": ""}):
            self.assertNotIn(EXTERNAL, enabled_providers())
            self.assertEqual(resolve_provider_name(self.db, self.user.id), GEMINI)

    def test_generate_text_never_falls_through_to_openai(self):
        from fastapi import HTTPException

        provider = get_provider(self.db, self.user.id)
        with self.assertRaises(HTTPException):
            asyncio.run(provider.generate_text(MagicMock(), "", [{"text": "hi"}]))


# --------------------------------------------------------------------------- queue


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class ExternalQueueTests(MatcherEnvMixin, unittest.TestCase):
    def setUp(self):
        self._start_env()
        self._db()
        self.user = User(username="ext-queue", hashed_password="x", is_active=True)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self._stop_env()

    def _job(self, positions=(0,), *, batch=False, session_lang="de"):
        now = datetime.datetime.utcnow()
        job = ScanJob(
            user_id=self.user.id, status="pending", created_at=now, updated_at=now,
            expires_at=now + datetime.timedelta(days=14), session_lang=session_lang,
        )
        self.db.add(job)
        self.db.flush()
        if self.db.get(ScanQueueUserState, self.user.id) is None:
            self.db.add(ScanQueueUserState(user_id=self.user.id))
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        for position in positions:
            (job_dir / f"{position}.jpg").write_bytes(_jpeg_bytes())
            self.db.add(ScanJobItem(
                job_id=job.id, user_id=self.user.id, position=position,
                image_path=f"{job.id}/{position}.jpg", content_type="image/jpeg",
                byte_size=4, batch_mode=batch, status="pending", resolved=False,
                attempts=0, transient_failures=0, next_attempt_at=now,
                created_at=now, updated_at=now,
            ))
        self.db.commit()
        return job

    def _process(self):
        claim = claim_next_scan_item(self.db)
        self.assertIsNotNone(claim)
        with patch("database.SessionLocal", self.Session):
            asyncio.run(scan_queue.process_claimed_scan_item(claim))
        self.db.expire_all()
        return claim

    def test_identified_scan_completes_with_mapped_matches_and_blob(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("identified")))
        job = self._job()
        claim = self._process()
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "done", item.error)
        self.assertEqual(item.matches[0]["id"], "sv03.5-043_en")
        self.assertEqual(item.recognized["_source"], "external")
        self.assertEqual(item.recognized["_identity_decision"], "IDENTIFIED")
        self.assertTrue(item.recognized["_identity_confident"])
        self.assertIsNone(item.recognized["name"])
        self.assertEqual(item.matcher_result["state"], "IDENTIFIED")
        self.assertEqual(item.matcher_result["session_lang"], "de")
        self.assertNotIn("candidates", item.matcher_result)
        # The job's session language reached the matcher.
        self.assertEqual(self.fake.requests[0].url.params["session_lang"], "de")
        self.assertEqual(job.id, item.job_id)

    def test_match_card_info_is_never_called(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("ambiguous")))
        self._job()
        with patch("api.recognize.match_card_info", new=AsyncMock()) as matcher, \
                patch("api.recognize.recognize_sanitized_card", new=AsyncMock()) as llm:
            claim = self._process()
        matcher.assert_not_awaited()
        llm.assert_not_awaited()
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "done")
        self.assertFalse(item.recognized["_identity_confident"])

    def test_every_state_completes_the_item(self):
        for state, name in STATE_FIXTURES.items():
            with self.subTest(state=state):
                self.fake.on("POST", "/identify", httpx.Response(200, json=fixture(name)))
                self._job()
                claim = self._process()
                item = self.db.get(ScanJobItem, claim.item_id)
                self.assertEqual(item.status, "done", item.error)
                self.assertEqual(item.matcher_result["state"], state)

    def test_503_is_a_retryable_backoff_not_a_failure(self):
        self.fake.on("POST", "/identify", httpx.Response(503))
        self._job()
        before = datetime.datetime.utcnow()
        claim = self._process()
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "retrying")
        self.assertEqual(item.transient_failures, 1)
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.retry_reason, "matcher_unavailable")
        self.assertGreaterEqual(
            item.next_attempt_at, before + datetime.timedelta(seconds=29)
        )
        self.assertIn("temporarily unavailable", item.error)

    def test_503_retry_after_is_honoured(self):
        self.fake.on("POST", "/identify", httpx.Response(503, headers={"Retry-After": "300"}))
        self._job()
        before = datetime.datetime.utcnow()
        claim = self._process()
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertGreaterEqual(item.next_attempt_at, before + datetime.timedelta(seconds=299))

    def test_connection_refused_is_retryable(self):
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        self.fake.on("POST", "/identify", refuse)
        self._job()
        claim = self._process()
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "retrying")
        self.assertIn("could not be reached", item.error)

    def test_timeout_fails_the_item_with_a_clear_message(self):
        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        self.fake.on("POST", "/identify", slow)
        self._job()
        claim = self._process()
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "failed")
        self.assertIn("did not answer within 20 s", item.error)
        self.assertIsNone(item.next_attempt_at)

    def test_rejected_photo_fails_permanently(self):
        self.fake.on("POST", "/identify", httpx.Response(422, json={"detail": "bad image"}))
        self._job()
        claim = self._process()
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "failed")
        self.assertIn("rejected the photo (422)", item.error)

    def test_composite_claim_is_returned_unresolved_and_requeued_individually(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("identified")))
        self._job(positions=(0, 1), batch=True)
        claim = claim_next_scan_item(self.db)
        self.assertTrue(claim.composite)
        with patch("database.SessionLocal", self.Session), \
                patch("api.recognize.recognize_composite_card_info", new=AsyncMock()) as llm:
            asyncio.run(scan_queue.process_claimed_scan_item(claim))
        llm.assert_not_awaited()
        self.db.expire_all()
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual([item.status for item in items], ["pending", "pending"])
        self.assertEqual([item.batch_mode for item in items], [False, False])
        self.assertEqual(self.fake.requests, [])
        # Both are now processed one at a time through the matcher.
        self._process()
        self._process()
        self.assertEqual(
            [item.status for item in self.db.query(ScanJobItem).all()], ["done", "done"]
        )
        self.assertEqual(len(self.fake.requests), 2)

    def test_composite_processor_direct_returns_none_for_every_position(self):
        results = asyncio.run(scan_queue.default_composite_processor(
            self.db, self.user.id, [b"a", b"b", b"c"], ["image/jpeg"] * 3
        ))
        self.assertEqual(results, [None, None, None])

    def test_retry_clears_the_matcher_blob(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("identified")))
        self._job()
        claim = self._process()
        item = self.db.get(ScanJobItem, claim.item_id)
        scan_queue.retry_scan_item(self.db, item)
        self.assertIsNone(item.matcher_result)


# --------------------------------------------------------------------------- trace


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class ExternalTraceTests(MatcherEnvMixin, unittest.TestCase):
    def setUp(self):
        self.trace_dir = tempfile.TemporaryDirectory()
        self.env_extra = {"SCAN_TRACE_DIR": self.trace_dir.name}
        self._start_env()
        self._db()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self._stop_env()
        self.trace_dir.cleanup()

    def _trace(self):
        return ScanTrace(enabled=True, user_id=7, mode="single", job_id=3, item_id=9,
                         provider="external", model="pokescan")

    def test_trace_stores_payload_and_artefacts_next_to_the_photo(self):
        payload = fixture("identified")
        self.fake.on("POST", "/identify", httpx.Response(200, json=payload))
        for kind in ("plane", "overlay"):
            self.fake.on(
                "GET", f"/trace/{payload['trace_id']}/{kind}.webp",
                httpx.Response(200, content=f"webp-{kind}".encode(),
                               headers={"content-type": "image/webp"}),
            )
        trace = self._trace()
        trace.set_image(b"jpeg")
        result = asyncio.run(external_matcher.recognize_with_matcher(
            self.db, b"jpeg", "image/jpeg", session_lang="en", trace=trace
        ))
        path = trace.save()
        data = json.loads(path.read_text())
        self.assertEqual(data["matcher"]["state"], "IDENTIFIED")
        self.assertEqual(len(data["matcher"]["candidates"]), 3)
        self.assertEqual(data["session_lang"], "en")
        self.assertEqual(data["decision"], {"mechanism": "external:IDENTIFIED",
                                            "selected": "sv03.5-043"})
        self.assertEqual(data["candidates"][0]["_score"], 0.91)
        stem = path.name[:-len(".json")]
        self.assertEqual((path.parent / f"{stem}.plane.webp").read_bytes(), b"webp-plane")
        self.assertEqual((path.parent / f"{stem}.overlay.webp").read_bytes(), b"webp-overlay")
        self.assertEqual(data["artefact_files"]["plane"], f"{stem}.plane.webp")
        self.assertTrue((path.parent / f"{stem}.jpg").is_file())
        self.assertEqual(result["_matcher"]["_pokecollector_trace_id"], trace.trace_id)
        from services.scan_trace import find_trace_artefact

        self.assertEqual(
            find_trace_artefact(7, 3, 9, trace.trace_id, "plane"),
            path.parent / f"{stem}.plane.webp",
        )

    def test_matcher_token_is_redacted_from_the_trace(self):
        payload = dict(fixture("identified_ja"))
        payload["hint"] = "debug echo: matcher-secret"
        self.fake.on("POST", "/identify", httpx.Response(200, json=payload))
        trace = self._trace()
        asyncio.run(external_matcher.recognize_with_matcher(
            self.db, b"jpeg", "image/jpeg", trace=trace
        ))
        text = trace.save().read_text()
        self.assertNotIn("matcher-secret", text)

    def test_artefact_failures_never_fail_the_scan(self):
        payload = fixture("identified")
        self.fake.on("POST", "/identify", httpx.Response(200, json=payload))

        def boom(request):
            raise httpx.ConnectError("gone", request=request)

        for kind in ("plane", "overlay"):
            self.fake.on("GET", f"/trace/{payload['trace_id']}/{kind}.webp", boom)
        trace = self._trace()
        result = asyncio.run(external_matcher.recognize_with_matcher(
            self.db, b"jpeg", "image/jpeg", trace=trace
        ))
        self.assertEqual(result["_identity_decision"], "IDENTIFIED")
        path = trace.save()
        self.assertNotIn("artefact_files", json.loads(path.read_text()))

    def test_no_trace_id_means_no_artefact_requests(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("identified_ja")))
        trace = self._trace()
        asyncio.run(external_matcher.recognize_with_matcher(
            self.db, b"jpeg", "image/jpeg", trace=trace
        ))
        self.assertEqual([r.url.path for r in self.fake.requests], ["/identify"])

    def test_disabled_trace_skips_artefacts(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("identified")))
        trace = ScanTrace(enabled=False, user_id=7, mode="single")
        result = asyncio.run(external_matcher.recognize_with_matcher(
            self.db, b"jpeg", "image/jpeg", trace=trace
        ))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertNotIn("_pokecollector_trace_id", result["_matcher"])


# --------------------------------------------------------------------------- API


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class ExternalApiTests(MatcherEnvMixin, unittest.TestCase):
    def setUp(self):
        self._start_env()
        self._db()
        self.user = User(username="ext-api", hashed_password="x", is_active=True, role="admin")
        self.db.add(self.user)
        self.db.commit()
        app = FastAPI()
        app.include_router(recognize_router, prefix="/api/cards")
        app.include_router(scan_jobs_router, prefix="/api/cards")
        app.include_router(settings_router, prefix="/api/settings")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()
        self._stop_env()

    def _enqueue(self, *, files=None, data=None):
        files = files or [("files", ("a.jpg", _jpeg_bytes(), "image/jpeg"))]
        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            return self.client.post("/api/cards/recognize/jobs", files=files, data=data or {})

    def test_enqueue_without_any_api_key_succeeds_and_stores_session_lang(self):
        # No gemini_api_key / openai_api_key anywhere, no capability proof.
        self.assertEqual(self.db.query(UserSetting).count(), 0)
        response = self._enqueue(data={"session_lang": "de"})
        self.assertEqual(response.status_code, 200, response.text)
        job = self.db.get(ScanJob, response.json()["id"])
        self.assertEqual(job.session_lang, "de")

    def test_direct_recognize_uses_the_matcher_and_maps_failures(self):
        self.fake.on("POST", "/identify", httpx.Response(200, json=fixture("identified")))
        files = {"file": ("a.jpg", _jpeg_bytes(), "image/jpeg")}
        response = self.client.post("/api/cards/recognize", files=files)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["_source"], "external")
        self.assertEqual(body["matches"][0]["id"], "sv03.5-043_en")
        self.assertEqual(body["_matcher"]["state"], "IDENTIFIED")

        self.fake.on("POST", "/identify", httpx.Response(503))
        self.assertEqual(self.client.post("/api/cards/recognize", files=files).status_code, 503)

        def slow(request):
            raise httpx.ReadTimeout("slow", request=request)

        self.fake.on("POST", "/identify", slow)
        self.assertEqual(self.client.post("/api/cards/recognize", files=files).status_code, 504)

    def test_enqueue_rejects_an_unknown_session_lang(self):
        response = self._enqueue(data={"session_lang": "ja"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.query(ScanJob).count(), 0)

    def test_enqueue_never_groups_photos_for_the_external_provider(self):
        files = [("files", (f"{i}.jpg", _jpeg_bytes(), "image/jpeg")) for i in range(3)]
        response = self._enqueue(files=files)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            [item.batch_mode for item in self.db.query(ScanJobItem).all()],
            [False, False, False],
        )

    def _done_item(self, name="identified"):
        job_id = self._enqueue(data={"session_lang": "en"}).json()["id"]
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == job_id).one()
        result = external_matcher.build_scan_result(fixture(name), self.db, "en")
        item.status = "done"
        item.recognized = result["recognized"]
        item.matches = result["matches"]
        item.matcher_result = result["_matcher"]
        self.db.commit()
        return job_id, item

    def test_resolve_and_add_accepts_a_mapped_candidate(self):
        job_id, item = self._done_item()
        self.db.add(Card(id="sv03.5-043_en", tcg_card_id="sv03.5-043", name="Oddish",
                         number="043", lang="en", is_custom=False))
        self.db.commit()
        match = item.matches[0]
        with patch("services.scan_trace.record_ground_truth", return_value=1):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{job_id}/items/{item.id}/resolve-and-add",
                json={
                    "confirmed_card_id": match["tcg_card_id"],
                    "card_id": match["id"],
                    "quantity": 1,
                    "condition": "NM",
                    "variant": "Normal",
                    "lang": match["lang"],
                    "purchase_price": None,
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["item"]["resolved"])
        self.assertEqual(self.db.query(CollectionItem).one().card_id, "sv03.5-043_en")

    def test_item_payload_flags_matcher_and_blob_endpoint_serves_it(self):
        job_id, item = self._done_item()
        detail = self.client.get(f"/api/cards/recognize/jobs/{job_id}").json()
        self.assertTrue(detail["items"][0]["has_matcher"])
        self.assertEqual(detail["items"][0]["recognized"]["_source"], "external")

        response = self.client.get(f"/api/cards/recognize/jobs/{job_id}/items/{item.id}/matcher")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["session_lang"], "en")
        self.assertEqual(body["matcher"]["state"], "IDENTIFIED")
        self.assertEqual(body["matcher"]["geometry"]["image_size"], [1200, 1600])
        self.assertNotIn("candidates", body["matcher"])
        base = f"/api/cards/recognize/jobs/{job_id}/items/{item.id}"
        self.assertEqual(body["artefacts"]["source"], f"{base}/image")
        self.assertEqual(body["artefacts"]["plane"], f"{base}/matcher/plane.webp")

        trace_id = fixture("identified")["trace_id"]
        self.fake.on("GET", f"/trace/{trace_id}/plane.webp",
                     httpx.Response(200, content=b"plane", headers={"content-type": "image/webp"}))
        plane = self.client.get(f"{base}/matcher/plane.webp")
        self.assertEqual(plane.status_code, 200)
        self.assertEqual(plane.content, b"plane")
        # The matcher has no overlay for this trace: a tolerant 404.
        self.assertEqual(self.client.get(f"{base}/matcher/overlay.webp").status_code, 404)
        self.assertEqual(self.client.get(f"{base}/matcher/secret.webp").status_code, 404)

    def test_blob_endpoint_404s_for_llm_items_and_other_users(self):
        job_id = self._enqueue().json()["id"]
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == job_id).one()
        self.assertEqual(
            self.client.get(f"/api/cards/recognize/jobs/{job_id}/items/{item.id}/matcher").status_code,
            404,
        )
        job_id, item = self._done_item()
        other = User(username="ext-other", hashed_password="x", is_active=True)
        self.db.add(other)
        self.db.commit()
        self.app.dependency_overrides[get_current_user] = lambda: other
        self.assertEqual(
            self.client.get(f"/api/cards/recognize/jobs/{job_id}/items/{item.id}/matcher").status_code,
            404,
        )

    def test_blob_without_trace_lists_no_artefacts(self):
        job_id, item = self._done_item("identified_ja")
        body = self.client.get(
            f"/api/cards/recognize/jobs/{job_id}/items/{item.id}/matcher"
        ).json()
        self.assertIsNone(body["artefacts"]["plane"])
        self.assertIsNone(body["artefacts"]["overlay"])

    def test_ref_proxy_serves_thumbnails_and_candidate_image_endpoint(self):
        self.fake.on("GET", "/ref/ja:SV2a-043",
                     httpx.Response(200, content=b"ref", headers={"content-type": "image/webp"}))
        response = self.client.get("/api/cards/recognize/matcher/ref/ja:SV2a-043")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ref")
        self.assertEqual(self.fake.requests[-1].headers["authorization"], "Bearer matcher-secret")
        self.assertEqual(
            self.client.get("/api/cards/recognize/matcher/ref/nope").status_code, 404
        )
        job_id, item = self._done_item("identified_ja")
        full = self.client.get(
            f"/api/cards/recognize/jobs/{job_id}/items/{item.id}/candidates/0/image"
        )
        self.assertEqual(full.status_code, 200)
        self.assertEqual(full.content, b"ref")

    def test_settings_lists_external_first_and_ready_without_a_key(self):
        config = self.client.get("/api/settings/scanner").json()
        self.assertEqual(config["provider"], "external")
        self.assertEqual(config["status"], "ready")
        self.assertEqual(config["model"], "pokescan")
        external = next(p for p in config["providers"] if p["id"] == "external")
        self.assertFalse(external["requires_api_key"])
        self.assertEqual(external["kind"], "matcher")
        admin = {p["id"]: p for p in config["administrator"]["providers"]}
        self.assertTrue(admin["external"]["enabled"])
        self.assertEqual(admin["external"]["endpoint"], MATCHER_URL)

    def test_scanner_test_uses_health_and_saves(self):
        self.fake.on("GET", "/health", httpx.Response(200, json={"ok": True, "bundle_version": "v12"}))
        self.db.add(UserSetting(user_id=self.user.id, key="scanner_provider", value="gemini"))
        self.db.commit()
        response = self.client.post("/api/settings/scanner/test", json={
            "provider": "external", "model": "pokescan", "save_on_success": True,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["health"]["bundle_version"], "v12")
        self.assertEqual([r.url.path for r in self.fake.requests], ["/health"])
        rows = {row.key: row.value for row in self.db.query(UserSetting).all()}
        self.assertEqual(rows["scanner_provider"], "external")
        self.assertIn("scanner_capability_external", rows)
        self.assertNotIn("openai_api_key", rows)

    def test_scanner_test_reports_a_down_matcher(self):
        self.fake.on("GET", "/health", httpx.Response(503))
        response = self.client.post("/api/settings/scanner/test", json={
            "provider": "external", "model": "pokescan", "save_on_success": True,
        })
        self.assertEqual(response.status_code, 502)
        self.assertIn("temporarily unavailable", response.json()["detail"])

    def test_external_status_endpoint_proxies_health_and_bundle(self):
        self.fake.on("GET", "/health", httpx.Response(200, json={"ok": True, "commit": "8f0ff3b"}))
        self.fake.on("GET", "/bundle", httpx.Response(200, json={"supported_sets": {"en": ["sv1"]}}))
        body = self.client.get("/api/settings/scanner/external").json()
        self.assertTrue(body["configured"])
        self.assertEqual(body["health"]["commit"], "8f0ff3b")
        self.assertEqual(body["bundle"]["supported_sets"]["en"], ["sv1"])
        self.assertIsNone(body["error"])

    def test_external_status_endpoint_when_down_or_unconfigured(self):
        self.fake.on("GET", "/health", httpx.Response(503))
        body = self.client.get("/api/settings/scanner/external").json()
        self.assertIsNone(body["health"])
        self.assertIn("temporarily unavailable", body["error"])
        with patch.dict(os.environ, {"EXTERNAL_MATCHER_URL": ""}):
            body = self.client.get("/api/settings/scanner/external").json()
        self.assertFalse(body["configured"])

    def test_external_status_endpoint_is_admin_only_in_multi_user_mode(self):
        self.user.role = "user"
        self.db.commit()
        self.fake.on("GET", "/health", httpx.Response(200, json={"ok": True}))
        self.fake.on("GET", "/bundle", httpx.Response(200, json={}))
        with patch("api.auth.multi_user_enabled", return_value=True):
            self.assertEqual(self.client.get("/api/settings/scanner/external").status_code, 403)
        with patch("api.auth.multi_user_enabled", return_value=False):
            self.assertEqual(self.client.get("/api/settings/scanner/external").status_code, 200)


if __name__ == "__main__":
    unittest.main()
