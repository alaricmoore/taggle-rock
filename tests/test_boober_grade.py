"""
Boober grading the Doozer's tags, with Ollama faked and a made-up tracker.

- The request: Boober, thinking on, one true/false per tag under review and
  missing tags only from the vocabulary; the instructions are the same for
  every note and hold tags to the rules they were made under.
- His answer is checked again: marks for other tags, marks that aren't
  true/false, missing tags outside the vocabulary and non-JSON are refused.
- The tracker is opened read-only, and only real note columns are read.
- Calibration is exactly the notes and tags you graded, skipping a note
  edited since its run; fresh notes are ones nobody graded, with tags made
  from the text as it is now, in a fixed order.
- Grades are saved without note text, readable only by you; a rerun carries
  on, and a note that fails twice is left for next time.
- The summary has counts only.
"""

import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest import mock

import boober_grade
import qwen
import vocab

VOCAB = vocab.parse("""
version: 1
body_part:
  joints: [ankles, knees]
symptom:
  pain: [killing me]
  fatigue: [tired]
""")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ollama_reply(content):
    return io.BytesIO(json.dumps({"message": {"role": "assistant", "content": content}}).encode())


def answer(verdicts, missing=()):
    return json.dumps({"verdicts": verdicts, "missing": list(missing)})


class TestPrompt(unittest.TestCase):
    def test_the_instructions_list_every_tag_and_its_words(self):
        prompt = boober_grade.system_prompt(VOCAB)
        self.assertIn("[body part]", prompt)
        self.assertIn("joints: ankles, knees", prompt)
        self.assertIn("fatigue: tired", prompt)
        self.assertEqual(prompt, boober_grade.system_prompt(VOCAB))

    def test_tags_are_held_to_the_rules_they_were_made_under(self):
        prompt = boober_grade.system_prompt(VOCAB)
        self.assertIn("merely likely, related, or usually true", prompt)
        self.assertIn("\"no rash today\" is not rash", prompt)
        self.assertIn("Missing means clearly earned, not possible", prompt)

    def test_prompt_id_changes_with_the_vocabulary(self):
        other = vocab.parse("version: 1\nsymptom:\n  fatigue: [tired]\n")
        self.assertEqual(boober_grade.prompt_id(VOCAB), boober_grade.prompt_id(VOCAB))
        self.assertNotEqual(boober_grade.prompt_id(VOCAB), boober_grade.prompt_id(other))

    def test_schema_marks_exactly_the_tags_under_review(self):
        s = boober_grade.schema(VOCAB, ["joints", "old tag"])
        self.assertEqual(s["properties"]["verdicts"]["required"], ["joints", "old tag"])
        self.assertEqual(s["properties"]["verdicts"]["properties"]["old tag"], {"type": "boolean"})
        self.assertEqual(s["properties"]["missing"]["items"]["enum"], ["joints", "pain", "fatigue"])


class TestAsk(unittest.TestCase):
    def ask(self, content, tags=("joints", "pain"), text="ankles killing me SECRET-TEXT"):
        with mock.patch("urllib.request.urlopen", return_value=ollama_reply(content)) as urlopen:
            result = boober_grade.ask(VOCAB, "rheumatic_notes", text, list(tags))
        return result, json.loads(urlopen.call_args.args[0].data)

    def test_the_request(self):
        _, body = self.ask(answer({"joints": True, "pain": True}))
        self.assertEqual(body["model"], "boober")
        self.assertEqual(body["think"], "medium")
        self.assertEqual(body["options"], {"temperature": 0})
        self.assertFalse(body["stream"])
        self.assertEqual(body["messages"][0]["content"], boober_grade.system_prompt(VOCAB))
        user = body["messages"][1]["content"]
        self.assertIn("Box: rheumatic", user)
        self.assertIn("ankles killing me SECRET-TEXT", user)
        self.assertIn("Tags to check: joints, pain", user)

    def test_a_note_with_no_tags_is_still_checked_for_missing_ones(self):
        result, body = self.ask(answer({}, ["fatigue"]), tags=())
        self.assertIn("(none: the note got no tags)", body["messages"][1]["content"])
        self.assertEqual(result, {"tags": {}, "missing": ["fatigue"]})

    def test_marks_come_back_in_review_order_and_missing_is_tidied(self):
        result, _ = self.ask(answer({"pain": False, "joints": True}, ["fatigue", "pain", "fatigue"]))
        self.assertEqual(list(result["tags"].items()), [("joints", True), ("pain", False)])
        self.assertEqual(result["missing"], ["fatigue"], "repeats and tags already under review drop out")

    def test_bad_answers_are_refused(self):
        for content in [answer({"joints": True}),                        # a tag left unmarked
                        answer({"joints": True, "pain": True, "fatigue": True}),  # an extra mark
                        answer({"joints": "yes", "pain": True}),         # not true/false
                        answer({"joints": True, "pain": True}, ["lupus"]),  # not in the vocabulary
                        "not json", None, '{"verdicts": {}}']:
            with self.subTest(content=content), self.assertRaises(qwen.QwenError):
                self.ask(content)

    def test_ollama_down_or_erroring_is_a_qwen_error(self):
        errors = [urllib.error.URLError("refused"),
                  urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"model failed"))]
        for error in errors:
            with self.subTest(error=error), mock.patch("urllib.request.urlopen", side_effect=error), \
                    self.assertRaises(qwen.QwenError):
                boober_grade.ask(VOCAB, "notes", "x", ["joints"])


