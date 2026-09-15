"""
Qwen's side, with Ollama faked: what is asked, and what is accepted back.

- The request pins Qwen to the vocabulary with a schema, turns thinking off,
  and sends the box and note text.
- The instructions are identical for every note (so Ollama can reuse them).
- His answer is checked again: unknown tags, malformed answers and too many
  tags are refused, repeats are dropped, categories come from the vocabulary.
- Ollama being down or erroring is a QwenError for that note, not a crash.
"""

import io
import json
import unittest
import urllib.error
from unittest import mock

import qwen
import vocab

VOCAB = vocab.parse("""
version: 1
body_part:
  lymph nodes: [glands, nodes]
symptom:
  fatigue: [tired, wiped out]
  rash: []
context:
  sun exposure: [sun]
""")


def ollama_reply(content):
    return io.BytesIO(json.dumps({"message": {"role": "assistant", "content": content}}).encode())


class TestPrompt(unittest.TestCase):
    def test_schema_allows_only_vocabulary_tags(self):
        s = qwen.schema(VOCAB)
        self.assertEqual(s["properties"]["tags"]["items"]["enum"],
                         ["lymph nodes", "fatigue", "rash", "sun exposure"])
        self.assertEqual(s["properties"]["tags"]["maxItems"], 20)
        self.assertNotIn("confidence", json.dumps(s))

    def test_the_instructions_list_every_tag_and_its_words(self):
        prompt = qwen.system_prompt(VOCAB)
        self.assertIn("lymph nodes: glands, nodes", prompt)
        self.assertIn("\nrash\n", prompt)
        self.assertIn("[body part]", prompt)
        self.assertEqual(prompt, qwen.system_prompt(VOCAB))

    def test_field_labels(self):
        self.assertEqual(qwen.field_label("notes"), "general")
        self.assertEqual(qwen.field_label("rheumatic_notes"), "rheumatic")
        self.assertEqual(qwen.field_label("air_hunger_notes"), "air hunger")


class TestCheckAnswer(unittest.TestCase):
    def test_tags_get_their_categories_and_repeats_are_dropped(self):
        self.assertEqual(qwen.check_answer(VOCAB, '{"tags": ["fatigue", "lymph nodes", "fatigue"]}'),
                         [("fatigue", "symptom"), ("lymph nodes", "body_part")])

    def test_no_tags_is_a_valid_answer(self):
        self.assertEqual(qwen.check_answer(VOCAB, '{"tags": []}'), [])

    def test_bad_answers_are_refused(self):
        for content in ['{"tags": ["lupus"]}', "not json", '{"tag": []}', '["fatigue"]',
                        None, json.dumps({"tags": ["fatigue"] * 1 + [f"x{i}" for i in range(3)]})]:
            with self.subTest(content=content), self.assertRaises(qwen.QwenError):
                qwen.check_answer(VOCAB, content)

    def test_more_than_twenty_distinct_tags_is_refused(self):
        big = vocab.parse("version: 1\nsymptom:\n" + "".join(f"  tag {i}: []\n" for i in range(25)))
        with self.assertRaises(qwen.QwenError):
            qwen.check_answer(big, json.dumps({"tags": [f"tag {i}" for i in range(21)]}))


class TestAsk(unittest.TestCase):
    def test_the_request_and_the_answer(self):
        with mock.patch("qwen.urllib.request.urlopen",
                        return_value=ollama_reply('{"tags": ["lymph nodes"]}')) as urlopen:
            tags = qwen.ask(VOCAB, "rheumatic_notes", "made-up: glands up again")
        self.assertEqual(tags, [("lymph nodes", "body_part")])
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "http://localhost:11434/api/chat")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "qwen-local")
        self.assertIs(body["think"], False)
        self.assertIs(body["stream"], False)
        self.assertEqual(body["format"], qwen.schema(VOCAB))
        self.assertEqual(body["options"]["temperature"], 0)
        self.assertEqual(body["messages"][0], {"role": "system", "content": qwen.system_prompt(VOCAB)})
        self.assertEqual(body["messages"][1]["content"], "Box: rheumatic\nNote:\nmade-up: glands up again")

    def test_ollama_trouble_is_a_qwen_error(self):
        failures = [
            urllib.error.URLError("connection refused"),
            urllib.error.HTTPError("http://localhost:11434/api/chat", 500, "boom", {}, io.BytesIO(b"model not found")),
            TimeoutError("timed out"),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("qwen.urllib.request.urlopen", side_effect=failure), \
                    self.assertRaises(qwen.QwenError):
                qwen.ask(VOCAB, "notes", "made-up note")

    def test_a_reply_that_is_not_json_is_a_qwen_error(self):
        with mock.patch("qwen.urllib.request.urlopen", return_value=io.BytesIO(b"<html>")), \
                self.assertRaises(qwen.QwenError):
            qwen.ask(VOCAB, "notes", "made-up note")


if __name__ == "__main__":
    unittest.main()
