"""
Boober grades the Doozer's tags, so you only have to look where he disagrees.

    python3 boober_grade.py                 calibration first, then up to 400 fresh notes
    python3 boober_grade.py --fresh 100     fewer fresh notes
    python3 boober_grade.py summary         how far Boober agrees with you: counts only

Boober is not trusted until he has been compared with you. So he grades the
calibration set first: every note you graded by hand with spot_check.py,
with exactly the tags you saw, so his marks and yours line up tag for tag.
Then he grades fresh notes nobody has graded, with the tags they have now.
His fresh grades mean something only once `summary` says he agrees with you.

Note text comes from the newest backup of the tracker on this laptop
(~/backups/pi-biotracking), opened read-only. Nothing goes to the Pi, and
the only model asked is Boober, on localhost.

Grades go to runs/boober-calibration.grades.jsonl and
runs/boober-fresh.grades.jsonl, readable only by you, in spot_check.py's
format: dates, boxes, tags, marks and missing tags. No note text, and
Boober's reasoning is not kept. Stop it any time; a rerun carries on.
"""

import argparse
import glob
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request

import qwen
import vocab as vocab_module
from spot_check import RUN_DIR, _private_append, read_jsonl

MODEL = "boober"
# Measured on a made-up note (2026-10-02): low 19 s, medium 37 s, same answer.
# A grader's care is worth the time; the run is overnight anyway.
THINK = "medium"
BACKUP_GLOB = os.path.expanduser("~/backups/pi-biotracking/biotracking-*.db")
CALIBRATION = os.path.join(RUN_DIR, "boober-calibration.grades.jsonl")
FRESH = os.path.join(RUN_DIR, "boober-fresh.grades.jsonl")
DEFAULT_FRESH = 400


def system_prompt(vocab) -> str:
    """The tagging rules Qwen was given, turned round into a grader's job, so
    Boober holds the tags to the same standard they were made under."""
    lines = [
        "You check the tags on short health diary notes written by one person with lupus.",
        "Another model tagged each note from the vocabulary below. Each tag is followed",
        "by words that mean it.",
        "",
        "A tag is right only if words in the note earn it:",
        "- the note uses one of the tag's words, or plainly says the same thing;",
        "- not merely likely, related, or usually true of this illness;",
        "- a negated mention does not earn it: \"no rash today\" is not rash.",
        "A tag that is no longer in the vocabulary is judged by its name alone.",
        "",
        "Mark every tag true (right) or false (wrong). Then list any vocabulary tags",
        "the note earns that are missing. Missing means clearly earned, not possible.",
        "Answer with JSON only.",
        "",
        "Vocabulary (tag: words that mean it):",
    ]
    for category in dict.fromkeys(vocab.category_of.values()):
        lines.append(f"[{category.replace('_', ' ')}]")
        for tag, cat in vocab.category_of.items():
            if cat == category:
                words = ", ".join(vocab.words_for[tag])
                lines.append(f"{tag}: {words}" if words else tag)
    return "\n".join(lines)


def prompt_id(vocab) -> str:
    return hashlib.sha256(system_prompt(vocab).encode("utf-8")).hexdigest()[:8]


def schema(vocab, tags: list) -> dict:
    """One true/false per tag under review, and missing tags from the vocabulary."""
    return {
        "type": "object",
        "properties": {
            "verdicts": {"type": "object",
                         "properties": {t: {"type": "boolean"} for t in tags},
                         "required": list(tags)},
            "missing": {"type": "array", "items": {"type": "string", "enum": vocab.tags}},
        },
        "required": ["verdicts", "missing"],
    }


def _post(body: dict, timeout: int):
    """One request to Ollama on localhost. Returns the answer's text. Raises QwenError."""
    req = urllib.request.Request(qwen.OLLAMA_URL, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        raise qwen.QwenError(f"Ollama said HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise qwen.QwenError(f"could not reach Ollama: {e}") from None
    except ValueError:
        raise qwen.QwenError("Ollama's reply was not JSON") from None
    return ((reply.get("message") if isinstance(reply, dict) else None) or {}).get("content")


def ask(vocab, field: str, text: str, tags: list, think: str = THINK, timeout: int = 1800) -> dict:
    """Boober's marks for one note. Raises qwen.QwenError."""
    user = (f"Box: {qwen.field_label(field)}\nNote:\n{text}\n\n"
            f"Tags to check: {', '.join(tags) if tags else '(none: the note got no tags)'}")
    body = {"model": MODEL, "stream": False, "think": think, "format": schema(vocab, tags),
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system_prompt(vocab)},
                         {"role": "user", "content": user}]}
    content = _post(body, timeout)
    try:
        data = json.loads(content)
        verdicts, missing = data["verdicts"], data["missing"]
    except (TypeError, ValueError, KeyError):
        raise qwen.QwenError(f"answer is not the JSON asked for: {str(content)[:80]!r}") from None
    if set(verdicts) != set(tags) or not all(isinstance(v, bool) for v in verdicts.values()):
        raise qwen.QwenError("answer doesn't mark exactly the tags asked about")
    if not isinstance(missing, list) or any(m not in vocab.category_of for m in missing):
        raise qwen.QwenError("answer has missing tags not in the vocabulary")
    return {"tags": {t: verdicts[t] for t in tags},
            "missing": [m for m in dict.fromkeys(missing) if m not in tags]}


