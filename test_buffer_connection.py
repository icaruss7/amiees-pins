"""Offline connection tests. No real credentials or social writes."""

import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import buffer_connection as b


class BufferConnectionTests(unittest.TestCase):
    def setUp(self):
        self.channel = {"id": "expected", "name": "amiees.com", "service": "tiktok",
                        "isDisconnected": False, "isLocked": False}
        self.calls = []

    def query(self, key, query, variables=None):
        self.calls.append((query, variables))
        if query == b.ACCOUNT:
            return {"account": {"organizations": [{"id": "org-1"}]}}
        return {"channels": [self.channel]}

    def check(self):
        return b.check_connection("fixture-secret", "expected", "@amiees.com", self.query)

    def response(self, raw):
        with patch.object(b.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(raw)
            return b.query_buffer("fixture-secret", b.ACCOUNT)

    def test_connection_is_read_only_and_does_not_claim_publication(self):
        result = self.check()
        self.assertEqual(result["status"], "buffer_connected")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["content_changed"])
        self.assertFalse(result["live_video_publication_tested"])
        self.assertEqual([q for q, _ in self.calls], [b.ACCOUNT, b.CHANNELS])

    def test_account_must_match_id_handle_and_service(self):
        for field, value in [("id", "other"), ("name", "other"), ("service", "instagram")]:
            with self.subTest(field=field):
                original = copy.deepcopy(self.channel)
                self.channel[field] = value
                with self.assertRaises(b.ConnectionError):
                    self.check()
                self.channel = original

    def test_disconnected_locked_and_unknown_status_are_rejected(self):
        for field in ["isDisconnected", "isLocked"]:
            for value in [True, None, "false", 0]:
                with self.subTest(field=field, value=value):
                    self.channel[field] = value
                    with self.assertRaises(b.ConnectionError):
                        self.check()
            self.channel[field] = False

    def test_duplicate_matches_are_rejected(self):
        def query(key, query, variables=None):
            return ({"account": {"organizations": [{"id": "a"}, {"id": "b"}]}}
                    if query == b.ACCOUNT else {"channels": [self.channel]})
        with self.assertRaisesRegex(b.ConnectionError, "ambiguous"):
            b.check_connection("fixture", "expected", "amiees.com", query)

    def test_missing_organization_and_bad_channel_list_fail(self):
        for value in [{}, {"account": None}, {"account": {"organizations": []}},
                      {"account": {"organizations": [{"id": ""}]}}]:
            with self.assertRaises(b.ConnectionError):
                b.check_connection("fixture", "expected", "amiees.com", lambda *args: value)

    def test_queries_cannot_be_replaced_by_mutations(self):
        with patch.object(b.urllib.request, "build_opener") as opener:
            with self.assertRaises(b.ConnectionError):
                b.query_buffer("fixture", "mutation { createPost {} }")
            opener.assert_not_called()

    def test_blank_key_fails_without_network(self):
        with patch.object(b.urllib.request, "build_opener") as opener:
            for key in ["", "invalid\nheader", "invalid key"]:
                with self.assertRaises(b.ConnectionError):
                    b.query_buffer(key, b.ACCOUNT)
            opener.assert_not_called()

    def test_request_has_pinned_endpoint_no_redirects_and_bearer_header(self):
        with patch.object(b.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(b'{"data":{"account":{}}}')
            b.query_buffer("fixture-secret", b.ACCOUNT)
            self.assertEqual(opener.call_args.args, (b.NoRedirect,))
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://api.buffer.com")
            self.assertEqual(request.headers["Authorization"], "Bearer fixture-secret")
            self.assertNotIn("fixture-secret", request.data.decode())

    def test_graphql_and_invalid_json_errors_do_not_leak_response(self):
        for raw in [b'{"data":{},"errors":[{"message":"fixture-secret private info"}]}',
                    b'not JSON fixture-secret', b'[]', b'{"data":null}']:
            with self.assertRaises(b.ConnectionError) as error:
                self.response(raw)
            self.assertNotIn("fixture-secret", str(error.exception))

    def test_http_errors_do_not_leak_request_or_body(self):
        with patch.object(b.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(
                "https://api.buffer.com", 401, "fixture-secret", {}, io.BytesIO(b"fixture-secret"))
            with self.assertRaises(b.ConnectionError) as error:
                b.query_buffer("fixture-secret", b.ACCOUNT)
            self.assertNotIn("fixture-secret", str(error.exception))

    def test_oversized_response_fails(self):
        with patch.object(b, "MAX_RESPONSE", 10):
            with self.assertRaisesRegex(b.ConnectionError, "size limit"):
                self.response(b'{' + b' ' * 20 + b'}')


if __name__ == "__main__":
    unittest.main()
