"""
Review the vocabulary draft one suggestion at a time, and write vocab.yaml.

    python3 review_vocab.py                  review, the most common first
    python3 review_vocab.py --min-notes 5    only what came up in 5+ notes
    python3 review_vocab.py summary          what's decided so far: counts only

Run it in its own terminal window, not with "!" inside Claude Code: it shows
phrases quoted from your notes.

For each suggestion:

    [y] add   [n] never suggest it again   [s] skip for now   [r] rename
    [c] category   [m] merge into a tag you have   [w] edit the words
    [q] save and stop

Every decision is written to drafts/decisions.jsonl as you make it, readable
only by you, so q and running it again carries on where you left off. The
same file is what stops a rejected suggestion coming back in a later draft.

vocab.yaml is rewritten when you stop: its comments, version and existing
order are kept, new tags are added to their category, and new words are
added to the tags that already exist. The old file is copied to
vocab.yaml.bak first, and nothing is written unless the result still loads.
"""

import argparse
import json
import os
import re
import shutil
import sys

import yaml

import vocab as vocab_module

DRAFT_PATH = "drafts/vocab_draft.yaml"
DECISIONS_PATH = "drafts/decisions.jsonl"
VOCAB_PATH = "vocab.yaml"

# A draft line: two spaces, the quoted tag, its words, and "# N notes", which
# is where the count lives: YAML drops comments, so the file is read by line.
_LINE_RE = re.compile(r'^  ("(?:[^"\\]|\\.)*"): \[(.*)\]  # (\d+) notes?$')
_CATEGORY_RE = re.compile(r"^([a-z_]+):$")
# Safe to write in YAML without quotes.
_PLAIN_RE = re.compile(r"[a-z0-9][a-z0-9 '/-]*")

CATEGORY_KEYS = {str(i + 1): c for i, c in enumerate(vocab_module.CATEGORIES)}
HELP = ("  [y] add  [n] never again  [s] skip  [r] rename  [c] category  "
        "[m] merge into a tag  [w] words  [q] save and stop")


class Stop(Exception):
    """You typed q, or the input ran out."""


def parse_draft(text: str) -> list:
    """[{tag, category, words, notes}] from a draft, most notes first."""
    found, category = [], None
    for line in text.splitlines():
        if _CATEGORY_RE.fullmatch(line) and line[:-1] in vocab_module.CATEGORIES:
            category = line[:-1]
            continue
        match = _LINE_RE.match(line)
        if not (match and category):
            continue
        inside = match.group(2).strip()
        found.append({"tag": json.loads(match.group(1)), "category": category,
                      "words": json.loads(f"[{inside}]") if inside else [],
                      "notes": int(match.group(3))})
    found.sort(key=lambda s: (-s["notes"], s["tag"]))
    return found


def read_decisions(path: str) -> dict:
    """suggested tag -> the last decision made about it."""
    decided = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    decided[entry["suggested"]] = entry
                except (ValueError, KeyError, TypeError):
                    continue   # a line cut off by a stopped run
    return decided


def _private_append(path: str):
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    f = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8")
    os.chmod(path, 0o600)
    return f


def terminal_input(prompt: str, prefill: str = "") -> str:
    """input(), with the old text ready to edit where readline allows it."""
    try:
        import readline
    except ImportError:
        readline = None
    if readline and prefill:
        readline.set_startup_hook(lambda: readline.insert_text(prefill))
        try:
            return input(prompt)
        finally:
            readline.set_startup_hook()
    if prefill:
        print(f"  now: {prefill}")
    return input(prompt)


def _ask(ask, prompt: str, prefill: str = "") -> str:
    try:
        answer = ask(prompt, prefill)
    except EOFError:
        raise Stop from None
    answer = answer.strip()
    if answer.lower() == "q":
        raise Stop
    return answer


