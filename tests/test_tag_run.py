"""
A tag run, with a fake tracker and a fake Qwen.

- Only notes never tagged, or edited since, are asked about; --limit caps it.
- Tags go to the tracker in batches of 20 under one run id, with model and
  vocabulary version, and the tracker's per-note results are counted.
- A note Qwen can't answer is skipped and counted, not sent.
- A dry run sends nothing and writes no log.
- Ctrl-C keeps what was done. A failed send is not retried on the way out.
- Note text never appears in the output or the log, even with --show; the
  log is readable only by its owner.
"""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import qwen
import tag_run
import vocab
from tracker import TrackerError

VOCAB = vocab.parse("""
version: 1
body_part:
  lymph nodes: [glands]
symptom:
  fatigue: [tired]
""")


def make_note(i, tagged=False, edited=False, text=None):
    text = text or f"made-up note number {i} SECRET-TEXT"
    sha = f"{i:064x}"
    return {"date": f"2026-01-{i % 28 + 1:02d}", "field": "notes", "text": text, "sha256": sha,
            "tagged_sha256": (sha if tagged and not edited else ("0" * 64 if edited else None))}


class FakeTracker:
    def __init__(self, notes, results=None, fail_post=False):
        self._notes = notes
        self.posts = []
        self.results = results or {}
        self.fail_post = fail_post

    def notes(self, since=None):
        self.since = since
        return self._notes

    def post_tags(self, run_id, model, vocab_version, notes):
        self.posts.append({"run_id": run_id, "model": model, "vocab_version": vocab_version,
                           "notes": [dict(n) for n in notes]})
        if self.fail_post:
            raise TrackerError("POST /api/tags: HTTP 502")
        return {"ok": True, "results": [{"date": n["date"], "field": n["field"],
                                         "result": self.results.get(n["note_sha256"], "tagged")}
                                        for n in notes]}


def fake_ask(vocab_, field, text):
    return [("fatigue", "symptom")] if "3" in text else [("lymph nodes", "body_part"), ("fatigue", "symptom")]


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = os.path.join(self.tmp.name, "runs")
        self.lines = []

    def run_tags(self, tracker, **kwargs):
        kwargs.setdefault("ask", fake_ask)
        return tag_run.run(tracker, VOCAB, log_dir=self.log_dir, out=self.lines.append,
                           run_id="run-test", **kwargs)

    def log_entries(self):
        with open(os.path.join(self.log_dir, "run-test.jsonl")) as f:
            return [json.loads(line) for line in f]


