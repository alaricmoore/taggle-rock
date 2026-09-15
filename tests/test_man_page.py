"""
The man page, man/taggle-rock.1.

- Every option the scripts accept is in it, so it can't quietly fall behind.
- groff formats it without warnings (skipped if groff isn't installed).
"""

import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "man", "taggle-rock.1")
SCRIPTS = ("tag_run.py", "draft_vocab.py", "spot_check.py")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class ManPageTest(unittest.TestCase):
    def test_every_option_is_documented(self):
        page = read(PAGE)
        for script in SCRIPTS:
            self.assertIn(script, page)
            options = re.findall(r'add_argument\("(--[a-z-]+)"', read(os.path.join(ROOT, script)))
            self.assertTrue(options, f"no options found in {script}")
            for option in options:
                with self.subTest(script=script, option=option):
                    self.assertIn(option.replace("-", r"\-"), page)

    @unittest.skipUnless(shutil.which("groff"), "groff is not installed")
    def test_groff_formats_it_without_warnings(self):
        # -k runs preconv first, as man(1) does, so the diagram's box-drawing
        # characters are read as UTF-8 rather than warned about.
        result = subprocess.run(["groff", "-k", "-man", "-Tutf8", "-ww", "-z", PAGE],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