def review_one(suggestion: dict, category_of: dict, ask, out) -> dict:
    """Show one suggestion and return the decision. Raises Stop."""
    tag, category = suggestion["tag"], suggestion["category"]
    words = list(suggestion["words"])
    out("")
    out(f"{suggestion['where']}  {category}  {tag}"
        + ("   (more words for a tag you have)" if tag in category_of else ""))
    if words:
        out("  phrases: " + ", ".join(words[:8]) + (" ..." if len(words) > 8 else ""))
    out(HELP)
    while True:
        key = _ask(ask, "  > ").lower()
        if key == "y":
            return {"action": "add", "tag": tag, "category": category_of.get(tag, category),
                    "words": words}
        if key == "n":
            return {"action": "reject"}
        if key == "s":
            return {"action": "skip"}
        if key == "r":
            new = _ask(ask, "  new name: ", tag).lower()
            if vocab_module.TAG_RE.fullmatch(new):
                tag = new
                out(f"  -> {tag}" + ("   (a tag you already have)" if tag in category_of else ""))
            else:
                out("  a tag is lowercase letters and digits, with spaces - / ' inside, 1-48 long")
        elif key == "c":
            answer = _ask(ask, "  1 body_part  2 symptom  3 severity  4 context: ").lower()
            chosen = CATEGORY_KEYS.get(answer, answer if answer in vocab_module.CATEGORIES else None)
            if chosen:
                category = chosen
                out(f"  -> {category}")
            else:
                out("  pick 1, 2, 3 or 4")
        elif key == "m":
            target = _ask(ask, "  merge into which tag: ").lower()
            if target in category_of:
                return {"action": "add", "tag": target, "category": category_of[target],
                        "words": words + [tag]}
            out(f"  {target!r} isn't in the vocabulary; [r] renames instead")
        elif key == "w":
            words = [w.strip() for w in _ask(ask, "  words: ", ", ".join(words)).split(",") if w.strip()]
            out("  -> " + (", ".join(words) or "no words, just the tag"))
        else:
            out(HELP)


def _scalar(text: str) -> str:
    return text if _PLAIN_RE.fullmatch(text) else json.dumps(text)


def render(original: str, additions: list) -> str:
    """vocab.yaml with the additions folded in, keeping its comments, version
    and the order it already has."""
    data = yaml.safe_load(original) or {}
    header = []
    for line in original.splitlines():
        if line.startswith("#") or not line.strip():
            header.append(line)
        else:
            break

    entries = {c: {tag: list(words or []) for tag, words in (data.get(c) or {}).items()}
               for c in vocab_module.CATEGORIES}
    category_of = {tag: c for c in entries for tag in entries[c]}
    for add in additions:
        tag = add["tag"]
        category = category_of.get(tag, add["category"])
        words = entries.setdefault(category, {}).setdefault(tag, [])
        category_of[tag] = category
        have = {w.lower() for w in words} | {tag.lower()}
        for word in add["words"]:
            if word.lower() not in have:
                words.append(word)
                have.add(word.lower())

    lines = list(header) + [f"version: {data.get('version')}"]
    for category in vocab_module.CATEGORIES:
        if not entries[category]:
            continue
        lines += ["", f"{category}:"]
        for tag, words in entries[category].items():
            lines.append(f"  {_scalar(tag)}: [{', '.join(_scalar(w) for w in words)}]")
    return "\n".join(lines) + "\n"


