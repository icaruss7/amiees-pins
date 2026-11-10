import unittest
from datetime import datetime, timezone
from unittest import mock

import x_publish as publisher


def row(**updates):
    item = {
        "post_id": "x-test-001",
        "release_at": "2026-11-09 14:00:00",
        "status": "queued",
        "post_type": "question",
        "text": "Which nature-inspired motif would you wear most?",
        "media_type": "TEXT",
        "image_url": "",
        "alt_text": "",
        "product_url": "",
        "utm_campaign": "x-conversation",
    }
    item.update(updates)
    return item


class XPublisherTests(unittest.TestCase):
    def test_rejects_private_image_url(self):
        with self.assertRaisesRegex(ValueError, "Local image"):
            publisher.validate_public_https("https://localhost/image.png")

    def test_rejects_long_post(self):
        with self.assertRaisesRegex(ValueError, "280"):
            publisher.validate_row(row(text="x" * 281))

    def test_urls_use_x_weighted_length(self):
        self.assertEqual(publisher.weighted_length("See https://amiees.com/products/a-very-long-slug"), 27)

    def test_published_post_is_not_eligible(self):
        state = {"x-test-001": {"phase": "published", "x_post_id": "123"}}
        now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
        self.assertFalse(publisher.eligible(row(), state, now))

    def test_account_identity_rejects_wrong_username(self):
        with mock.patch.object(publisher, "EXPECTED_USERNAME", "amiees"), \
             mock.patch.object(publisher, "api_json",
                               return_value={"data": {"id": "1", "username": "other"}}):
            with self.assertRaisesRegex(RuntimeError, "X_EXPECTED_USERNAME"):
                publisher.account_identity()

    def test_publish_checkpoints_before_publication(self):
        state = {}
        calls = []

        def api(method, url, payload=None, query=None):
            calls.append((method, url))
            return {"data": {"id": "post-1"}}

        def checkpoint(value):
            calls.append(("checkpoint", value["x-test-001"]["phase"]))

        with mock.patch.object(publisher, "api_json", side_effect=api), \
             mock.patch.object(publisher, "checkpoint", side_effect=checkpoint):
            publisher.publish(row(), state, "amiees")

        publishing_checkpoint = calls.index(("checkpoint", "publishing"))
        public_call = calls.index(("POST", "https://api.x.com/2/tweets"))
        self.assertLess(publishing_checkpoint, public_call)
        self.assertEqual(state["x-test-001"]["phase"], "published")

    def test_oauth_header_is_deterministic(self):
        with mock.patch.object(publisher, "API_KEY", "key"), \
             mock.patch.object(publisher, "API_SECRET", "secret"), \
             mock.patch.object(publisher, "ACCESS_TOKEN", "token"), \
             mock.patch.object(publisher, "ACCESS_TOKEN_SECRET", "token-secret"):
            header = publisher.oauth_header(
                "GET", "https://api.x.com/2/users/me?user.fields=id%2Cusername",
                nonce="abc", timestamp=1700000000)
        self.assertIn('oauth_consumer_key="key"', header)
        self.assertIn('oauth_signature_method="HMAC-SHA1"', header)
        self.assertIn("oauth_signature=", header)


if __name__ == "__main__":
    unittest.main()
