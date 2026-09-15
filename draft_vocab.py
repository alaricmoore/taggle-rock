"""
Draft vocabulary suggestions from your own notes, with Qwen, on this laptop.

    python3 draft_vocab.py                  read every note, write drafts/vocab_draft.yaml
    python3 draft_vocab.py --limit 50       try it on the first 50 notes
    python3 draft_vocab.py --min-notes 3    only suggest what came up in 3+ notes

Your notes go only to Qwen (Ollama on localhost), and vocab.yaml is never
changed: the draft is for you to read, edit and copy from. It holds words from
your notes, so it's written readable only by you, in drafts/ (gitignored), and
nothing from a note is printed.

Qwen's suggestions are checked by rules, not trusted: a phrase he says is in a
note but isn't gets thrown out, words already in the vocabulary are skipped,
and a tag spelled in a way the tracker wouldn't accept is dropped. Answers are
cached per note, so a stopped run carries on where it left off.
"""

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime

import qwen
import vocab as vocab_module
from tracker import Tracker, TrackerError

DRAFT_DIR = "drafts"


def norm(text: str) -> str:
    """Lowercase with single spaces, for comparing phrases with note text."""
    return " ".join(str(text).lower().split())


def _private_dir(path: str):
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _private_open(path: str, mode: str):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    f = os.fdopen(os.open(path, flags, 0o600), mode, encoding="utf-8")
    os.chmod(path, 0o600)
    return f


class Cache:
    """drafts/cache.jsonl: Qwen's answer per (note hash, vocabulary version)."""

    def __init__(self, path: str):
        self.path = path
        self.answers = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        self.answers[(entry["sha256"], entry["vocab"])] = entry["candidates"]
                    except (ValueError, KeyError, TypeError):
                        continue   # a line cut off by a stopped run
        self._f = None

    def get(self, sha: str, vocab_version: str):
        return self.answers.get((sha, vocab_version))

    def put(self, sha: str, vocab_version: str, candidates: list):
        if self._f is None:
            self._f = _private_open(self.path, "a")
        self.answers[(sha, vocab_version)] = candidates
        self._f.write(json.dumps({"sha256": sha, "vocab": vocab_version, "candidates": candidates}) + "\n")
        self._f.flush()

    def close(self):
        if self._f:
            self._f.close()


def tally(vocab, answers):
    """Sort Qwen's suggestions into new tags and new words for existing tags.

    `answers` is [(note_text, [{phrase, tag, category}])]. Returns
    (new, more, dropped): new and more map tag -> {"phrases": Counter,
    "notes": set of note numbers}, new also has "categories": Counter;
    dropped counts suggestions thrown out, by reason.
    """
    word_to_tag = {norm(w): tag for tag, words in vocab.words_for.items() for w in words}
    known_words = set(word_to_tag) | set(vocab.tags)
    new, more, dropped = {}, {}, Counter()
    for number, (text, candidates) in enumerate(answers):
        note = norm(text)
        for c in candidates:
            phrase, tag = norm(c["phrase"]), norm(c["tag"])
            # A "new" tag that is already a word for an existing tag belongs to
            # that tag: "pred 10mg" suggested as "prednisone" goes under steroids.
            tag = word_to_tag.get(tag, tag) if tag not in vocab.category_of else tag
            if not phrase or phrase not in note:
                dropped["phrase not actually in the note"] += 1
            elif phrase in known_words:
                dropped["already in the vocabulary"] += 1
            elif tag in vocab.category_of:
                entry = more.setdefault(tag, {"phrases": Counter(), "notes": set()})
                entry["phrases"][phrase] += 1
                entry["notes"].add(number)
            elif vocab_module.TAG_RE.fullmatch(tag):
                entry = new.setdefault(tag, {"phrases": Counter(), "notes": set(), "categories": Counter()})
                entry["phrases"][phrase] += 1
                entry["notes"].add(number)
                entry["categories"][c["category"]] += 1
            else:
                dropped["tag not spelled the way the tracker needs"] += 1
    return new, more, dropped


