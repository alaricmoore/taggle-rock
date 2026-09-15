"""
The signed connection to the tracker, with the network faked.

- The copied api_signing.py still produces private-track's known answers.
- Every request carries a signature that verifies over exactly the method,
  path, query and body sent, plus the User-Agent Cloudflare needs.
- HTTP errors, an unreachable server and a bad config are TrackerErrors.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import api_signing
from tracker import Tracker, TrackerError

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def reply(obj):
    return io.BytesIO(json.dumps(obj).encode())


def signature_verifies(req, client_id="qwen", secret=SECRET):
    url = req.full_url.split("://", 1)[1]
    path_and_query = url[url.index("/"):]
    path, _, query = path_and_query.partition("?")
    h = {k.lower(): v for k, v in req.header_items()}
    expected = api_signing.sign(secret, req.get_method(), path, query, client_id,
                                h["x-timestamp"], h["x-nonce"], req.data or b"")
    return expected == h["x-signature"]


class TestSigningCopy(unittest.TestCase):
    def test_known_answer_from_private_track(self):
        # tests/test_api_auth.py in private-track: the same inputs, the same signature.
        self.assertEqual(
            api_signing.sign(SECRET, "GET", "/api/clinicians", "user_id=1&a=%7e", "qwen",
                             "1789430400", "abcdefghijklmnop", b""),
            "91f1e65c5db6e943755aeeaa295f14aae0a1af044cbec99fa164eed618b8e499")


class TestRequests(unittest.TestCase):
    def setUp(self):
        self.tracker = Tracker("https://tracker.example/", "qwen", SECRET, 1)

    def test_notes_is_a_signed_get(self):
        with mock.patch("tracker.urllib.request.urlopen",
                        return_value=reply({"ok": True, "notes": [{"date": "2026-01-05"}]})) as urlopen:
            notes = self.tracker.notes(since="2026-01-01")
        self.assertEqual(notes, [{"date": "2026-01-05"}])
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.full_url, "https://tracker.example/api/notes?user_id=1&since=2026-01-01")
        self.assertIsNone(req.data)
        self.assertEqual(req.get_header("User-agent"), "taggle-rock/0.1")
        self.assertTrue(signature_verifies(req))

    def test_post_tags_signs_the_exact_body_sent(self):
        with mock.patch("tracker.urllib.request.urlopen",
                        return_value=reply({"ok": True, "counts": {}, "results": []})) as urlopen:
            self.tracker.post_tags("run-1", "qwen-local", "v1-abc", [{"date": "2026-01-05"}])
        req = urlopen.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"user_id": 1, "run_id": "run-1", "model": "qwen-local",
                                                "vocab_version": "v1-abc", "notes": [{"date": "2026-01-05"}]})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertTrue(signature_verifies(req))

    def test_undo(self):
        with mock.patch("tracker.urllib.request.urlopen",
                        return_value=reply({"ok": True, "run_id": "run-1", "notes_removed": 4})) as urlopen:
            self.assertEqual(self.tracker.undo("run-1"), 4)
        self.assertTrue(signature_verifies(urlopen.call_args.args[0]))

    def test_a_signature_with_the_wrong_secret_does_not_verify(self):
        # A control, so signature_verifies isn't passing vacuously.
        with mock.patch("tracker.urllib.request.urlopen", return_value=reply({"notes": []})) as urlopen:
            self.tracker.notes()
        self.assertFalse(signature_verifies(urlopen.call_args.args[0], secret="f" * 64))

    def test_trouble_is_a_tracker_error(self):
        failures = [
            urllib.error.HTTPError("https://tracker.example/api/notes", 403, "Forbidden", {},
                                   io.BytesIO(b'{"error":"forbidden"}')),
            urllib.error.URLError("name resolution failed"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("tracker.urllib.request.urlopen", side_effect=failure), \
                    self.assertRaises(TrackerError) as caught:
                self.tracker.notes()
            self.assertNotIn(SECRET, str(caught.exception))


class TestConfig(unittest.TestCase):
    def write(self, obj):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj))
        self.addCleanup(os.remove, path)
        return path

    def test_reads_config(self):
        t = Tracker.from_config(self.write({"server": "https://x.example", "client_id": "qwen",
                                            "secret": SECRET, "user_id": 1}))
        self.assertEqual((t.server, t.client_id, t.user_id), ("https://x.example", "qwen", 1))

    def test_missing_keys_and_bad_json_are_tracker_errors(self):
        for content in [{"server": "https://x.example", "client_id": "qwen"}, "{not json,}"]:
            with self.subTest(content=content), self.assertRaises(TrackerError):
                Tracker.from_config(self.write(content))


if __name__ == "__main__":
    unittest.main()
