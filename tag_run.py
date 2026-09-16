"""
Tag the tracker's notes with Qwen.

    python3 tag_run.py                      tag every note that is new or changed
    python3 tag_run.py --limit 10           just the first 10 of those
    python3 tag_run.py --since 2026-01-01   only notes from that date on
    python3 tag_run.py --retag              tag again every note not yet tagged with this
                                            vocabulary (after changing vocab.yaml)
    python3 tag_run.py --trial --model NAME --notes-from RUN_ID
                                            try another model on the notes you graded in
                                            RUN_ID: writes a run log, sends nothing
    python3 tag_run.py --dry-run --limit 5 --show
                                            ask Qwen and print the tags, send nothing
    python3 tag_run.py undo RUN_ID          remove everything one run tagged
    python3 tag_run.py clear --yes          empty the tags of the boxes config.json skips

Note text is never printed or saved here: only dates, boxes and tags. Tags go
to the tracker in batches, so stopping a run (Ctrl-C) keeps what it had done.
Each run writes runs/<run_id>.jsonl on this laptop (gitignored): the date,
box, tags and the tracker's result for every note.
"""

import argparse
import glob
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


def notes_graded_in(log_dir: str, run_id: str) -> set:
    """(date, field) of the notes you graded for a run, for --notes-from."""
    path = os.path.join(log_dir, f"{run_id}.grades.jsonl")
    keys = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
                keys.add((entry["date"], entry["field"]))
            except (ValueError, KeyError, TypeError):
                continue   # a line cut off by a stopped run
    return keys