class TestWhatGetsAsked(RunTest):
    def test_only_new_and_edited_notes_are_asked_about(self):
        notes = [make_note(1), make_note(2, tagged=True), make_note(3, tagged=True, edited=True)]
        asked = []
        tracker = FakeTracker(notes)
        self.run_tags(tracker, ask=lambda v, f, t: asked.append(t) or [])
        self.assertEqual(asked, [notes[0]["text"], notes[2]["text"]])

    def test_retag_asks_about_tagged_notes_too(self):
        notes = [make_note(1), make_note(2, tagged=True), make_note(3, tagged=True, edited=True)]
        asked = []
        self.run_tags(FakeTracker(notes), ask=lambda v, f, t: asked.append(t) or [], retag=True)
        self.assertEqual(asked, [n["text"] for n in notes])

    def test_retag_carries_on_after_notes_done_with_this_vocabulary(self):
        notes = [make_note(i, tagged=True) for i in range(1, 7)]
        os.makedirs(self.log_dir)
        earlier = [
            {"date": notes[0]["date"], "field": "notes", "sha256": notes[0]["sha256"],
             "vocab": VOCAB.version, "result": "tagged"},                       # done: skipped
            {"date": notes[1]["date"], "field": "notes", "sha256": notes[1]["sha256"],
             "vocab": "v0-older", "result": "tagged"},                          # older vocabulary
            {"date": notes[2]["date"], "field": "notes", "sha256": "f" * 64,
             "vocab": VOCAB.version, "result": "tagged"},                       # edited since
            {"date": notes[3]["date"], "field": "notes", "sha256": notes[3]["sha256"],
             "vocab": VOCAB.version, "result": "stale"},                        # never stored
            {"date": notes[4]["date"], "field": "notes", "result": "tagged"},  # old log format
        ]
        with open(os.path.join(self.log_dir, "run-earlier.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(e) for e in earlier) + '\n{"cut off')
        with open(os.path.join(self.log_dir, "run-earlier.grades.jsonl"), "w") as f:
            f.write(json.dumps({"date": notes[5]["date"], "field": "notes", "tags": {}}) + "\n")
        asked = []
        self.run_tags(FakeTracker(notes), ask=lambda v, f, t: asked.append(t) or [], retag=True)
        self.assertEqual(asked, [n["text"] for n in notes[1:]])
        self.assertIn("5 note(s) to tag", self.lines[0])
        self.assertIn("retag: 1 already done with this vocabulary", self.lines[0])

    def test_the_log_records_hash_vocabulary_model_and_prompt(self):
        note = make_note(1)
        self.run_tags(FakeTracker([note]))
        entry = self.log_entries()[0]
        self.assertEqual((entry["sha256"], entry["vocab"]), (note["sha256"], VOCAB.version))
        self.assertEqual((entry["model"], entry["prompt"]),
                         (qwen.MODEL, qwen.prompt_id(VOCAB)))
        self.assertEqual(tag_run.tagged_with(self.log_dir, VOCAB.version),
                         {(note["date"], "notes", note["sha256"])})

    def test_notes_from_asks_about_those_notes_even_though_they_are_tagged(self):
        # The point of --notes-from is measuring on notes you have judged, and
        # those have been tagged by definition.
        notes = [make_note(i, tagged=True) for i in range(1, 4)]
        os.makedirs(self.log_dir)
        with open(os.path.join(self.log_dir, "run-old.grades.jsonl"), "w") as f:
            f.write(json.dumps({"date": notes[1]["date"], "field": "notes",
                                "tags": {}, "missing": []}) + "\n")
        asked = []
        self.run_tags(FakeTracker(notes), ask=lambda v, f, t: asked.append(t) or [],
                      notes_from="run-old")
        self.assertEqual(asked, [notes[1]["text"]])

    def test_notes_from_limits_the_run_to_the_notes_graded_for_another_run(self):
        notes = [make_note(i) for i in range(1, 6)]
        os.makedirs(self.log_dir)
        with open(os.path.join(self.log_dir, "run-old.grades.jsonl"), "w") as f:
            for note in (notes[1], notes[3]):
                f.write(json.dumps({"date": note["date"], "field": "notes",
                                    "tags": {}, "missing": []}) + "\n")
        asked = []
        summary = self.run_tags(FakeTracker(notes), ask=lambda v, f, t: asked.append(t) or [],
                                notes_from="run-old")
        self.assertEqual(asked, [notes[1]["text"], notes[3]["text"]])
        self.assertEqual(summary["notes"], 2)

    def test_a_trial_writes_the_log_but_sends_nothing(self):
        tracker = FakeTracker([make_note(i) for i in range(1, 4)])
        summary = self.run_tags(tracker, trial=True)
        self.assertEqual(tracker.posts, [])
        self.assertEqual((summary["trial"], summary["tagged"]), (3, 0))
        entries = self.log_entries()
        self.assertEqual([e["result"] for e in entries], ["trial"] * 3)
        self.assertEqual(entries[0]["tags"], ["lymph nodes", "fatigue"])
        self.assertTrue(any("nothing was sent" in line for line in self.lines))

    def test_another_model_is_asked_and_written_down(self):
        asked = []

        def ask(vocab_, field, text, model=None):
            asked.append(model)
            return [("fatigue", "symptom")]

        tracker = FakeTracker([make_note(1)])
        self.run_tags(tracker, ask=ask, model="qwen-instruct")
        self.assertEqual(asked, ["qwen-instruct"])
        self.assertEqual(self.log_entries()[0]["model"], "qwen-instruct")
        self.assertEqual(tracker.posts[0]["model"], "qwen-instruct")
        self.assertIn("model qwen-instruct", self.lines[0])

    def test_limit_and_since(self):
        tracker = FakeTracker([make_note(i) for i in range(1, 11)])
        summary = self.run_tags(tracker, limit=4, since="2026-01-01")
        self.assertEqual(summary["notes"], 4)
        self.assertEqual(tracker.since, "2026-01-01")


    def test_a_skipped_box_is_never_asked_about(self):
        keep = make_note(2)
        keep["field"] = "cycle_notes"
        asked = []
        self.run_tags(FakeTracker([make_note(1), keep, make_note(3)]),
                      ask=lambda v, f, t: asked.append(f) or [], skip_fields=("notes",))
        self.assertEqual(asked, ["cycle_notes"])
        self.assertIn("2 note(s) in notes skipped", self.lines[0])


class ClearTest(RunTest):
    def test_clear_empties_the_tags_of_those_boxes_and_logs_it(self):
        keep = make_note(2)
        keep["field"] = "cycle_notes"
        tracker = FakeTracker([make_note(1), keep, make_note(3)])
        summary = tag_run.clear(tracker, VOCAB, ("notes",), log_dir=self.log_dir,
                                out=self.lines.append, run_id="run-test")
        sent = tracker.posts[0]["notes"]
        self.assertEqual([n["field"] for n in sent], ["notes", "notes"])
        self.assertTrue(all(n["tags"] == [] for n in sent))
        self.assertEqual((summary["notes"], summary["cleared"]), (2, 2))
        entries = self.log_entries()
        self.assertTrue(all(e["cleared"] and e["tags"] == [] for e in entries))


class TestSending(RunTest):
    def test_batches_of_twenty_under_one_run(self):
        tracker = FakeTracker([make_note(i) for i in range(1, 46)])
        summary = self.run_tags(tracker)
        self.assertEqual([len(p["notes"]) for p in tracker.posts], [20, 20, 5])
        self.assertEqual({(p["run_id"], p["model"], p["vocab_version"]) for p in tracker.posts},
                         {("run-test", qwen.MODEL, VOCAB.version)})
        first = tracker.posts[0]["notes"][0]
        self.assertEqual(first["note_sha256"], make_note(1)["sha256"])
        self.assertEqual(first["tags"], [{"tag": "lymph nodes", "category": "body_part"},
                                         {"tag": "fatigue", "category": "symptom"}])
        self.assertNotIn("text", first)
        self.assertEqual(summary["tagged"], 45)
        self.assertEqual(summary["answered"], 45)

    def test_the_trackers_results_are_counted_and_logged(self):
        notes = [make_note(1), make_note(2), make_note(4)]
        tracker = FakeTracker(notes, results={notes[1]["sha256"]: "stale", notes[2]["sha256"]: "missing"})
        summary = self.run_tags(tracker)
        self.assertEqual((summary["tagged"], summary["stale"], summary["missing"]), (1, 1, 1))
        self.assertEqual([e["result"] for e in self.log_entries()], ["tagged", "stale", "missing"])

    def test_a_note_qwen_cant_answer_is_skipped(self):
        def ask(v, f, text):
            if "2" in text:
                raise qwen.QwenError("answer is not JSON")
            return []
        tracker = FakeTracker([make_note(1), make_note(2), make_note(4)])
        summary = self.run_tags(tracker, ask=ask)
        self.assertEqual(summary["qwen_failed"], 1)
        self.assertEqual(len(tracker.posts[0]["notes"]), 2)
        self.assertIn("qwen_failed", [e["result"] for e in self.log_entries()])

    def test_a_dry_run_sends_nothing_and_writes_no_log(self):
        tracker = FakeTracker([make_note(i) for i in range(1, 6)])
        summary = self.run_tags(tracker, dry_run=True, show=True)
        self.assertEqual(tracker.posts, [])
        self.assertFalse(os.path.exists(self.log_dir))
        self.assertEqual(summary["answered"], 5)
        self.assertTrue(any("lymph nodes, fatigue" in line for line in self.lines))

    def test_ctrl_c_keeps_what_was_done(self):
        def ask(v, f, text):
            if "number 3 " in text:
                raise KeyboardInterrupt
            return [("fatigue", "symptom")]
        tracker = FakeTracker([make_note(i) for i in range(1, 6)])
        summary = self.run_tags(tracker, ask=ask)
        self.assertEqual(len(tracker.posts), 1)
        self.assertEqual(len(tracker.posts[0]["notes"]), 2)
        self.assertEqual(summary["tagged"], 2)

    def test_a_failed_send_is_not_retried_on_the_way_out(self):
        tracker = FakeTracker([make_note(i) for i in range(1, 4)], fail_post=True)
        with self.assertRaises(TrackerError):
            self.run_tags(tracker)
        self.assertEqual(len(tracker.posts), 1)


class TestPrivacy(RunTest):
    def test_note_text_is_never_printed_or_logged(self):
        tracker = FakeTracker([make_note(i) for i in range(1, 4)])
        self.run_tags(tracker, show=True)
        self.assertTrue(self.lines)
        self.assertFalse(any("SECRET-TEXT" in line for line in self.lines))
        with open(os.path.join(self.log_dir, "run-test.jsonl")) as f:
            self.assertNotIn("SECRET-TEXT", f.read())

    def test_the_log_is_readable_only_by_its_owner(self):
        self.run_tags(FakeTracker([make_note(1)]))
        self.assertEqual(stat.S_IMODE(os.stat(self.log_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.log_dir, "run-test.jsonl")).st_mode), 0o600)


class TestMain(unittest.TestCase):
    def test_undo(self):
        fake = mock.Mock()
        fake.undo.return_value = 7
        with mock.patch("tag_run.Tracker.from_config", return_value=fake), \
                redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(tag_run.main(["undo", "run-20260914-210000"]), 0)
        fake.undo.assert_called_once_with("run-20260914-210000")
        self.assertIn("removed tags from 7 note(s)", printed.getvalue())

    def test_clear_refuses_without_yes(self):
        fake = mock.Mock()
        fake.skip_fields = ("notes",)
        with mock.patch("tag_run.Tracker.from_config", return_value=fake), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err, \
                self.assertRaises(SystemExit):
            tag_run.main(["clear", "--vocab", "vocab.example.yaml"])
        self.assertIn("--yes", err.getvalue())
        fake.post_tags.assert_not_called()

    def test_a_tracker_error_is_a_clean_exit_1(self):
        with mock.patch("tag_run.Tracker.from_config", side_effect=TrackerError("config.json is missing ['secret']")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(tag_run.main([]), 1)
        self.assertIn("missing", err.getvalue())


if __name__ == "__main__":
    unittest.main()
