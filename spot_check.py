"""
Grade a sample of a tag run by hand, to measure how right Qwen's tags are.

    python3 spot_check.py                     grade 30 random notes from the latest run
    python3 spot_check.py --sample 50         grade more (the first 30 are kept)
    python3 spot_check.py --run RUN_ID        grade a particular run
    python3 spot_check.py --run NEW_RUN --same-notes-as OLD_RUN
                                              grade the notes you graded for OLD_RUN, as
                                              NEW_RUN tagged them: a fair before/after
    python3 spot_check.py summary             the numbers so far: counts only, no note text

Run it in its own terminal window, not with "!" inside Claude Code: it shows
you note text, and "!" output goes into the conversation.

For each note you see its box, date, text and the tags it got. Mark each tag
right (y) or wrong (n), then type any tags or words that should have been
there, or press Enter. q stops; run it again to carry on where you left off.
The sample is the same every time for a run, so stopping loses nothing.

Grades go to runs/<run_id>.grades.jsonl, readable only by you: dates, boxes,
tags, your marks and what you typed as missing. The note text is not saved.
A note edited since the run is skipped, since its tags may no longer fit.
"""

import argparse
import glob
import json
import os
import random
import sys
import textwrap

import qwen
import vocab as vocab_module
from tracker import Tracker, TrackerError

RUN_DIR = "runs"
DEFAULT_SAMPLE = 30


class Stop(Exception):
    """You typed q (or the input ran out)."""


def norm(text: str) -> str:
    return " ".join(str(text).lower().split())


def read_jsonl(path: str) -> list:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue   # a line cut off by a stopped run
    return rows


def latest_run(directory: str = RUN_DIR) -> str:
    """The newest run log that has notes in it."""
    paths = [p for p in glob.glob(os.path.join(directory, "run-*.jsonl"))
             if not p.endswith(".grades.jsonl") and os.path.getsize(p)]
    if not paths:
        raise FileNotFoundError(f"no run logs with notes in {directory}/")
    return os.path.basename(max(paths)).removesuffix(".jsonl")


def sampled(log_rows: list, seed: str) -> list:
    """The run's tagged notes in a shuffled order fixed by `seed`, so a rerun
    grades the same sample and a bigger --sample extends it."""
    rows = [r for r in log_rows if r.get("result") in ("tagged", "trial")]
    random.Random(seed).shuffle(rows)
    return rows


def _private_append(path: str):
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    f = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a", encoding="utf-8")
    os.chmod(path, 0o600)
    return f


def _ask(ask_input, prompt: str) -> str:
    try:
        answer = ask_input(prompt).strip()
    except EOFError:
        raise Stop from None
    if answer.lower() == "q":
        raise Stop
    return answer


def _yes_no(ask_input, prompt: str) -> bool:
    while True:
        answer = _ask(ask_input, prompt).lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def grade_note(row: dict, text: str, where: str, ask_input, out) -> dict:
    """Show one note and its tags; return your marks. Raises Stop."""
    out("")
    out(where)
    for paragraph in text.splitlines() or [""]:
        out(textwrap.indent(textwrap.fill(paragraph, 72) or "", "    ") or "")
    tags = row["tags"]
    out("  tags: " + (", ".join(tags) or "none"))
    width = max(map(len, tags), default=0)
    marks = {tag: _yes_no(ask_input, f"  {tag.ljust(width)}  right? [y/n, q stops] ") for tag in tags}
    answer = _ask(ask_input, "  missing, comma-separated (Enter for none): ")
    missing = [m for m in (norm(x) for x in answer.split(",")) if m]
    return {"date": row["date"], "field": row["field"], "tags": marks, "missing": missing}


def same_notes(log_rows: list, earlier_grades: list, out=print) -> list:
    """This run's rows for the notes graded in an earlier run, in the order
    they were graded there. A note this run didn't tag is left out and said."""
    rows = {(r["date"], r["field"]): r for r in log_rows if r.get("result") in ("tagged", "trial")}
    picked = []
    for g in earlier_grades:
        row = rows.get((g["date"], g["field"]))
        if row:
            picked.append(row)
        else:
            out(f"{g['date']} {qwen.field_label(g['field'])}: not tagged in this run, left out")
    return picked


