import contextlib
import io
import unittest
from unittest import mock

import tiktok_publish as publisher


def row(**updates):
    item = {
        "post_id": "tt-test-001",
        "release_at": "2026-11-10 14:00:00",
        "status": "queued",
        "product": "dolphin-dreams-ring",
        "caption": "A little piece of the ocean to keep close. #Jewelry #Amiees",
        "video_url": "https://cdn.shopify.com/videos/c/o/v/example.mp4",
        "video_sha256": "a" * 64,
        "is_ai_generated": "true",
        "sound_search": "Trending dreamy instrumental from Commercial Sounds",
        "product_url": "https://amiees.com/products/dolphin-dreams-ring",
    }
    item.update(updates)
    return item


class TikTokPublisherTests(unittest.TestCase):
    def account_query(self, query, variables=None):
        if query == publisher.ACCOUNT_QUERY:
            return {"account": {"organizations": [{"id": "org-1"}]}}
        if query == publisher.CHANNELS_QUERY:
            return {"channels": [{
                "id": "673d4900da2e3cb64c96536a",
                "name": "amiees.com",
                "service": "tiktok",
                "isDisconnected": False,
                "isLocked": False,
            }]}
        raise AssertionError("Unexpected query")

    def test_discovers_exact_connected_tiktok_channel(self):
        self.assertEqual(
            publisher.discover_tiktok_channel(self.account_query),
            ("673d4900da2e3cb64c96536a", "amiees.com"),
        )

    def test_builds_notification_video_input(self):
        result = publisher.post_input(row(), "channel-tt")
        self.assertEqual(result["schedulingType"], "notification")
        self.assertEqual(result["mode"], "shareNow")
        self.assertEqual(result["assets"][0]["video"]["url"], row()["video_url"])
        self.assertTrue(result["metadata"]["tiktok"]["isAiGenerated"])
        self.assertNotIn("sound_search", result["text"])

    def test_rejects_more_than_five_hashtags(self):
        bad = row(caption="#one #two #three #four #five #six")
        with self.assertRaisesRegex(ValueError, "more than five"):
            publisher.validate_row(bad)

    def test_rejects_em_dash_in_caption(self):
        with self.assertRaisesRegex(ValueError, "em dashes"):
            publisher.validate_row(row(caption="Jewelry \N{EM DASH} made simple."))

    def test_notification_checkpoints_before_buffer_mutation(self):
        state = {}
        calls = []

        def checkpoint(value):
            calls.append(("checkpoint", value["tt-test-001"]["phase"]))

        def buffer(query, variables=None):
            calls.append(("buffer", query))
            return {
                "createPost": {
                    "__typename": "PostActionSuccess",
                    "post": {"id": "buffer-tt-1", "status": "buffer"},
                }
            }

        with mock.patch.object(publisher, "checkpoint", side_effect=checkpoint), \
             mock.patch.object(publisher, "buffer_json", side_effect=buffer), \
             contextlib.redirect_stdout(io.StringIO()):
            publisher.schedule_notification(row(), state, "channel-tt")

        self.assertLess(
            calls.index(("checkpoint", "scheduling_notification")),
            next(i for i, call in enumerate(calls) if call[0] == "buffer"),
        )
        self.assertEqual(state["tt-test-001"]["phase"], "notification_scheduled")
        self.assertEqual(state["tt-test-001"]["buffer_post_id"], "buffer-tt-1")
        self.assertEqual(state["tt-test-001"]["sound_search"], row()["sound_search"])


if __name__ == "__main__":
    unittest.main()
