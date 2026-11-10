import contextlib
import io
import unittest
from unittest import mock

import x_buffer_publish as publisher


def row(**updates):
    item = {
        "post_id": "x-test-001",
        "release_at": "2026-11-09 14:00:00",
        "status": "queued",
        "post_type": "product",
        "text": "A small test post.",
        "media_type": "IMAGE",
        "image_url": "https://cdn.shopify.com/image.jpg",
        "alt_text": "Silver bracelet on cream linen.",
        "product_url": "https://amiees.com/products/test",
        "utm_campaign": "x-test",
    }
    item.update(updates)
    return item


class BufferXPublisherTests(unittest.TestCase):
    def account_query(self, query, variables=None):
        if query == publisher.ACCOUNT_QUERY:
            return {"account": {"organizations": [{"id": "org-1"}]}}
        if query == publisher.CHANNELS_QUERY:
            return {"channels": [{
                "id": "channel-x",
                "name": "AmieesShop",
                "service": "twitter",
                "isDisconnected": False,
                "isLocked": False,
            }]}
        raise AssertionError("Unexpected query")

    def test_discovers_exact_connected_x_channel(self):
        with mock.patch.object(publisher, "EXPECTED_USERNAME", "amieesshop"):
            self.assertEqual(
                publisher.discover_x_channel(self.account_query),
                ("channel-x", "AmieesShop"),
            )

    def test_rejects_wrong_x_channel(self):
        def wrong(query, variables=None):
            if query == publisher.ACCOUNT_QUERY:
                return {"account": {"organizations": [{"id": "org-1"}]}}
            return {"channels": [{
                "id": "wrong",
                "name": "PersonalAccount",
                "service": "twitter",
                "isDisconnected": False,
                "isLocked": False,
            }]}
        with mock.patch.object(publisher, "EXPECTED_USERNAME", "amieesshop"):
            with self.assertRaisesRegex(publisher.BufferError, "missing or ambiguous"):
                publisher.discover_x_channel(wrong)

    def test_image_input_preserves_alt_text(self):
        result = publisher.post_input(row(), "channel-x")
        image = result["assets"][0]["image"]
        self.assertEqual(image["url"], "https://cdn.shopify.com/image.jpg")
        self.assertEqual(image["metadata"]["altText"], "Silver bracelet on cream linen.")
        self.assertEqual(result["mode"], "shareNow")

    def test_submit_checkpoints_before_buffer_mutation(self):
        state = {}
        calls = []

        def checkpoint(value):
            calls.append(("checkpoint", value["x-test-001"]["phase"]))

        def buffer(query, variables=None):
            calls.append(("buffer", query))
            return {
                "createPost": {
                    "__typename": "PostActionSuccess",
                    "post": {
                        "id": "buffer-1",
                        "status": "sent",
                        "externalLink": "https://x.com/AmieesShop/status/1",
                    },
                }
            }

        with mock.patch.object(publisher.core, "checkpoint", side_effect=checkpoint), \
             mock.patch.object(publisher, "buffer_json", side_effect=buffer), \
             contextlib.redirect_stdout(io.StringIO()):
            publisher.submit(row(media_type="TEXT", image_url="", alt_text=""), state, "channel-x")

        self.assertLess(
            calls.index(("checkpoint", "publishing")),
            next(i for i, call in enumerate(calls) if call[0] == "buffer"),
        )
        self.assertEqual(state["x-test-001"]["phase"], "published")
        self.assertEqual(state["x-test-001"]["buffer_post_id"], "buffer-1")
        self.assertEqual(state["x-test-001"]["transport"], "buffer")


if __name__ == "__main__":
    unittest.main()