def grade(tracker, vocab, run_id: str, size: int = DEFAULT_SAMPLE, log_dir: str = RUN_DIR,
          ask_input=input, out=print, same_notes_as: str = None) -> list:
    """Grade until `size` notes of the sample are done, or you stop. Returns all grades.

    With `same_notes_as`, the sample is the notes graded for that earlier run
    instead, and `size` is how many of them there are."""
    log_path = os.path.join(log_dir, f"{run_id}.jsonl")
    grades_path = os.path.join(log_dir, f"{run_id}.grades.jsonl")
    log_rows = read_jsonl(log_path)
    if same_notes_as:
        earlier = read_jsonl(os.path.join(log_dir, f"{same_notes_as}.grades.jsonl"))
        candidates = same_notes(log_rows, earlier, out)
        size = len(candidates)
    else:
        candidates = sampled(log_rows, seed=run_id)
    grades = read_jsonl(grades_path) if os.path.exists(grades_path) else []
    done = {(g["date"], g["field"]) for g in grades}
    notes = {(n["date"], n["field"]): n for n in tracker.notes()}
    out(f"{run_id}: {len(done)} of {size} graded so far"
        + (f" (the notes graded for {same_notes_as})" if same_notes_as else ""))

    with _private_append(grades_path) as f:
        try:
            for row in candidates:
                if len(grades) >= size:
                    break
                key = (row["date"], row["field"])
                if key in done:
                    continue
                note = notes.get(key)
                where = f"[{len(grades) + 1}/{size}] {row['date']} {qwen.field_label(row['field'])}"
                if not note or note["sha256"] != note["tagged_sha256"]:
                    out(f"{where}: skipped, edited or deleted since the run")
                    continue
                entry = grade_note(row, note["text"], where, ask_input, out)
                f.write(json.dumps(entry) + "\n")
                f.flush()
                grades.append(entry)
                done.add(key)
        except Stop:
            out("stopped: run it again to carry on")

    out("")
    out(summarize(vocab, log_rows, grades))
    return grades


def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def summarize(vocab, log_rows: list, grades: list) -> str:
    """The run's numbers and the grading so far. Counts only: no note text,
    no tag names, nothing you typed."""
    tagged = [r for r in log_rows if r.get("result") in ("tagged", "trial")]
    failed = sum(1 for r in log_rows if r.get("result") == "qwen_failed")
    untagged = sum(1 for r in tagged if not r.get("tags"))
    tag_count = sum(len(r.get("tags") or []) for r in tagged)

    judged = [(tag, ok) for g in grades for tag, ok in g["tags"].items()]
    right = sum(ok for _, ok in judged)
    with_missing = sum(1 for g in grades if g["missing"])
    missing = [m for g in grades for m in g["missing"]]
    known = set(vocab.tags) | {norm(w) for words in vocab.words_for.values() for w in words}
    known_missing = sum(1 for m in missing if m in known)
    empty = [g for g in grades if not g["tags"]]

    lines = [
        f"run: {len(log_rows)} notes, {len(tagged)} tagged, {failed} failed, "
        f"{untagged} with no tags ({_pct(untagged, len(tagged))}), "
        f"{tag_count / len(tagged) if tagged else 0:.1f} tags per note",
        f"graded: {len(grades)} notes",
        f"tags marked right: {right} of {len(judged)} ({_pct(right, len(judged))})",
        f"notes missing something: {with_missing} of {len(grades)} ({_pct(with_missing, len(grades))})",
        f"  missing items: {len(missing)}, {known_missing} already in the vocabulary (Qwen missed it), "
        f"{len(missing) - known_missing} not (a vocabulary gap)",
        f"notes with no tags: {len(empty)}, rightly empty {sum(1 for g in empty if not g['missing'])}",
    ]
    by_category = {}
    for tag, ok in judged:
        counts = by_category.setdefault(vocab.category_of.get(tag, "no longer in vocabulary"), [0, 0])
        counts[0] += ok
        counts[1] += 1
    if by_category:
        lines.append("right by category: " + ", ".join(
            f"{category} {r}/{n}" for category, (r, n) in by_category.items()))
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Grade a sample of a tag run by hand.")
    parser.add_argument("command", nargs="?", default="grade", choices=["grade", "summary"])
    parser.add_argument("--run", help="the run to grade (default: the latest one with notes)")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="how many notes to grade")
    parser.add_argument("--same-notes-as", metavar="RUN_ID",
                        help="grade the notes graded for that earlier run (ignores --sample)")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--vocab", default="vocab.yaml")
    args = parser.parse_args(argv)

    try:
        run_id = args.run or latest_run()
        vocab = vocab_module.load(args.vocab)
        if args.command == "summary":
            grades_path = os.path.join(RUN_DIR, f"{run_id}.grades.jsonl")
            grades = read_jsonl(grades_path) if os.path.exists(grades_path) else []
            print(f"{run_id}\n" + summarize(vocab, read_jsonl(os.path.join(RUN_DIR, f"{run_id}.jsonl")), grades))
            return 0
        if args.same_notes_as and args.same_notes_as == run_id:
            parser.error("--same-notes-as needs a different run than the one being graded")
        grade(Tracker.from_config(args.config), vocab, run_id, size=args.sample,
              same_notes_as=args.same_notes_as)
        return 0
    except (TrackerError, vocab_module.VocabError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
