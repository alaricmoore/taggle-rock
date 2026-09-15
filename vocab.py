"""
Loading and checking vocab.yaml.

The vocabulary is the list of tags Qwen may use, with the words that mean
each one. It is checked against the same rules the tracker enforces, so a
typo is caught here when the run starts, not as hundreds of "invalid" results
from the server afterwards.
"""

import hashlib
import re
from dataclasses import dataclass

import yaml

# The categories the tracker accepts (private-track db.TAG_CATEGORIES).
CATEGORIES = ("body_part", "symptom", "severity", "context")

# The tag spelling the tracker accepts (private-track routes/tags_api.py _TAG_RE).
TAG_RE = re.compile(r"[a-z0-9](?:[a-z0-9 '/-]{0,46}[a-z0-9])?")


class VocabError(ValueError):
    """vocab.yaml is malformed. The message says where."""


@dataclass(frozen=True)
class Vocab:
    version: str        # "v1-3fa2b9c0": the file's version, plus a hash of its contents
    category_of: dict   # tag -> category
    words_for: dict     # tag -> tuple of words that mean it

    @property
    def tags(self) -> list:
        """Every tag, in file order."""
        return list(self.category_of)


def parse(text: str) -> Vocab:
    """A Vocab from the text of vocab.yaml. Raises VocabError on anything off."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise VocabError(f"not valid YAML: {e}") from None
    if not isinstance(data, dict):
        raise VocabError("the file must be a mapping of version and categories")

    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, (int, str)) or version == "":
        raise VocabError("version is required (a number, bumped when a tag's meaning changes)")
    unknown = sorted(set(data) - {"version", *CATEGORIES})
    if unknown:
        raise VocabError(f"unknown top-level keys {unknown}; categories are {list(CATEGORIES)}")

    category_of, words_for = {}, {}
    for category in CATEGORIES:
        entries = data.get(category) or {}
        if not isinstance(entries, dict):
            raise VocabError(f"{category}: must be a mapping of tag: [words]")
        for tag, words in entries.items():
            where = f"{category} / {tag!r}"
            if not (isinstance(tag, str) and TAG_RE.fullmatch(tag)):
                raise VocabError(f"{where}: tags must be lowercase ASCII words of 1-48 characters")
            if tag in category_of:
                raise VocabError(f"{where}: already listed under {category_of[tag]}")
            words = words or []
            if not (isinstance(words, list) and all(isinstance(w, str) and w.strip() for w in words)):
                raise VocabError(f"{where}: words must be a list of text, like [glands, nodes]")
            category_of[tag] = category
            words_for[tag] = tuple(w.strip() for w in words)
    if not category_of:
        raise VocabError("the vocabulary has no tags")

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return Vocab(version=f"v{version}-{digest}", category_of=category_of, words_for=words_for)


def load(path: str = "vocab.yaml") -> Vocab:
    with open(path, encoding="utf-8") as f:
        return parse(f.read())
