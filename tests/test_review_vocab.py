"""
Reviewing the vocabulary draft, with typed answers.

- Suggestions come most-common first, and --min-notes hides the rest.
- y adds a tag, or adds words to a tag that already exists; n never asks
  again; s asks again next time; r, c, m and w change what gets added.
- vocab.yaml keeps its comments, version and order; the old file is backed
  up; nothing is written if the result wouldn't load.
- Decisions are saved as they are made, readable only by their owner, so q
  and running it again carries on.
"""

import json
import os
import stat
import tempfile
import unittest

import review_vocab
import vocab as vocab_module

VOCAB = """\
# taggle-rock vocabulary. You own this file.
#
# One tag per line, then the words that mean it.

version: 3

body_part:
  joints: [knuckles, ankles]

symptom:
  fatigue: [tired]
"""

DRAFT = """\
# Vocabulary draft from qwen-local, 2026-09-15 14:22: 1349 of 1349 notes read.
#
# Nothing here is used until you copy it into vocab.yaml.

version: draft

body_part:
  # new tags
  "feet": ["my feet", "soles"]  # 9 notes

symptom:
  # new tags
  "night sweats": ["soaked through", "woke up drenched"]  # 17 notes
  "itching": ["itchy", "scratching"]  # 3 notes
  # more words for tags you already have
  "fatigue": ["wiped", "running on empty"]  # 21 notes
"""


def typed(*answers):
    it = iter(answers)

    def ask(prompt, prefill=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return ask


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vocab_path = os.path.join(self.tmp.name, "vocab.yaml")
        self.draft_path = os.path.join(self.tmp.name, "drafts", "vocab_draft.yaml")
        self.decisions_path = os.path.join(self.tmp.name, "drafts", "decisions.jsonl")
        os.makedirs(os.path.dirname(self.draft_path))
        self.write(self.vocab_path, VOCAB)
        self.write(self.draft_path, DRAFT)
        self.lines = []

    def write(self, path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def review(self, *answers, min_notes=2):
        return review_vocab.review(self.draft_path, self.vocab_path, self.decisions_path,
                                   min_notes=min_notes, ask=typed(*answers), out=self.lines.append)

    def vocab(self):
        return vocab_module.load(self.vocab_path)

    def test_most_common_first_and_min_notes(self):
        order = [s["tag"] for s in review_vocab.parse_draft(DRAFT)]
        self.assertEqual(order, ["fatigue", "night sweats", "feet", "itching"])
        self.assertEqual(review_vocab.parse_draft(DRAFT)[0]["words"], ["wiped", "running on empty"])
        self.review("n", "n", "n", min_notes=5)   # itching (3 notes) is not offered
        self.assertEqual([d["suggested"] for d in review_vocab.read_decisions(self.decisions_path).values()],
                         ["fatigue", "night sweats", "feet"])

    def test_adding_a_new_tag_and_words_for_one_you_have(self):
        self.review("y", "y", "n", "n")           # fatigue words, night sweats, no feet, no itching
        vocab = self.vocab()
        self.assertEqual(vocab.words_for["fatigue"], ("tired", "wiped", "running on empty"))
        self.assertEqual(vocab.category_of["night sweats"], "symptom")
        self.assertNotIn("feet", vocab.tags)
        text = self.read(self.vocab_path)
        self.assertTrue(text.startswith("# taggle-rock vocabulary. You own this file."), "comments kept")
        self.assertIn("version: 3", text)
        self.assertEqual(self.read(self.vocab_path + ".bak"), VOCAB)

    def test_a_rejected_suggestion_never_comes_back_but_a_skipped_one_does(self):
        self.review("n", "s", "n", "n")
        self.lines.clear()
        self.review("y", "n", min_notes=17)       # only fatigue and night sweats are 17+
        self.assertIn("1 to look at", self.lines[0])
        self.assertIn("night sweats", self.vocab().tags)

    def test_rename_category_merge_and_word_editing(self):
        self.review(
            "w", "wiped out", "y",                # fatigue: replace the words, then add
            "r", "sweats", "c", "3", "y",         # night sweats -> sweats, severity
            "m", "joints",                        # feet merged into joints: m decides on its own
            "n")                                  # itching
        vocab = self.vocab()
        self.assertEqual(vocab.words_for["fatigue"], ("tired", "wiped out"))
        self.assertEqual(vocab.category_of["sweats"], "severity")
        self.assertNotIn("night sweats", vocab.tags)
        self.assertEqual(vocab.words_for["joints"], ("knuckles", "ankles", "my feet", "soles", "feet"))

    def test_a_bad_answer_is_asked_again(self):
        # A name the tracker would refuse, then a good one; an unknown key reprints the help.
        self.review("r", "NOT A TAG!", "?", "r", "sweats", "y", "n", "n", "n")
        self.assertIn("sweats", self.vocab().tags)
        self.assertTrue(any("1-48 long" in line for line in self.lines))
        self.assertGreater(sum(line == review_vocab.HELP for line in self.lines), 4)

    def test_q_saves_what_was_decided_and_carries_on_next_time(self):
        self.review("y", "q")
        self.assertEqual(self.vocab().words_for["fatigue"], ("tired", "wiped", "running on empty"))
        self.assertIn("stopped: run it again to carry on", self.lines)
        self.lines.clear()
        self.review("y", "n", "n")
        self.assertIn("night sweats", self.vocab().tags)
        self.assertIn("1 added, 0 rejected so far", self.lines[0])

    def test_nothing_is_written_if_the_result_would_not_load(self):
        # A tag the tracker would refuse, as a decision file from an older version
        # might hold: the write is refused rather than leaving vocab.yaml broken.
        decided = {"feet": {"suggested": "feet", "action": "add", "tag": "NOT A TAG!",
                            "category": "body_part", "words": ["soles"]}}
        result = review_vocab.apply_decisions(self.vocab_path, decided, out=self.lines.append)
        self.assertIsNone(result)
        self.assertEqual(self.read(self.vocab_path), VOCAB)
        self.assertFalse(os.path.exists(self.vocab_path + ".bak"))
        self.assertTrue(any("NOT changed" in line for line in self.lines))
        self.assertTrue(any("decisions.jsonl" in line for line in self.lines), "says they're kept")

    def test_decisions_are_readable_only_by_their_owner(self):
        self.review("y", "q")
        self.assertEqual(stat.S_IMODE(os.stat(self.decisions_path).st_mode), 0o600)
        entry = json.loads(self.read(self.decisions_path).splitlines()[0])
        self.assertEqual(entry["suggested"], "fatigue")

    def test_summary_is_counts_only(self):
        self.review("y", "n", "s", "n")
        text = review_vocab.summarize(self.draft_path, self.decisions_path)
        self.assertIn("draft: 4 suggestions, 4 at 2+ notes", text)
        self.assertIn("decided: 1 added, 2 rejected, 1 skipped", text)
        self.assertIn("still to look at: 1", text)
        self.assertIn("words those additions carry: 2", text)
        for private in ("fatigue", "night sweats", "wiped", "soaked through"):
            self.assertNotIn(private, text)

    def test_words_with_commas_or_quotes_are_written_safely(self):
        self.write(self.draft_path, DRAFT + '  "brain fog": ["cant think, at all"]  # 4 notes\n')
        # Order is by notes: fatigue 21, night sweats 17, feet 9, brain fog 4, itching 3.
        self.review("n", "n", "n", "y", "n")
        self.assertEqual(self.vocab().words_for["brain fog"], ("cant think, at all",))


if __name__ == "__main__":
    unittest.main()
