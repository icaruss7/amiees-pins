import unittest
from datetime import datetime, timezone
from unittest import mock

import threads_publish as publisher


def row(**updates):
    item = {
        "post_id": "threads-test-001",
        "release_at": "2026-11-09 14:00:00",
        "status": "queued",
        "post_type": "question",
        "text": "Which nature-inspired motif would you wear most?",
        "media_type": "TEXT",
        "image_url": "",
        "alt_text": "",
        "product_url": "",
        "utm_campaign": "threads-conversation",
    }
    item.update(updates)
    return item


class ThreadsPublisherTests(unittest.TestCase):
    def test_rejects_private_image_url(self):
        with self.assertRaisesRegex(ValueError, "Local image"):
            publisher.validate_row(row(media_type="IMAGE", image_url="https://localhost/image.png"))

    def test_rejects_text_over_500_characters(self):
        with self.assertRaisesRegex(ValueError, "500"):
            publisher.validate_row(row(text="x" * 501))

    def test_published_post_is_not_eligible(self):
        state = {"threads-test-001": {"phase": "published", "thread_id": "123"}}
        now = datetime(2026, 11, 9, 20, 0, tzinfo=timezone.utc)
        self.assertFalse(publisher.eligible(row(), state, now))

    def test_publish_checkpoints_before_publication(self):
        state = {}
        calls = []

        def api(method, path, **params):
            calls.append((method, path))
            if path.endswith("/threads"):
                return {"id": "container-1"}
            if path.endswith("/threads_publish"):
                return {"id": "thread-1"}
            return {"permalink": "https://www.threads.net/@amiees/post/1"}

        def checkpoint(value):
            calls.append(("checkpoint", value["threads-test-001"]["phase"]))

        with mock.patch.object(publisher, "call", side_effect=api), \
             mock.patch.object(publisher, "wait_ready", return_value="FINISHED"), \
             mock.patch.object(publisher, "checkpoint", side_effect=checkpoint):
            publisher.publish(row(), state, "threads-user-1")

        self.assertIn(("checkpoint", "ready"), calls)
        self.assertIn(("checkpoint", "publishing"), calls)
        self.assertIn(("checkpoint", "published"), calls)
        publishing_checkpoint = calls.index(("checkpoint", "publishing"))
        public_call = calls.index(("POST", "threads-user-1/threads_publish"))
        self.assertLess(publishing_checkpoint, public_call)


    def test_account_identity_accepts_connected_profile(self):
        with mock.patch.object(publisher, "call",
                               return_value={"id": "threads-user-1", "username": "amiees"}):
            self.assertEqual(publisher.account_identity(), ("threads-user-1", "amiees"))

    def test_account_identity_rejects_wrong_expected_username(self):
        with mock.patch.object(publisher, "EXPECTED_USERNAME", "amiees"), \
             mock.patch.object(publisher, "call",
                               return_value={"id": "threads-user-1", "username": "anotherbrand"}):
            with self.assertRaisesRegex(RuntimeError, "EXPECTED_USERNAME"):
                publisher.account_identity()


if __name__ == "__main__":
    unittest.main()