class TrackerTest(unittest.TestCase):
    """A made-up tracker database and runs folder."""

    NOTES = {
        ("2026-01-01", "neuro_notes"): "tingles SECRET-TEXT",
        ("2026-01-02", "neuro_notes"): "ankles killing me SECRET-TEXT",
        ("2026-01-03", "neuro_notes"): "tired SECRET-TEXT",
        ("2026-01-04", "neuro_notes"): "made soup SECRET-TEXT",
        ("2026-01-05", "neuro_notes"): "knees SECRET-TEXT",
        ("2026-01-01", "notes"): "general box SECRET-TEXT",
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "runs")
        os.makedirs(self.dir)
        self.db = os.path.join(self.tmp.name, "tracker.db")
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE daily_observations (user_id INTEGER, date TEXT, notes TEXT, "
                    "neuro_notes TEXT, derm_notes TEXT)")
        con.execute("CREATE TABLE tagged_notes (user_id INTEGER, date TEXT, field TEXT, note_sha256 TEXT)")
        con.execute("CREATE TABLE note_tags (user_id INTEGER, date TEXT, field TEXT, tag TEXT)")
        for day in sorted({d for d, _ in self.NOTES}):
            con.execute("INSERT INTO daily_observations VALUES (1, ?, ?, ?, '  ')",
                        (day, self.NOTES.get((day, "notes")), self.NOTES.get((day, "neuro_notes"))))
        con.execute("INSERT INTO daily_observations VALUES (2, '2026-01-01', NULL, 'someone else', NULL)")
        con.commit()
        con.close()
        self.texts = boober_grade.note_texts(self.db, 1, {"notes", "neuro_notes", "derm_notes"})

    def tag_in_db(self, day, field, tags, text=None):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO tagged_notes VALUES (1, ?, ?, ?)",
                    (day, field, sha(text or self.NOTES[(day, field)])))
        con.executemany("INSERT INTO note_tags VALUES (1, ?, ?, ?)", [(day, field, t) for t in tags])
        con.commit()
        con.close()

    def write_run(self, run_id, logged, grades):
        """logged: {(date, field): text the run saw}; grades: spot_check.py rows."""
        with open(os.path.join(self.dir, f"{run_id}.jsonl"), "w") as f:
            for (day, field), text in logged.items():
                f.write(json.dumps({"date": day, "field": field, "sha256": sha(text),
                                    "tags": [], "result": "tagged"}) + "\n")
        with open(os.path.join(self.dir, f"{run_id}.grades.jsonl"), "w") as f:
            for g in grades:
                f.write(json.dumps(g) + "\n")


class TestReading(TrackerTest):
    def test_note_texts_are_one_users_non_empty_notes(self):
        self.assertEqual(self.texts, self.NOTES)

    def test_the_tracker_is_opened_read_only(self):
        with mock.patch("sqlite3.connect", wraps=sqlite3.connect) as connect:
            boober_grade.note_texts(self.db, 1, {"neuro_notes"})
        self.assertTrue(connect.call_args.args[0].endswith("?mode=ro"))

    def test_only_real_note_columns_are_read(self):
        texts = boober_grade.note_texts(self.db, 1, {"neuro_notes", 'notes" FROM x; --', "user_id"})
        self.assertEqual({f for _, f in texts}, {"neuro_notes"})