# ------------------------------------------------------------------
# What to grade
# ------------------------------------------------------------------

def newest_backup() -> str:
    paths = sorted(glob.glob(BACKUP_GLOB))
    if not paths:
        raise FileNotFoundError(f"no backups match {BACKUP_GLOB}")
    return paths[-1]


def note_texts(db_path: str, user_id: int, fields: set) -> dict:
    """{(date, field): text} for every non-empty note, read-only."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # Only real columns, and only note boxes: a field name goes into the SQL.
        columns = {r[1] for r in con.execute("PRAGMA table_info(daily_observations)")
                   if r[1] == "notes" or r[1].endswith("_notes")}
        texts = {}
        for field in sorted(fields & columns):
            for day, text in con.execute(
                    f'SELECT date, "{field}" FROM daily_observations WHERE user_id = ?', (user_id,)):
                if text and text.strip():
                    texts[(day, field)] = text
        return texts
    finally:
        con.close()


def human_grades(directory: str = RUN_DIR) -> list:
    """[(run_id, grade row)] from every spot_check.py grades file."""
    out = []
    for path in sorted(glob.glob(os.path.join(directory, "run-*.grades.jsonl"))):
        run_id = os.path.basename(path).removesuffix(".grades.jsonl")
        out += [(run_id, g) for g in read_jsonl(path)]
    return out


def calibration_items(texts: dict, directory: str = RUN_DIR) -> list:
    """Every note you graded, with the tags you graded, if the text is unchanged
    since that run. A note graded in several runs is in here once per run."""
    items, logs = [], {}
    for run_id, g in human_grades(directory):
        if run_id not in logs:
            logs[run_id] = {(r["date"], r["field"]): r
                            for r in read_jsonl(os.path.join(directory, f"{run_id}.jsonl"))}
        row = logs[run_id].get((g["date"], g["field"]))
        text = texts.get((g["date"], g["field"]))
        if not row or text is None or hashlib.sha256(text.encode("utf-8")).hexdigest() != row.get("sha256"):
            continue
        items.append({"date": g["date"], "field": g["field"], "tags": list(g["tags"]), "source": run_id})
    return items


def fresh_items(db_path: str, texts: dict, user_id: int, skip_fields: set, size: int,
                directory: str = RUN_DIR) -> list:
    """A fixed random sample of notes nobody has graded, with the tags they have
    in the backup, if those tags were made from the text as it is now."""
    graded = {(g["date"], g["field"]) for _, g in human_grades(directory)}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tagged = con.execute("SELECT date, field, note_sha256 FROM tagged_notes WHERE user_id = ?",
                             (user_id,)).fetchall()
        tags = {}
        for day, field, tag in con.execute(
                "SELECT date, field, tag FROM note_tags WHERE user_id = ? ORDER BY tag", (user_id,)):
            tags.setdefault((day, field), []).append(tag)
    finally:
        con.close()
    source = "db:" + os.path.basename(db_path)
    items = []
    for day, field, sha in sorted(tagged):
        text = texts.get((day, field))
        if field in skip_fields or (day, field) in graded or text is None \
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != sha:
            continue
        items.append({"date": day, "field": field, "tags": tags.get((day, field), []), "source": source})
    random.Random("boober-fresh").shuffle(items)
    return items[:size]


def key(item: dict) -> tuple:
    return item["date"], item["field"], item["source"]


def grade_all(vocab, items: list, texts: dict, path: str, label: str, think: str) -> None:
    done = {key(g) for g in read_jsonl(path)} if os.path.exists(path) else set()
    todo = [i for i in items if key(i) not in done]
    print(f"{label}: {len(items)} notes, {len(items) - len(todo)} already graded", flush=True)
    pid = prompt_id(vocab)
    with _private_append(path) as f:
        for n, item in enumerate(todo, 1):
            where = f"[{n}/{len(todo)}] {label} {item['date']} {qwen.field_label(item['field'])}"
            start = time.monotonic()
            for attempt in (1, 2):
                try:
                    marks = ask(vocab, item["field"], texts[(item["date"], item["field"])], item["tags"], think)
                    break
                except qwen.QwenError as e:
                    print(f"{where}: attempt {attempt} failed: {e}", flush=True)
            else:
                continue
            seconds = round(time.monotonic() - start, 1)
            f.write(json.dumps({**item, **marks, "model": MODEL, "think": think,
                                "prompt": pid, "seconds": seconds}) + "\n")
            f.flush()
            print(f"{where}: {len(item['tags'])} tags, {seconds}s", flush=True)


# ------------------------------------------------------------------
# How far Boober agrees with you
# ------------------------------------------------------------------

def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def summarize(vocab, directory: str = RUN_DIR) -> str:
    """Counts only: no note text, no tag names, nothing you typed."""
    path = os.path.join(directory, os.path.basename(CALIBRATION))
    boober = {key(g): g for g in read_jsonl(path)} if os.path.exists(path) else {}
    both_right = both_wrong = you_only = boober_only = 0
    notes = missing_agree = 0
    for run_id, g in human_grades(directory):
        b = boober.get((g["date"], g["field"], run_id))
        if not b:
            continue
        notes += 1
        for tag, yours in g["tags"].items():
            his = b["tags"].get(tag)
            both_right += yours and his
            both_wrong += (not yours) and (his is False)
            you_only += yours and his is False
            boober_only += (not yours) and bool(his)
        missing_agree += bool(g["missing"]) == bool(b["missing"])
    judged = both_right + both_wrong + you_only + boober_only
    agree = both_right + both_wrong
    # Cohen's kappa: agreement beyond what two graders guessing at their own
    # rates would reach by chance. 1 is perfect; 0 is no better than chance.
    if judged:
        p_you, p_him = (both_right + you_only) / judged, (both_right + boober_only) / judged
        chance = p_you * p_him + (1 - p_you) * (1 - p_him)
        kappa = f"{(agree / judged - chance) / (1 - chance):.2f}" if chance < 1 else "-"
    else:
        kappa = "-"
    fresh_path = os.path.join(directory, os.path.basename(FRESH))
    fresh = read_jsonl(fresh_path) if os.path.exists(fresh_path) else []
    fresh_marks = [ok for g in fresh for ok in g["tags"].values()]
    return "\n".join([
        f"calibration: {notes} notes graded by both of you, {judged} tags",
        f"  agree on {agree} of {judged} tags ({_pct(agree, judged)}), kappa {kappa}",
        f"  both say right {both_right}, both say wrong {both_wrong}",
        f"  you say right, Boober wrong {you_only}; you say wrong, Boober right {boober_only}",
        f"  agree whether anything is missing: {missing_agree} of {notes} notes ({_pct(missing_agree, notes)})",
        f"fresh: {len(fresh)} notes, Boober marks {sum(fresh_marks)} of {len(fresh_marks)} tags right "
        f"({_pct(sum(fresh_marks), len(fresh_marks))}), {sum(1 for g in fresh if g['missing'])} notes missing something",
        "  (trust the fresh numbers only as far as calibration says)",
    ])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Boober grades the Doozer's tags.")
    parser.add_argument("command", nargs="?", default="grade", choices=["grade", "summary"])
    parser.add_argument("--fresh", type=int, default=DEFAULT_FRESH, help="fresh notes after calibration")
    parser.add_argument("--think", default=THINK, choices=["low", "medium", "high"])
    parser.add_argument("--db", help="tracker database to read (default: the newest backup)")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--vocab", default="vocab.yaml")
    args = parser.parse_args(argv)
    try:
        vocab = vocab_module.load(args.vocab)
        if args.command == "summary":
            print(summarize(vocab))
            return 0
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
        db_path = args.db or newest_backup()
        fields = {g["field"] for _, g in human_grades()} | set(qwen_fields(db_path))
        texts = note_texts(db_path, config["user_id"], fields)
        print(f"reading {db_path}, prompt {prompt_id(vocab)}, think {args.think}", flush=True)
        grade_all(vocab, calibration_items(texts), texts, CALIBRATION, "calibration", args.think)
        grade_all(vocab, fresh_items(db_path, texts, config["user_id"], set(config.get("skip_fields", [])),
                                     args.fresh), texts, FRESH, "fresh", args.think)
        print(summarize(vocab), flush=True)
        return 0
    except (vocab_module.VocabError, OSError, sqlite3.Error, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def qwen_fields(db_path: str) -> list:
    """The note boxes that have ever been tagged."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT field FROM tagged_notes")]
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