def render(vocab, new, more, min_notes: int, header: list) -> str:
    """The draft as YAML shaped like vocab.yaml, most common first."""
    q = json.dumps   # a JSON string is a valid YAML string, quotes and all
    by_category = {c: {"new": [], "more": []} for c in vocab_module.CATEGORIES}
    for tag, e in new.items():
        if len(e["notes"]) >= min_notes:
            # Majority vote; ties go to the category order in vocab.CATEGORIES.
            best = max(vocab_module.CATEGORIES, key=lambda c: (e["categories"][c], -vocab_module.CATEGORIES.index(c)))
            by_category[best]["new"].append((tag, e))
    for tag, e in more.items():
        if len(e["notes"]) >= min_notes:
            by_category[vocab.category_of[tag]]["more"].append((tag, e))

    lines = [f"# {line}" if line else "#" for line in header]
    lines += ["", "version: draft"]
    for category, groups in by_category.items():
        if not (groups["new"] or groups["more"]):
            continue
        lines += ["", f"{category}:"]
        for kind, title in (("new", "new tags"), ("more", "more words for tags you already have")):
            entries = sorted(groups[kind], key=lambda te: (-len(te[1]["notes"]), te[0]))
            if not entries:
                continue
            lines.append(f"  # {title}")
            for tag, e in entries:
                phrases = [p for p, _ in e["phrases"].most_common() if p != tag]
                n = len(e["notes"])
                lines.append(f"  {q(tag)}: [{', '.join(q(p) for p in phrases)}]  # {n} note{'s' if n != 1 else ''}")
    return "\n".join(lines) + "\n"


def draft(tracker, vocab, ask=qwen.ask_candidates, limit=None, min_notes=2,
          directory=DRAFT_DIR, out=print, now=None):
    """Read notes, ask Qwen, write the draft. Returns a summary dict (also printed)."""
    notes = tracker.notes()
    if limit:
        notes = notes[:limit]
    _private_dir(directory)
    cache = Cache(os.path.join(directory, "cache.jsonl"))
    counts = Counter()
    answers = []
    stopped = False
    out(f"reading {len(notes)} note(s) with vocabulary {vocab.version}")
    try:
        for i, note in enumerate(notes, 1):
            where = f"[{i}/{len(notes)}] {note['date']} {qwen.field_label(note['field'])}"
            cached = cache.get(note["sha256"], vocab.version)
            if cached is not None:
                counts["from cache"] += 1
                answers.append((note["text"], cached))
                continue
            try:
                candidates = ask(vocab, note["field"], note["text"])
            except qwen.QwenError as e:
                counts["qwen failed"] += 1
                out(f"{where}: skipped, {e}")
                continue
            counts["asked"] += 1
            cache.put(note["sha256"], vocab.version, candidates)
            answers.append((note["text"], candidates))
            out(f"{where}: {len(candidates)} suggestion(s)")
    except KeyboardInterrupt:
        stopped = True
        out("stopped: writing a draft from the notes read so far (run again to carry on)")
    finally:
        cache.close()

    new, more, dropped = tally(vocab, answers)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    header = [
        f"Vocabulary draft from {qwen.MODEL}, {stamp}: {len(answers)} of {len(notes)} notes read,"
        f" vocabulary {vocab.version}." + (" Stopped early." if stopped else ""),
        "",
        "Nothing here is used until you copy it into vocab.yaml. Edit freely:",
        "rename tags, move them to another category, merge them, delete what's wrong.",
        "Phrases are as Qwen quoted them from your notes, lowercased, and only ones",
        "actually found in a note. The count is how many notes each tag came up in",
        f"(at least {min_notes} to be listed).",
    ]
    path = os.path.join(directory, "vocab_draft.yaml")
    with _private_open(path, "w") as f:
        f.write(render(vocab, new, more, min_notes, header))

    summary = {
        "notes": len(notes), "asked": counts["asked"], "from_cache": counts["from cache"],
        "qwen_failed": counts["qwen failed"],
        "new_tags": sum(len(e["notes"]) >= min_notes for e in new.values()),
        "existing_tags_with_new_words": sum(len(e["notes"]) >= min_notes for e in more.values()),
        "dropped": dict(dropped), "draft": path, "stopped": stopped,
    }
    out(f"{summary['asked']} asked, {summary['from_cache']} from cache, {summary['qwen_failed']} failed; "
        f"{summary['new_tags']} new tag(s), {summary['existing_tags_with_new_words']} existing tag(s) "
        f"with new words; dropped: {dict(dropped) or 'none'}")
    out(f"draft written to {path}")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Draft vocabulary suggestions from your notes with Qwen.")
    parser.add_argument("--limit", type=int, help="read at most this many notes")
    parser.add_argument("--min-notes", type=int, default=2, help="list a tag only if it came up in this many notes")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--vocab", default="vocab.yaml")
    args = parser.parse_args(argv)
    try:
        draft(Tracker.from_config(args.config), vocab_module.load(args.vocab),
              limit=args.limit, min_notes=args.min_notes)
        return 0
    except (TrackerError, vocab_module.VocabError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