class TestCalibration(TrackerTest):
    def test_your_notes_with_exactly_the_tags_you_graded(self):
        self.write_run("run-a", {("2026-01-02", "neuro_notes"): self.NOTES[("2026-01-02", "neuro_notes")]},
                       [{"date": "2026-01-02", "field": "neuro_notes",
                         "tags": {"joints": True, "old tag": False}, "missing": []}])
        self.assertEqual(boober_grade.calibration_items(self.texts, self.dir),
                         [{"date": "2026-01-02", "field": "neuro_notes",
                           "tags": ["joints", "old tag"], "source": "run-a"}])

    def test_a_note_edited_since_its_run_or_not_in_the_log_is_skipped(self):
        self.write_run("run-a", {("2026-01-02", "neuro_notes"): "the text before an edit"},
                       [{"date": "2026-01-02", "field": "neuro_notes", "tags": {"pain": True}, "missing": []},
                        {"date": "2026-01-03", "field": "neuro_notes", "tags": {"fatigue": True}, "missing": []}])
        self.assertEqual(boober_grade.calibration_items(self.texts, self.dir), [])

    def test_a_note_graded_in_two_runs_is_in_once_per_run(self):
        logged = {("2026-01-02", "neuro_notes"): self.NOTES[("2026-01-02", "neuro_notes")]}
        for run_id, tags in (("run-a", {"pain": True}), ("run-b", {"joints": True})):
            self.write_run(run_id, logged, [{"date": "2026-01-02", "field": "neuro_notes",
                                             "tags": tags, "missing": []}])
        items = boober_grade.calibration_items(self.texts, self.dir)
        self.assertEqual([(i["source"], i["tags"]) for i in items], [("run-a", ["pain"]), ("run-b", ["joints"])])


class TestFresh(TrackerTest):
    def fresh(self, size=30, skip=frozenset({"notes"})):
        return boober_grade.fresh_items(self.db, self.texts, 1, set(skip), size, directory=self.dir)

    def test_ungraded_notes_with_the_tags_they_have_now(self):
        self.tag_in_db("2026-01-02", "neuro_notes", ["pain", "joints"])
        self.tag_in_db("2026-01-04", "neuro_notes", [])
        items = sorted(self.fresh(), key=lambda i: i["date"])
        self.assertEqual(items, [
            {"date": "2026-01-02", "field": "neuro_notes", "tags": ["joints", "pain"], "source": "db:tracker.db"},
            {"date": "2026-01-04", "field": "neuro_notes", "tags": [], "source": "db:tracker.db"},
        ])

    def test_skipped_boxes_graded_notes_and_stale_tags_are_left_out(self):
        self.tag_in_db("2026-01-01", "notes", ["fatigue"])                        # a skipped box
        self.tag_in_db("2026-01-03", "neuro_notes", ["fatigue"])                  # graded by you
        self.tag_in_db("2026-01-05", "neuro_notes", ["joints"], text="older text")  # tagged before an edit
        self.write_run("run-a", {}, [{"date": "2026-01-03", "field": "neuro_notes", "tags": {}, "missing": []}])
        self.assertEqual(self.fresh(), [])

    def test_the_sample_is_fixed_and_a_smaller_one_is_its_start(self):
        for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"):
            self.tag_in_db(day, "neuro_notes", [])
        everything = self.fresh()
        self.assertEqual(everything, self.fresh())
        self.assertEqual(self.fresh(size=2), everything[:2])


