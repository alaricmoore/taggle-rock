"""
Vocabulary drafting, with made-up notes and a faked Qwen.

- Qwen's suggestions are checked by rules: a phrase not really in the note,
  words already in the vocabulary, and badly spelled tags are thrown out.
- Suggestions sort into new tags and new words for existing tags, counted by
  notes; --min-notes hides rare ones; a new tag's category is the majority.
- The draft is valid YAML shaped like vocab.yaml, and its new tags would pass
  the tracker's spelling rule.
- Answers are cached per note and vocabulary version; Ctrl-C still writes a
  draft; a failed note is skipped.
- Nothing from a note is printed, and drafts/ is readable only by its owner.
- qwen.ask_candidates asks with thinking off and a schema, and drops malformed items.
"""

import io
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import yaml

import draft_vocab
import qwen
import vocab

VOCAB = vocab.parse("""
version: 1
body_part:
  lymph nodes: [glands]
symptom:
  fatigue: [tired]
""")


def note(i, text):
    return {"date": f"2026-01-{i:02d}", "field": "notes", "text": text,
            "sha256": f"{i:064x}", "tagged_sha256": None}


NOTES = [
    note(1, "Made-up: night sweats again, bone tired, knees stiff SECRETWORD"),
    note(2, "Made-up: Night  sweats, bone tired SECRETWORD"),
    note(3, "Made-up: glands sore, knees stiff SECRETWORD"),
    note(4, "Made-up: quiet day SECRETWORD"),
]

ANSWERS = {
    1: [{"phrase": "night sweats", "tag": "night sweats", "category": "symptom"},
        {"phrase": "bone tired", "tag": "fatigue", "category": "symptom"},
        {"phrase": "knees stiff", "tag": "stiffness", "category": "symptom"},
        {"phrase": "SECRETWORD", "tag": "Bad Tag!", "category": "context"}],
    2: [{"phrase": "night sweats", "tag": "night sweats", "category": "context"},
        {"phrase": "bone tired", "tag": "fatigue", "category": "symptom"},
        {"phrase": "fever of 104", "tag": "fever", "category": "symptom"}],     # not in the note
    3: [{"phrase": "glands", "tag": "lymph nodes", "category": "body_part"},    # already known
        {"phrase": "knees stiff", "tag": "stiffness", "category": "symptom"}],
    4: [],
}


class FakeTracker:
    def __init__(self, notes=NOTES):
        self._notes = notes

    def notes(self, since=None):
        return self._notes


def fake_ask(vocab_, field, text):
    number = next(n for n in NOTES if n["text"] == text)["date"][-2:]
    return [dict(c) for c in ANSWERS[int(number)]]


class DraftTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "drafts")
        self.lines = []

    def run_draft(self, ask=fake_ask, **kwargs):
        return draft_vocab.draft(FakeTracker(), VOCAB, ask=ask, directory=self.dir,
                                 out=self.lines.append, now=datetime(2026, 9, 14, 21, 0), **kwargs)

    def draft_text(self):
        with open(os.path.join(self.dir, "vocab_draft.yaml"), encoding="utf-8") as f:
            return f.read()


class TestTally(DraftTest):
    def test_suggestions_are_checked_and_sorted(self):
        summary = self.run_draft(min_notes=1)
        data = yaml.safe_load(self.draft_text())
        self.assertEqual(data["version"], "draft")
        self.assertEqual(data["symptom"], {
            "night sweats": [],               # the phrase is the tag itself
            "stiffness": ["knees stiff"],
            "fatigue": ["bone tired"],        # new words for an existing tag
        })
        self.assertNotIn("fever", self.draft_text())
        self.assertEqual(summary["dropped"], {
            "tag not spelled the way the tracker needs": 1,
            "phrase not actually in the note": 1,
            "already in the vocabulary": 1,
        })

    def test_a_new_tag_that_is_already_a_known_word_goes_under_that_tag(self):
        # Seen with real Qwen on a made-up note: "pred 10mg" suggested as a new
        # tag "prednisone", when prednisone is already a word for steroids.
        v = vocab.parse("version: 1\ncontext:\n  steroids: [prednisone, pred]\n")
        new, more, _ = draft_vocab.tally(v, [(
            "made-up: took pred 10mg",
            [{"phrase": "pred 10mg", "tag": "Prednisone", "category": "context"}])])
        self.assertEqual(new, {})
        self.assertEqual(dict(more["steroids"]["phrases"]), {"pred 10mg": 1})

    def test_a_new_tags_category_is_the_majority(self):
        # "night sweats" came as symptom once and context once. A tie goes to
        # whichever comes first in vocab.CATEGORIES, and symptom is before context.
        self.run_draft(min_notes=1)
        self.assertNotIn("context", yaml.safe_load(self.draft_text()))

    def test_min_notes_hides_rare_suggestions(self):
        self.run_draft(min_notes=2)
        data = yaml.safe_load(self.draft_text())
        self.assertEqual(set(data["symptom"]), {"night sweats", "stiffness", "fatigue"})
        self.run_draft(min_notes=3)
        self.assertNotIn("symptom", yaml.safe_load(self.draft_text()))

    def test_the_draft_lists_counts_and_new_tags_pass_the_trackers_spelling(self):
        self.run_draft(min_notes=1)
        text = self.draft_text()
        self.assertIn('"night sweats": []  # 2 notes', text)
        self.assertIn("# new tags", text)
        self.assertIn("# more words for tags you already have", text)
        for category in ("body_part", "symptom", "severity", "context"):
            for tag in (yaml.safe_load(text).get(category) or {}):
                self.assertRegex(tag, vocab.TAG_RE)

    def test_the_most_common_come_first(self):
        self.run_draft(min_notes=1)
        text = self.draft_text()
        self.assertLess(text.index('"night sweats"'), text.index('"fatigue"'))


