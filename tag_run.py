"""
Tag the tracker's notes with Qwen.

    python3 tag_run.py                      tag every note that is new or changed
    python3 tag_run.py --limit 10           just the first 10 of those
    python3 tag_run.py --since 2026-01-01   only notes from that date on
    python3 tag_run.py --dry-run --limit 5 --show
                                            ask Qwen and print the tags, send nothing
    python3 tag_run.py undo RUN_ID          remove everything one run tagged

Note text is never printed or saved here: only dates, boxes and tags. Tags go
to the tracker in batches, so stopping a run (Ctrl-C) keeps what it had done.
Each run writes runs/<run_id>.jsonl on this laptop (gitignored): the date,
box, tags and the tracker's result for every note.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

import qwen
import vocab as vocab_module
from tracker import Tracker, TrackerError

BATCH_SIZE = 20


def new_run_id(now: datetime = None) -> str:
    return "run-" + (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def needs_tags(notes: list) -> list:
    """Notes never tagged, or edited since they were."""
    return [n for n in notes if n["sha256"] != n["tagged_sha256"]]


class RunLog:
    """runs/<run_id>.jsonl: one line per note. Readable only by you: tags are health data."""

    def __init__(self, directory: str, run_id: str):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, f"{run_id}.jsonl")
        self._f = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "a")
        self.path = path

    def write(self, **entry):
        self._f.write(json.dumps(entry) + "\n")
        self._f.flush()

    def close(self):
        self._f.close()


def run(tracker, vocab, ask=qwen.ask, limit=None, since=None, dry_run=False, show=False,
        log_dir="runs", out=print, run_id=None):
    """Tag what needs tagging. Returns a summary dict (also printed)."""
    run_id = run_id or new_run_id()
    todo = needs_tags(tracker.notes(since=since))
    if limit:
        todo = todo[:limit]
    counts = {"answered": 0, "qwen_failed": 0, "tagged": 0, "stale": 0, "missing": 0, "invalid": 0}
    out(f"{run_id}: {len(todo)} note(s) to tag, vocabulary {vocab.version}"
        + (" (dry run: nothing is sent)" if dry_run else ""))

    log = None if dry_run else RunLog(log_dir, run_id)
    batch = []
    started = time.monotonic()

    def send():
        # Taken out of the batch before sending, so a failed send isn't tried
        # again on the way out. Its notes are still untagged, so the next run
        # picks them up.
        pending = batch[:]
        batch.clear()
        if not pending or dry_run:
            return
        reply = tracker.post_tags(run_id, qwen.MODEL, vocab.version, pending)
        for sent, result in zip(pending, reply["results"]):
            counts[result["result"]] += 1
            log.write(date=sent["date"], field=sent["field"],
                      tags=[t["tag"] for t in sent["tags"]], result=result["result"],
                      error=result.get("error"))

    try:
        for i, note in enumerate(todo, 1):
            where = f"[{i}/{len(todo)}] {note['date']} {qwen.field_label(note['field'])}"
            try:
                tags = ask(vocab, note["field"], note["text"])
            except qwen.QwenError as e:
                counts["qwen_failed"] += 1
                out(f"{where}: skipped, {e}")
                if log:
                    log.write(date=note["date"], field=note["field"], result="qwen_failed", error=str(e))
                continue
            counts["answered"] += 1
            out(f"{where}: " + (", ".join(t for t, _ in tags) or "no tags") if show
                else f"{where}: {len(tags)} tag(s)")
            batch.append({"date": note["date"], "field": note["field"], "note_sha256": note["sha256"],
                          "tags": [{"tag": t, "category": c} for t, c in tags]})
            if len(batch) >= BATCH_SIZE:
                send()
    except KeyboardInterrupt:
        out("stopped: sending what was done so far")
    finally:
        try:
            send()
        finally:
            if log:
                log.close()

    summary = {"run_id": run_id, "notes": len(todo), "seconds": round(time.monotonic() - started), **counts}
    out(("dry run: " if dry_run else "") + ", ".join(f"{k} {v}" for k, v in summary.items() if k != "run_id"))
    if not dry_run and counts["tagged"]:
        out(f"to undo this run: python3 tag_run.py undo {run_id}")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tag the tracker's notes with Qwen.")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "undo"])
    parser.add_argument("run_id", nargs="?", help="for undo: the run to remove")
    parser.add_argument("--limit", type=int, help="tag at most this many notes")
    parser.add_argument("--since", help="only notes from this date (YYYY-MM-DD) on")
    parser.add_argument("--dry-run", action="store_true", help="ask Qwen but send nothing")
    parser.add_argument("--show", action="store_true", help="print the tags for each note")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--vocab", default="vocab.yaml")
    args = parser.parse_args(argv)

    try:
        tracker = Tracker.from_config(args.config)
        if args.command == "undo":
            if not args.run_id:
                parser.error("undo needs the run id, e.g. undo run-20260914-210000")
            print(f"{args.run_id}: removed tags from {tracker.undo(args.run_id)} note(s)")
            return 0
        vocab = vocab_module.load(args.vocab)
        run(tracker, vocab, limit=args.limit, since=args.since, dry_run=args.dry_run, show=args.show)
        return 0
    except (TrackerError, vocab_module.VocabError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
