"""The per-user "Scanner v2 (Beta)" switch, and what it does to a scan.

Two halves: the setting itself must round-trip through the ordinary settings
contract and stay private to one user, and the background scan worker must
route on it -- offline when it is on, and the untouched provider path when it
is off.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api import recognize_local
    from api.settings import _get_user_settings, set_setting, update_settings
    from database import Base
    from models import User, UserSetting
    from services import scan_queue
    from services.local_scanner import (
        LOCAL_SCANNER_SETTING_KEY,
        local_scanner_enabled,
    )
    from services.scan_providers import ScanProvider

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _local_result(name):
    """The shape /recognize/local returns, trimmed to what the queue stores."""
    return {
        "recognized": {"name": None, "number": None, "language": None},
        "matches": [{"id": f"card-{name}", "_distance": 3, "_confidence": "high"}],
        "_identity_confident": True,
        "_identity_decision": "local_image",
        "_source": "local_fingerprint",
    }


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed")
class LocalScannerSettingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="scan-v2", hashed_password="x", is_active=True)
        self.other = User(username="scan-v1", hashed_password="x", is_active=True)
        self.db.add_all([self.user, self.other])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _stored(self, user):
        row = (
            self.db.query(UserSetting)
            .filter(
                UserSetting.user_id == user.id,
                UserSetting.key == LOCAL_SCANNER_SETTING_KEY,
            )
            .first()
        )
        return None if row is None else row.value

    def test_the_toggle_is_off_until_a_user_turns_it_on(self):
        self.assertEqual(
            _get_user_settings(self.db, self.user.id)[LOCAL_SCANNER_SETTING_KEY],
            "false",
        )
        self.assertIsNone(self._stored(self.user))
        self.assertFalse(local_scanner_enabled(self.db, self.user.id))

    def test_the_toggle_survives_a_reload_and_can_be_switched_back_off(self):
        update_settings({LOCAL_SCANNER_SETTING_KEY: "true"}, self.db, self.user)
        self.assertEqual(self._stored(self.user), "true")
        # A fresh session is what a page reload actually gets.
        reloaded = sessionmaker(bind=self.engine)()
        try:
            self.assertEqual(
                _get_user_settings(reloaded, self.user.id)[LOCAL_SCANNER_SETTING_KEY],
                "true",
            )
            self.assertTrue(local_scanner_enabled(reloaded, self.user.id))
        finally:
            reloaded.close()

        update_settings({LOCAL_SCANNER_SETTING_KEY: "false"}, self.db, self.user)
        self.assertEqual(self._stored(self.user), "false")
        self.assertFalse(local_scanner_enabled(self.db, self.user.id))

    def test_one_users_choice_never_reaches_another(self):
        update_settings({LOCAL_SCANNER_SETTING_KEY: "true"}, self.db, self.user)
        self.assertTrue(local_scanner_enabled(self.db, self.user.id))
        self.assertFalse(local_scanner_enabled(self.db, self.other.id))
        self.assertEqual(
            _get_user_settings(self.db, self.other.id)[LOCAL_SCANNER_SETTING_KEY],
            "false",
        )

    def test_the_single_key_endpoint_writes_it_too(self):
        set_setting(
            LOCAL_SCANNER_SETTING_KEY, {"value": "true"}, self.db, self.user
        )
        self.assertTrue(local_scanner_enabled(self.db, self.user.id))

    def test_only_a_true_value_turns_offline_matching_on(self):
        for value, expected in [
            ("on", True), ("1", True), ("yes", True), ("TRUE", True),
            ("off", False), ("0", False), ("", False), ("maybe", False),
        ]:
            with self.subTest(value=value):
                update_settings({LOCAL_SCANNER_SETTING_KEY: value}, self.db, self.user)
                self.assertEqual(local_scanner_enabled(self.db, self.user.id), expected)
                # Whatever was sent, only the canonical text is ever stored.
                self.assertIn(self._stored(self.user), {"true", "false"})

    def test_a_row_written_outside_the_endpoint_is_read_forgivingly(self):
        self.db.add(UserSetting(
            user_id=self.user.id, key=LOCAL_SCANNER_SETTING_KEY, value=" True ",
        ))
        self.db.commit()
        self.assertTrue(local_scanner_enabled(self.db, self.user.id))


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed")
class LocalScannerQueueRoutingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="queue-v2", hashed_password="x", is_active=True)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _give_provider_a_credential(self):
        """Only for the test that exercises the PROVIDER path.

        Upstream moved the credential check inside `default_scan_processor`, so
        the provider path now 400s without one before it reaches anything worth
        asserting. Deliberately not in setUp: every other test here turns the
        toggle on, and their whole claim is that the offline path answers with
        no credential configured at all. Handing them one for free would make
        that claim unfalsifiable.
        """
        self.db.add(UserSetting(
            user_id=self.user.id, key="gemini_api_key", value="test-key",
        ))
        self.db.commit()

    def _turn_on(self):
        self.db.add(UserSetting(
            user_id=self.user.id, key=LOCAL_SCANNER_SETTING_KEY, value="true",
        ))
        self.db.commit()

    def test_an_individual_photo_is_matched_offline_with_no_provider_call(self):
        self._turn_on()
        generate = AsyncMock()
        recognize = AsyncMock(return_value=_local_result("pikachu"))

        with patch.object(ScanProvider, "generate_text", new=generate), patch.object(
            recognize_local, "recognize_sanitized_card_locally", new=recognize
        ):
            result = asyncio.run(scan_queue.default_scan_processor(
                self.db, self.user.id, b"stored-photo", "image/jpeg",
                job_id=7, item_id=8,
            ))

        self.assertEqual(result["_source"], "local_fingerprint")
        generate.assert_not_awaited()
        self.assertEqual(recognize.await_args.args[1], b"stored-photo")

    def test_leaving_the_toggle_off_keeps_todays_provider_path(self):
        self._give_provider_a_credential()
        recognize = AsyncMock(return_value=_local_result("pikachu"))
        provider = AsyncMock(return_value={"recognized": {}, "matches": []})

        with patch.object(
            recognize_local, "recognize_sanitized_card_locally", new=recognize
        ), patch("api.recognize.recognize_sanitized_card", new=provider):
            result = asyncio.run(scan_queue.default_scan_processor(
                self.db, self.user.id, b"stored-photo", "image/jpeg",
            ))

        recognize.assert_not_awaited()
        provider.assert_awaited_once()
        self.assertEqual(result, {"recognized": {}, "matches": []})

    def test_an_inactive_owner_is_refused_before_any_recognition(self):
        self._turn_on()
        self.user.is_active = False
        self.db.commit()
        recognize = AsyncMock()

        with patch.object(
            recognize_local, "recognize_sanitized_card_locally", new=recognize
        ), self.assertRaises(scan_queue.PermanentScanError):
            asyncio.run(scan_queue.default_scan_processor(
                self.db, self.user.id, b"stored-photo", "image/jpeg",
            ))
        recognize.assert_not_awaited()

    def test_a_staged_group_is_matched_one_photo_at_a_time(self):
        self._turn_on()
        composite = AsyncMock()
        recognize = AsyncMock(side_effect=[
            _local_result("a"), _local_result("b"), _local_result("c"),
        ])

        with patch(
            "api.recognize.recognize_composite_card_info", new=composite
        ), patch.object(
            recognize_local, "recognize_sanitized_card_locally", new=recognize
        ):
            results = asyncio.run(scan_queue.default_composite_processor(
                self.db, self.user.id,
                [b"one", b"two", b"three"],
                ["image/jpeg"] * 3,
                job_id=7, item_ids=[10, 11, 12],
            ))

        # Never composited: a frame holding four cards is the case offline
        # matching cannot box, and there is no provider call to save.
        composite.assert_not_awaited()
        self.assertEqual(
            [result["matches"][0]["id"] for result in results],
            ["card-a", "card-b", "card-c"],
        )
        self.assertEqual(
            [call.args[1] for call in recognize.await_args_list],
            [b"one", b"two", b"three"],
        )

    def test_one_unreadable_photo_only_sends_itself_back_for_an_individual_scan(self):
        self._turn_on()
        recognize = AsyncMock(side_effect=[
            _local_result("a"),
            HTTPException(status_code=400, detail="Could not read the uploaded image."),
            _local_result("c"),
        ])

        with patch.object(
            recognize_local, "recognize_sanitized_card_locally", new=recognize
        ):
            results = asyncio.run(scan_queue.default_composite_processor(
                self.db, self.user.id,
                [b"one", b"two", b"three"],
                ["image/jpeg"] * 3,
                job_id=7, item_ids=[10, 11, 12],
            ))

        self.assertEqual(
            [None if result is None else result["matches"][0]["id"] for result in results],
            ["card-a", None, "card-c"],
        )

    def test_an_index_that_is_not_ready_yet_retries_the_whole_group_later(self):
        self._turn_on()
        not_ready = HTTPException(
            status_code=503, detail="Card fingerprints are still being built."
        )

        with patch.object(
            recognize_local,
            "recognize_sanitized_card_locally",
            new=AsyncMock(side_effect=not_ready),
        ), self.assertRaises(HTTPException) as caught:
            asyncio.run(scan_queue.default_composite_processor(
                self.db, self.user.id, [b"one", b"two"], ["image/jpeg"] * 2,
                job_id=7, item_ids=[10, 11],
            ))

        self.assertEqual(caught.exception.status_code, 503)
        self.assertIsInstance(
            scan_queue._scan_error_from_http(caught.exception),
            scan_queue.TransientScanError,
        )

    def test_a_local_scan_is_not_attributed_to_a_vision_provider(self):
        from services.scan_trace import create_scan_trace as real_create

        self._turn_on()
        traces = []

        def capture(db, user_id, **kwargs):
            traces.append(kwargs)
            return real_create(db, user_id, **kwargs)

        with patch("services.scan_trace.create_scan_trace", new=capture), patch.object(
            recognize_local,
            "recognize_sanitized_card_locally",
            new=AsyncMock(return_value=_local_result("pikachu")),
        ):
            asyncio.run(scan_queue.default_scan_processor(
                self.db, self.user.id, b"stored-photo", "image/jpeg",
                job_id=7, item_id=8,
            ))

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]["provider"], scan_queue.LOCAL_SCANNER_PROVIDER)
        self.assertEqual(traces[0]["model"], scan_queue.LOCAL_SCANNER_MODEL)
        self.assertNotIn(traces[0]["provider"], {"gemini", "openai"})


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed")
class AlreadySanitizedPhotoTests(unittest.TestCase):
    """The queue stores sanitized JPEGs; hashing a re-encode of one is worse."""

    def test_an_upload_is_sanitized_before_it_is_fingerprinted(self):
        with patch.object(
            recognize_local, "sanitize_image_bytes"
        ) as sanitize, patch.object(
            recognize_local, "photo_hash_variants", return_value=[]
        ) as fingerprint:
            sanitize.return_value.data = b"cleaned"
            recognize_local._match_photo(b"raw-upload", object())

        sanitize.assert_called_once_with(b"raw-upload")
        fingerprint.assert_called_once_with(b"cleaned")

    def test_a_stored_scan_photo_is_never_re_encoded(self):
        with patch.object(
            recognize_local, "sanitize_image_bytes"
        ) as sanitize, patch.object(
            recognize_local, "photo_hash_variants", return_value=[]
        ) as fingerprint:
            recognize_local._match_photo(b"already-clean", object(), sanitized=True)

        sanitize.assert_not_called()
        fingerprint.assert_called_once_with(b"already-clean")


if __name__ == "__main__":
    unittest.main()
