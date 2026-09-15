"""
Grading a tag run by hand, with a fake tracker and typed answers.

- The sample is only tagged notes, in the same order every time for a run,
  and a bigger sample extends it.
- Marks and missing words are saved per note; the note text is not, and the
  grades file is readable only by its owner.
- A rerun carries on after the notes already graded; q stops without saving
  the half-graded note; a wrong answer is asked again.
- A note edited or deleted since the run is skipped and the next one taken.
- The summary has counts only: no note text, tag names or typed words.
"""

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import spot_check
import vocab

VOCAB = vocab.parse("""
version: 1
body_part:
  joints: [ankles, knees]
symptom:
  pain: [killing me]
  fatigue: [tired]
""")


def make_note(i, tags, edited=False):
    sha = f"{i:064x}"
    return {"date": f"2026-01-{i:02d}", "field": "notes", "text": f"made-up note {i} SECRET-TEXT",
            "sha256": sha, "tagged_sha256": "0" * 64 if edited else sha, "tags": tags}


class FakeTracker:
    def __init__(self, notes):
        self._notes = notes

    def notes(self, since=None):
        return [{k: v for k, v in n.items() if k != "tags"} for n in self._notes]


def typed(*answers):
    it = iter(answers)

    def ask(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return ask


class SpotCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.notes = [make_note(1, ["joints", "pain"]), make_note(2, []), make_note(3, ["fatigue"])]
        self.write_log("run-test", self.notes + [
            {"date": "2026-01-09", "field": "notes", "result": "qwen_failed", "error": "x"}])
        self.lines = []

    def write_log(self, run_id, rows):
        with open(os.path.join(self.dir, f"{run_id}.jsonl"), "w") as f:
            for r in rows:
                entry = {k: r[k] for k in ("date", "field", "result", "error") if k in r}
                if "tags" in r:
                    entry.update(tags=r["tags"], result="tagged", error=None)
                f.write(json.dumps(entry) + "\n")

    def grade(self, answers, size=3, notes=None):
        return spot_check.grade(FakeTracker(notes or self.notes), VOCAB, "run-test", size=size,
                                log_dir=self.dir, ask_input=typed(*answers), out=self.lines.append)

    def order(self):
        rows = spot_check.read_jsonl(os.path.join(self.dir, "run-test.jsonl"))
        return [r["date"] for r in spot_check.sampled(rows, "run-test")]

    def answers_for_all(self):
        """y for every tag, nothing missing, in sample order."""
        by_date = {n["date"]: n for n in self.notes}
        answers = []
        for date in self.order():
            answers += ["y"] * len(by_date[date]["tags"]) + [""]
        return answers

    def test_sample_is_tagged_notes_in_a_fixed_order(self):
        self.assertEqual(sorted(self.order()), ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(self.order(), self.order())

    def test_marks_and_missing_are_saved_without_note_text(self):
        first = {n["date"]: n for n in self.notes}[self.order()[0]]
        answers = ["n"] * len(first["tags"]) + ["Ankles, swelling"]
        grades = self.grade(answers, size=1)
        self.assertEqual(grades, [{"date": first["date"], "field": "notes",
                                   "tags": {t: False for t in first["tags"]},
                                   "missing": ["ankles", "swelling"]}])
        path = os.path.join(self.dir, "run-test.grades.jsonl")
        with open(path) as f:
            self.assertNotIn("SECRET-TEXT", f.read())
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertTrue(any("SECRET-TEXT" in line for line in self.lines), "you see the note while grading")

    def test_rerun_carries_on_and_bigger_sample_extends(self):
        by_date = {n["date"]: n for n in self.notes}
        first, second = self.order()[:2]
        self.grade(["y"] * len(by_date[first]["tags"]) + [""], size=1)
        grades = self.grade(["y"] * len(by_date[second]["tags"]) + [""], size=2)
        self.assertEqual([g["date"] for g in grades], [first, second])

    def test_q_stops_without_saving_the_half_graded_note(self):
        notes = [make_note(1, ["joints", "pain"]), make_note(3, ["fatigue"])]
        self.notes = notes
        self.write_log("run-test", notes)
        by_date = {n["date"]: n for n in notes}
        first, second = self.order()
        answers = ["y"] * len(by_date[first]["tags"]) + [""] + ["q"]
        grades = self.grade(answers, size=2)
        self.assertEqual([g["date"] for g in grades], [first])
        self.assertIn("stopped: run it again to carry on", self.lines)
        saved = spot_check.read_jsonl(os.path.join(self.dir, "run-test.grades.jsonl"))
        self.assertEqual([g["date"] for g in saved], [first])

    def test_a_wrong_answer_is_asked_again(self):
        notes = [make_note(3, ["fatigue"])]
        self.write_log("run-test", notes)
        grades = self.grade(["maybe", "y", ""], size=1, notes=notes)
        self.assertEqual(grades[0]["tags"], {"fatigue": True})

    def test_note_edited_since_the_run_is_skipped(self):
        edited_date = self.order()[0]
        notes = [make_note(int(n["date"][-2:]), n["tags"], edited=n["date"] == edited_date)
                 for n in self.notes]
        grades = self.grade(self.answers_for_all(), size=2, notes=notes)
        self.assertEqual([g["date"] for g in grades], self.order()[1:3])
        self.assertTrue(any("skipped, edited or deleted" in line for line in self.lines))

    def test_same_notes_as_grades_the_earlier_runs_notes_with_the_new_tags(self):
        # The earlier run's grades: notes 3 then 1, in that order.
        with open(os.path.join(self.dir, "run-old.grades.jsonl"), "w") as f:
            for date in ("2026-01-03", "2026-01-01"):
                f.write(json.dumps({"date": date, "field": "notes", "tags": {}, "missing": ["x"]}) + "\n")
        # The new run tagged note 1 differently and never tagged note 3.
        new = [make_note(1, ["joints"]), make_note(2, ["pain"])]
        self.write_log("run-new", new)
        grades = spot_check.grade(FakeTracker(self.notes), VOCAB, "run-new", size=30, log_dir=self.dir,
                                  ask_input=typed("y", ""), out=self.lines.append,
                                  same_notes_as="run-old")
        self.assertEqual(grades, [{"date": "2026-01-01", "field": "notes",
                                   "tags": {"joints": True}, "missing": []}])
        self.assertIn("2026-01-03 general: not tagged in this run, left out", self.lines)
        self.assertIn("run-new: 0 of 1 graded so far (the notes graded for run-old)", self.lines)

    def test_summary_counts_only(self):
        log_rows = spot_check.read_jsonl(os.path.join(self.dir, "run-test.jsonl"))
        grades = [
            {"date": "2026-01-01", "field": "notes", "tags": {"joints": True, "pain": False},
             "missing": ["ankles", "swelling-typed-word"]},
            {"date": "2026-01-02", "field": "notes", "tags": {}, "missing": []},
            {"date": "2026-01-03", "field": "notes", "tags": {"old tag": True}, "missing": []},
        ]
        text = spot_check.summarize(VOCAB, log_rows, grades)
        self.assertIn("run: 4 notes, 3 tagged, 1 failed, 1 with no tags (33%), 1.0 tags per note", text)
        self.assertIn("tags marked right: 2 of 3 (67%)", text)
        self.assertIn("notes missing something: 1 of 3 (33%)", text)
        self.assertIn("missing items: 2, 1 already in the vocabulary (Qwen missed it), 1 not", text)
        self.assertIn("notes with no tags: 1, rightly empty 1", text)
        self.assertIn("right by category: body_part 1/1, symptom 0/1, no longer in vocabulary 1/1", text)
        for private in ("SECRET-TEXT", "joints", "pain", "ankles", "swelling-typed-word"):
            self.assertNotIn(private, text)

    def test_latest_run_ignores_grades_and_empty_logs(self):
        open(os.path.join(self.dir, "run-zzz.jsonl"), "w").close()
        with open(os.path.join(self.dir, "run-test.grades.jsonl"), "w") as f:
            f.write("{}\n")
        self.assertEqual(spot_check.latest_run(self.dir), "run-test")

    def test_summary_command_needs_no_tracker(self):
        cwd = os.getcwd()
        vocab_path = os.path.join(cwd, "vocab.yaml")
        os.makedirs(os.path.join(self.dir, "runs"))
        os.replace(os.path.join(self.dir, "run-test.jsonl"), os.path.join(self.dir, "runs", "run-test.jsonl"))
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(spot_check.Tracker, "from_config") as from_config:
            code = spot_check.main(["summary", "--vocab", vocab_path])
        self.assertEqual(code, 0)
        from_config.assert_not_called()
        self.assertIn("graded: 0 notes", out.getvalue())


if __name__ == "__main__":
    unittest.main()