def apply_decisions(vocab_path: str, decided: dict, out=print):
    """Write vocab.yaml from every "add" decision. Returns the new Vocab, or
    None if the result wouldn't load, in which case nothing is written."""
    with open(vocab_path, encoding="utf-8") as f:
        original = f.read()
    text = render(original, [d for d in decided.values() if d.get("action") == "add"])
    try:
        new = vocab_module.parse(text)
    except vocab_module.VocabError as e:
        out(f"vocab.yaml NOT changed: the result wouldn't load ({e})")
        out("your decisions are safe in drafts/decisions.jsonl")
        return None
    if text == original:
        out("vocab.yaml unchanged")
        return new

    old = vocab_module.parse(original)
    # Owner-only, like runs/ and drafts/: a used vocabulary ends up quoting
    # your notes. os.replace keeps the new file's mode.
    shutil.copy2(vocab_path, vocab_path + ".bak")
    os.chmod(vocab_path + ".bak", 0o600)
    temporary = vocab_path + ".new"
    with open(temporary, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(temporary, 0o600)
    os.replace(temporary, vocab_path)
    words = sum(len(w) for w in new.words_for.values())
    out(f"vocab.yaml written: {len(new.tags)} tags and {words} words "
        f"(was {len(old.tags)} and {sum(len(w) for w in old.words_for.values())}). "
        f"The old file is {vocab_path}.bak")
    out("to tag with it: python3 tag_run.py --retag")
    return new


def review(draft_path: str = DRAFT_PATH, vocab_path: str = VOCAB_PATH,
           decisions_path: str = DECISIONS_PATH, min_notes: int = 2,
           ask=terminal_input, out=print):
    """Ask about every undecided suggestion, then write vocab.yaml."""
    with open(draft_path, encoding="utf-8") as f:
        suggestions = parse_draft(f.read())
    vocab = vocab_module.load(vocab_path)
    decided = read_decisions(decisions_path)
    todo = [s for s in suggestions
            if s["notes"] >= min_notes and decided.get(s["tag"], {}).get("action") in (None, "skip")]
    out(f"{len(suggestions)} suggestion(s) in the draft, {len(todo)} to look at "
        f"({_counted(decided, 'add')} added, {_counted(decided, 'reject')} rejected so far)")

    with _private_append(decisions_path) as f:
        try:
            for i, suggestion in enumerate(todo, 1):
                suggestion["where"] = f"[{i}/{len(todo)}] {suggestion['notes']} notes"
                decision = review_one(suggestion, vocab.category_of, ask, out)
                decision["suggested"] = suggestion["tag"]
                f.write(json.dumps(decision) + "\n")
                f.flush()
                decided[suggestion["tag"]] = decision
        except Stop:
            out("stopped: run it again to carry on")
    return apply_decisions(vocab_path, decided, out)


def _counted(decided: dict, action: str) -> int:
    return sum(1 for d in decided.values() if d.get("action") == action)


def summarize(draft_path: str, decisions_path: str, min_notes: int = 2) -> str:
    """Counts only: no tag names, phrases or note text."""
    with open(draft_path, encoding="utf-8") as f:
        suggestions = parse_draft(f.read())
    decided = read_decisions(decisions_path)
    shown = [s for s in suggestions if s["notes"] >= min_notes]
    left = [s for s in shown if decided.get(s["tag"], {}).get("action") in (None, "skip")]
    adds = [d for d in decided.values() if d.get("action") == "add"]
    return "\n".join([
        f"draft: {len(suggestions)} suggestions, {len(shown)} at {min_notes}+ notes",
        f"decided: {len(adds)} added, {_counted(decided, 'reject')} rejected, "
        f"{_counted(decided, 'skip')} skipped",
        f"still to look at: {len(left)}",
        f"words those additions carry: {sum(len(d.get('words') or []) for d in adds)}",
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Review the vocabulary draft and write vocab.yaml.")
    parser.add_argument("command", nargs="?", default="review", choices=["review", "summary"])
    parser.add_argument("--min-notes", type=int, default=2,
                        help="only suggestions that came up in this many notes")
    parser.add_argument("--draft", default=DRAFT_PATH)
    parser.add_argument("--vocab", default=VOCAB_PATH)
    args = parser.parse_args(argv)

    try:
        if args.command == "summary":
            print(summarize(args.draft, DECISIONS_PATH, args.min_notes))
            return 0
        review(args.draft, args.vocab, min_notes=args.min_notes)
        return 0
    except (vocab_module.VocabError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