class TestRunning(DraftTest):
    def test_answers_are_cached_per_note_and_vocabulary(self):
        self.run_draft()
        never = mock.Mock(side_effect=AssertionError("asked again"))
        summary = self.run_draft(ask=never)
        self.assertEqual((summary["asked"], summary["from_cache"]), (0, 4))

        changed = vocab.parse("version: 2\nsymptom:\n  fatigue: [tired]\n")
        calls = []
        draft_vocab.draft(FakeTracker(), changed, ask=lambda v, f, t: calls.append(t) or [],
                          directory=self.dir, out=self.lines.append)
        self.assertEqual(len(calls), 4)

    def test_ctrl_c_still_writes_a_draft(self):
        def ask(v, field, text):
            if text == NOTES[2]["text"]:
                raise KeyboardInterrupt
            return fake_ask(v, field, text)
        summary = self.run_draft(ask=ask, min_notes=1)
        self.assertTrue(summary["stopped"])
        self.assertIn("2 of 4 notes read", self.draft_text())
        self.assertIn("Stopped early.", self.draft_text())
        self.assertEqual(self.run_draft(min_notes=1)["from_cache"], 2)

    def test_a_failed_note_is_skipped(self):
        def ask(v, field, text):
            if text == NOTES[0]["text"]:
                raise qwen.QwenError("answer is not JSON")
            return fake_ask(v, field, text)
        summary = self.run_draft(ask=ask)
        self.assertEqual(summary["qwen_failed"], 1)
        self.assertEqual(summary["asked"], 3)

    def test_limit(self):
        self.assertEqual(self.run_draft(limit=2)["notes"], 2)


class TestPrivacy(DraftTest):
    def test_nothing_from_a_note_is_printed(self):
        self.run_draft(min_notes=1)
        printed = "\n".join(self.lines)
        for word in ("SECRETWORD", "sweats", "knees", "bone tired"):
            self.assertNotIn(word.lower(), printed.lower())

    def test_drafts_are_readable_only_by_their_owner(self):
        self.run_draft()
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        for name in ("vocab_draft.yaml", "cache.jsonl"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, name)).st_mode), 0o600, name)


class TestAskCandidates(unittest.TestCase):
    def test_the_request_and_dropping_malformed_items(self):
        content = json.dumps({"candidates": [
            {"phrase": "night sweats", "tag": "night sweats", "category": "symptom"},
            {"phrase": "x", "tag": "y", "category": "diagnosis"},
            {"phrase": 3, "tag": "y", "category": "symptom"},
            "not an object",
        ]})
        reply = io.BytesIO(json.dumps({"message": {"content": content}}).encode())
        with mock.patch("qwen.urllib.request.urlopen", return_value=reply) as urlopen:
            got = qwen.ask_candidates(VOCAB, "derm_notes", "made-up note")
        self.assertEqual(got, [{"phrase": "night sweats", "tag": "night sweats", "category": "symptom"}])
        body = json.loads(urlopen.call_args.args[0].data)
        self.assertIs(body["think"], False)
        self.assertEqual(body["format"], qwen.candidates_schema())
        self.assertIn("lymph nodes [body_part]: glands", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1]["content"], "Box: derm\nNote:\nmade-up note")

    def test_a_malformed_answer_is_a_qwen_error(self):
        for content in ["not json", '{"tags": []}', '["a"]']:
            reply = io.BytesIO(json.dumps({"message": {"content": content}}).encode())
            with self.subTest(content=content), \
                    mock.patch("qwen.urllib.request.urlopen", return_value=reply), \
                    self.assertRaises(qwen.QwenError):
                qwen.ask_candidates(VOCAB, "notes", "made-up note")


if __name__ == "__main__":
    unittest.main()