def tagged_with(log_dir: str, vocab_version: str) -> set:
    """(date, field, sha256) of every note a run log says was tagged with this
    vocabulary. Lets --retag carry on after Ctrl-C instead of starting over.
    Logs from before the version and hash were recorded count for nothing."""
    done = set()
    for path in glob.glob(os.path.join(log_dir, "run-*.jsonl")):
        if path.endswith(".grades.jsonl"):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    if e.get("result") == "tagged" and e.get("vocab") == vocab_version and e.get("sha256"):
                        done.add((e["date"], e["field"], e["sha256"]))
                except (ValueError, KeyError, AttributeError):
                    continue   # a line cut off by a stopped run
    return done


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
        log_dir="runs", out=print, run_id=None, retag=False, trial=False,
        model=None, notes_from=None, skip_fields=None):
    """Tag what needs tagging. Returns a summary dict (also printed).

    With `retag`, every note not already tagged with this vocabulary is asked
    about, tagged or not. The tracker replaces a note's tags when the new ones
    arrive, so search keeps the old tags until then.

    With `trial`, the answers go to the run log and nowhere else: nothing is
    sent, so the tracker's tags are left alone. `model` asks a different model
    (the log records which), and `notes_from` limits the run to the notes you
    graded for an earlier run. Together they compare two models on the same
    notes: spot_check.py grades a trial log like any other run."""
    run_id = run_id or new_run_id()
    named_model = model is not None   # asked for by name, so pass it on
    model = model or qwen.MODEL
    notes = tracker.notes(since=since)
    skip = tuple(skip_fields or ())
    if skip:
        before = len(notes)
        notes = [n for n in notes if n["field"] not in skip]
        skipped_boxes = before - len(notes)
    if notes_from:
        # You named the notes, so they are the run: asking whether they have
        # been tagged already would leave nothing to do.
        wanted = notes_graded_in(log_dir, notes_from)
        todo = [n for n in notes if (n["date"], n["field"]) in wanted]
    elif retag:
        done = tagged_with(log_dir, vocab.version)
        todo = [n for n in notes if (n["date"], n["field"], n["sha256"]) not in done]
    else:
        todo = needs_tags(notes)
    skipped = len(notes) - len(todo)
    if limit:
        todo = todo[:limit]
    counts = {"answered": 0, "qwen_failed": 0, "tagged": 0, "trial": 0, "stale": 0,
              "missing": 0, "invalid": 0}
    out(f"{run_id}: {len(todo)} note(s) to tag, vocabulary {vocab.version}, model {model},"
        f" prompt {qwen.prompt_id(vocab)}"
        + (f" ({skipped_boxes} note(s) in {', '.join(skip)} skipped)" if skip else "")
        + (f" (from the notes graded in {notes_from})" if notes_from else "")
        + (f" (retag: {skipped} already done with this vocabulary)" if retag else "")
        + (" (trial: the log only, nothing is sent)" if trial else "")
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
        if trial:
            # Written down, not sent: the tracker's tags are left as they are.
            for sent in pending:
                counts["trial"] += 1
                log.write(date=sent["date"], field=sent["field"], sha256=sent["note_sha256"],
                          vocab=vocab.version, model=model, prompt=qwen.prompt_id(vocab),
                          tags=[t["tag"] for t in sent["tags"]], result="trial", error=None)
            return
        reply = tracker.post_tags(run_id, model, vocab.version, pending)
        for sent, result in zip(pending, reply["results"]):
            counts[result["result"]] += 1
            log.write(date=sent["date"], field=sent["field"], sha256=sent["note_sha256"],
                      vocab=vocab.version, model=model, prompt=qwen.prompt_id(vocab),
                      tags=[t["tag"] for t in sent["tags"]],
                      result=result["result"], error=result.get("error"))

    try:
        for i, note in enumerate(todo, 1):
            where = f"[{i}/{len(todo)}] {note['date']} {qwen.field_label(note['field'])}"
            try:
                tags = ask(vocab, note["field"], note["text"], model=model) if named_model \
                    else ask(vocab, note["field"], note["text"])
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
    if trial and counts["trial"]:
        out(f"trial only: nothing was sent. To grade it: python3 spot_check.py --run {run_id}"
            + (f" --same-notes-as {notes_from}" if notes_from else ""))
    if not dry_run and counts["tagged"]:
        out(f"to undo this run: python3 tag_run.py undo {run_id}"
            + (" (removes its tags; the earlier ones don't come back)" if retag else ""))
    return summary


def clear(tracker, vocab, fields, log_dir="runs", out=print, run_id=None, model=None):
    """Give every note in `fields` an empty set of tags, so what they had
    leaves search. For a box whose tags are noise: one with no subject of its
    own collects whatever the model can reach.

    This is not an undo. The tags are replaced by nothing, and `undo` on this
    run only unmarks the notes; it cannot bring back what they had."""
    run_id = run_id or new_run_id()
    model = model or qwen.MODEL
    todo = [n for n in tracker.notes() if n["field"] in fields]
    counts = {"cleared": 0, "stale": 0, "missing": 0, "invalid": 0}
    out(f"{run_id}: clearing the tags of {len(todo)} note(s) in {', '.join(fields)}")

    log = RunLog(log_dir, run_id)
    try:
        for start in range(0, len(todo), BATCH_SIZE):
            pending = [{"date": n["date"], "field": n["field"], "note_sha256": n["sha256"], "tags": []}
                       for n in todo[start:start + BATCH_SIZE]]
            reply = tracker.post_tags(run_id, model, vocab.version, pending)
            for sent, result in zip(pending, reply["results"]):
                got = result["result"]
                counts["cleared" if got == "tagged" else got] += 1
                log.write(date=sent["date"], field=sent["field"], sha256=sent["note_sha256"],
                          vocab=vocab.version, model=model, tags=[], result=got,
                          cleared=True, error=result.get("error"))
    finally:
        log.close()

    summary = {"run_id": run_id, "notes": len(todo), **counts}
    out(", ".join(f"{k} {v}" for k, v in summary.items() if k != "run_id"))
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Tag the tracker's notes with Qwen.")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "undo", "clear"])
    parser.add_argument("run_id", nargs="?", help="for undo: the run to remove")
    parser.add_argument("--limit", type=int, help="tag at most this many notes")
    parser.add_argument("--since", help="only notes from this date (YYYY-MM-DD) on")
    parser.add_argument("--dry-run", action="store_true", help="ask Qwen but send nothing")
    parser.add_argument("--show", action="store_true", help="print the tags for each note")
    parser.add_argument("--retag", action="store_true",
                        help="tag again every note not yet tagged with this vocabulary")
    parser.add_argument("--trial", action="store_true",
                        help="write the run log but send nothing, to compare models")
    parser.add_argument("--model", help=f"the Ollama model to ask (default {qwen.MODEL})")
    parser.add_argument("--notes-from", metavar="RUN_ID",
                        help="only the notes you graded for that run")
    parser.add_argument("--yes", action="store_true",
                        help="for clear: yes, empty those tags (they don't come back)")
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
        if args.command == "clear":
            if not tracker.skip_fields:
                parser.error('nothing to clear: config.json has no "skip_fields"')
            if not args.yes:
                parser.error(f"clear empties the tags of {', '.join(tracker.skip_fields)} and they "
                             "don't come back; say --yes if that's what you want")
            clear(tracker, vocab, tracker.skip_fields, model=args.model)
            return 0
        run(tracker, vocab, limit=args.limit, since=args.since, dry_run=args.dry_run, show=args.show,
            retag=args.retag, trial=args.trial, model=args.model, notes_from=args.notes_from,
            skip_fields=tracker.skip_fields)
        return 0
    except (TrackerError, vocab_module.VocabError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
