"""
vocab.yaml is checked against the tracker's rules when a run starts: tag
spelling, known categories, no tag listed twice, words as lists. The shipped
skeleton must pass.
"""

import unittest

import vocab

GOOD = """
version: 2
body_part:
  lymph nodes: [glands, nodes]
symptom:
  fatigue: [tired]
  rash:
"""


class TestVocab(unittest.TestCase):
    def test_the_shipped_vocabulary_loads(self):
        v = vocab.load("vocab.yaml")
        self.assertTrue(v.version.startswith("v1-"))
        self.assertIn("lymph nodes", v.tags)
        self.assertEqual(v.category_of["lymph nodes"], "body_part")
        self.assertTrue(all(vocab.TAG_RE.fullmatch(t) for t in v.tags))

    def test_parses_tags_categories_and_words(self):
        v = vocab.parse(GOOD)
        self.assertEqual(v.tags, ["lymph nodes", "fatigue", "rash"])
        self.assertEqual(v.category_of, {"lymph nodes": "body_part", "fatigue": "symptom", "rash": "symptom"})
        self.assertEqual(v.words_for["lymph nodes"], ("glands", "nodes"))
        self.assertEqual(v.words_for["rash"], ())

    def test_the_version_changes_with_the_contents(self):
        a = vocab.parse(GOOD).version
        b = vocab.parse(GOOD.replace("[tired]", "[tired, wiped out]")).version
        self.assertTrue(a.startswith("v2-") and b.startswith("v2-"))
        self.assertNotEqual(a, b)

    def test_problems_are_refused_with_a_reason(self):
        cases = {
            "not yaml": "version: [1\n",
            "not a mapping": "- a\n- b\n",
            "no version": "symptom:\n  fatigue: [tired]\n",
            "unknown category": "version: 1\ndiagnosis:\n  lupus: []\n",
            "capital letters": "version: 1\nsymptom:\n  Fatigue: [tired]\n",
            "non-ascii": "version: 1\nsymptom:\n  sjögren's: []\n",
            "trailing space": "version: 1\nsymptom:\n  'fatigue ': []\n",
            "listed twice": "version: 1\nsymptom:\n  rash: []\nbody_part:\n  rash: []\n",
            "words not a list": "version: 1\nsymptom:\n  fatigue: tired\n",
            "empty word": "version: 1\nsymptom:\n  fatigue: ['']\n",
            "no tags": "version: 1\nsymptom: {}\n",
            "category not a mapping": "version: 1\nsymptom: [fatigue]\n",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(vocab.VocabError):
                vocab.parse(text)


if __name__ == "__main__":
    unittest.main()