class TestGradeAll(TrackerTest):
    ITEMS = [{"date": "2026-01-02", "field": "neuro_notes", "tags": ["joints", "pain"], "source": "run-a"},
             {"date": "2026-01-03", "field": "neuro_notes", "tags": ["fatigue"], "source": "run-a"}]

    def run_grades(self, replies, items=ITEMS):
        path = os.path.join(self.dir, "boober-calibration.grades.jsonl")
        out = io.StringIO()
        with mock.patch.object(boober_grade, "ask", side_effect=replies) as ask, redirect_stdout(out):
            boober_grade.grade_all(VOCAB, items, self.texts, path, "calibration", "medium")
        return path, ask, out.getvalue()

    def test_grades_are_saved_without_note_text_and_only_you_can_read_them(self):
        path, _, out = self.run_grades([{"tags": {"joints": True, "pain": False}, "missing": ["fatigue"]},
                                        {"tags": {"fatigue": True}, "missing": []}])
        rows = boober_grade.read_jsonl(path)
        self.assertEqual(rows[0]["tags"], {"joints": True, "pain": False})
        self.assertEqual(rows[0]["missing"], ["fatigue"])
        self.assertEqual((rows[0]["source"], rows[0]["model"], rows[0]["think"]), ("run-a", "boober", "medium"))
        self.assertEqual(rows[0]["prompt"], boober_grade.prompt_id(VOCAB))
        with open(path) as f:
            self.assertNotIn("SECRET-TEXT", f.read())
        self.assertNotIn("SECRET-TEXT", out)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_a_rerun_carries_on(self):
        self.run_grades([{"tags": {"joints": True, "pain": True}, "missing": []}], items=self.ITEMS[:1])
        path, ask, out = self.run_grades([{"tags": {"fatigue": True}, "missing": []}])
        self.assertEqual(ask.call_count, 1)
        self.assertIn("calibration: 2 notes, 1 already graded", out)
        self.assertEqual(len(boober_grade.read_jsonl(path)), 2)

    def test_one_failure_is_tried_again_and_two_leave_the_note_for_next_time(self):
        fail = qwen.QwenError("answer is not the JSON asked for")
        path, ask, out = self.run_grades([fail, {"tags": {"joints": True, "pain": True}, "missing": []},
                                          fail, fail])
        self.assertEqual(ask.call_count, 4)
        self.assertEqual([r["date"] for r in boober_grade.read_jsonl(path)], ["2026-01-02"])
        self.assertIn("attempt 2 failed", out)


class TestSummary(TrackerTest):
    def write_boober(self, name, rows):
        with open(os.path.join(self.dir, name), "w") as f:
            for r in rows:
                f.write(json.dumps({"source": "run-a", "field": "neuro_notes", **r}) + "\n")

    def test_agreement_counts_only(self):
        self.write_run("run-a", {}, [
            {"date": "2026-01-02", "field": "neuro_notes",
             "tags": {"joints": True, "pain": True, "fatigue": False}, "missing": ["swelling-typed-word"]},
            {"date": "2026-01-03", "field": "neuro_notes", "tags": {"old tag": False}, "missing": []},
            {"date": "2026-01-04", "field": "neuro_notes", "tags": {"pain": True}, "missing": []},  # not graded by Boober
        ])
        self.write_boober("boober-calibration.grades.jsonl", [
            {"date": "2026-01-02", "tags": {"joints": True, "pain": False, "fatigue": False}, "missing": []},
            {"date": "2026-01-03", "tags": {"old tag": True}, "missing": []},
        ])
        self.write_boober("boober-fresh.grades.jsonl", [
            {"date": "2026-01-05", "source": "db:x", "tags": {"joints": True, "pain": False}, "missing": ["fatigue"]},
        ])
        text = boober_grade.summarize(VOCAB, self.dir)
        self.assertIn("calibration: 2 notes graded by both of you, 4 tags", text)
        # you: R R W W; Boober: R W W R -> agree 2 of 4, at the rate chance alone gives
        self.assertIn("agree on 2 of 4 tags (50%), kappa 0.00", text)
        self.assertIn("both say right 1, both say wrong 1", text)
        self.assertIn("you say right, Boober wrong 1; you say wrong, Boober right 1", text)
        self.assertIn("agree whether anything is missing: 1 of 2 notes (50%)", text)
        self.assertIn("fresh: 1 notes, Boober marks 1 of 2 tags right (50%), 1 notes missing something", text)
        for private in ("SECRET-TEXT", "joints", "pain", "old tag", "swelling-typed-word"):
            self.assertNotIn(private, text)

    def test_perfect_agreement_is_kappa_one(self):
        self.write_run("run-a", {}, [{"date": "2026-01-02", "field": "neuro_notes",
                                      "tags": {"joints": True, "pain": False}, "missing": []}])
        self.write_boober("boober-calibration.grades.jsonl", [
            {"date": "2026-01-02", "tags": {"joints": True, "pain": False}, "missing": []}])
        self.assertIn("agree on 2 of 2 tags (100%), kappa 1.00", boober_grade.summarize(VOCAB, self.dir))

    def test_nothing_graded_yet(self):
        text = boober_grade.summarize(VOCAB, self.dir)
        self.assertIn("calibration: 0 notes graded by both of you, 0 tags", text)
        self.assertIn("kappa -", text)


if __name__ == "__main__":
    unittest.main()
