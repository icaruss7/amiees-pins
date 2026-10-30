"""Native audio, approval binding and additional Reel spacing regressions."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import ig_pipeline_media as media
import ig_publish as publisher
import test_ig_publish as fixtures


class NativeAudioTests(unittest.TestCase):
    def test_commercial_review_required_and_platform_limited(self):
        record = {"platform": "instagram", "format": "REELS", "instagram_audio": {
            "configuration": {"audio_id": "123", "audio_volume": 70, "video_volume": 1},
            "title": "Fixture", "artist": "Fixture artist", "catalog_collected_at": "2026-10-30T00:00:00Z",
            "rights_review": {"commercial_use_verified": True, "by": "Reviewer",
                              "at": "2026-10-30T00:00:00Z", "source_url": "https://www.facebook.com/sound/collection/"}}}
        self.assertEqual(media.native_audio(record)["audio_id"], "123")
        for change in (lambda r: r.update(platform="pinterest"),
                       lambda r: r["instagram_audio"].update(rights_review=[]),
                       lambda r: r["instagram_audio"]["rights_review"].update(commercial_use_verified=False),
                       lambda r: r["instagram_audio"]["configuration"].update(audio_volume=0)):
            invalid = copy.deepcopy(record)
            change(invalid)
            with self.assertRaises(ValueError):
                media.native_audio(invalid)

    def test_legacy_fingerprint_unchanged_and_audio_change_invalidates_container(self):
        row = {"post_id": "legacy", "caption": "Original", "images": "https://example.com/image.jpg"}
        keys = ("post_id", "caption", "images", "media_type", "video_url", "video_sha256", "cover_url")
        old = hashlib.sha256(json.dumps({k: row.get(k, "") for k in keys}, sort_keys=True).encode()).hexdigest()
        self.assertEqual(publisher.fingerprint(row), old)
        row["audio_configuration"] = {"audio_id": "123", "audio_volume": 70, "video_volume": 1}
        approved = publisher.fingerprint(row)
        row["audio_configuration"]["audio_id"] = "456"
        self.assertNotEqual(approved, publisher.fingerprint(row))

    def test_extra_slots_keep_normal_posts_twenty_hours_apart(self):
        now = datetime(2026, 10, 30, 13, tzinfo=timezone.utc)
        normal, extra = {"post_id": "nature"}, {"post_id": "extra", "schedule_mode": "additional_reel"}
        rows = [normal, extra]
        recent = {"nature": {"posted_at": (now - timedelta(hours=9)).isoformat()}}
        self.assertTrue(publisher.spacing_ready(extra, recent, rows, now))
        self.assertFalse(publisher.spacing_ready(normal, recent, rows, now))
        recent = {"extra": {"posted_at": (now - timedelta(hours=7)).isoformat()}}
        self.assertFalse(publisher.spacing_ready(normal, recent, rows, now))
        recent["extra"]["posted_at"] = (now - timedelta(hours=8)).isoformat()
        self.assertTrue(publisher.spacing_ready(normal, recent, rows, now))

    def test_native_track_and_ai_label_sent_when_creating_reel(self):
        fixture = fixtures.PublisherTests()
        fixture.setUp()
        fixture.row.update(media_type="REELS", images="", video_url="https://cdn.example.com/a.mp4",
                           video_sha256="a" * 64, is_ai_generated=True,
                           audio_configuration={"audio_id": "123", "audio_volume": 70, "video_volume": 1})
        with patch.object(publisher, "call", side_effect=fixture.api), patch.object(publisher, "checkpoint", side_effect=fixture.checkpoint), patch.object(publisher, "verify_video"):
            publisher.publish("account-1", fixture.row, fixture.state)
        creation = next(params for path, params in fixture.events if path == "account-1/media")
        self.assertEqual(json.loads(creation["audio_configuration"]), fixture.row["audio_configuration"])
        self.assertEqual(creation["is_ai_generated"], "true")


if __name__ == "__main__":
    unittest.main()
