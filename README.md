# taggle-rock

Local LLM tagging of private free-text notes.

Say you have 1,300 diary notes like *"ankles absolutely killing me today"*.
Six months later, a search for **joint pain** should find that note, even
though it never used either word. taggle-rock gets you there. A language
model running on your own computer reads each note once, in a nightly batch,
and picks tags from a vocabulary you control. After that, search runs on the
stored tags, which is fast and cheap, and the model is never needed for it.

The Doozers need not know why the Trash Heap wrote the text. They just
organize it.

## The promises

- **The text is the record.** Notes are only ever read. Nothing changes them.
- **Tags are derived metadata.** Every tag records the run that wrote it, the
  model and the vocabulary version, so a bad run can be taken back out in
  one go.
- **The model is not trusted.** Its answer is held to a JSON schema built
  from your vocabulary, then checked again by rules. Its confidence is never
  asked for, because it means nothing.
- **Notes stay on your hardware.** They go only to a model on localhost
  (Ollama). Run logs hold dates, sections and tags, never note text. Note
  text is shown only on the screen where you grade notes by hand, and
  vocabulary drafts quote short phrases. Both are kept in gitignored files
  only you can read.

## The map

```
 your notes (the source)                 this computer: taggle-rock
┌──────────────────────────┐            ┌──────────────────────────────────┐
│ free-text notes          │──read────► │ tag_run.py (nightly timer)       │
│                          │            │  - new or edited notes only      │
│ tags + run id  ◄─────────┼──write─────┤  - model picks from vocab.yaml   │
│                          │            │  - schema + rules check answers  │
│ search by tag            │            │                                  │
└──────────────────────────┘            │ draft_vocab.py: suggests words   │
                                        │ spot_check.py: you grade samples │
          Ollama, localhost only ◄──────┤                                  │
          (qwen3 30B-A3B)               └──────────────────────────────────┘
```

## First real run

The first user is [SardineTracker](https://github.com/alaricmoore/sardinetracker),
a symptom diary for people with systemic autoimmune rheumatic diseases. The
first full run went over the author's own diary on 2026-09-15:

| | |
|---|---|
| Notes | **1,322**, from 12 diary sections, 13 months of entries |
| Failed | 0 (a failed note is skipped and retried by the next run) |
| Answers rejected by the checks | 0 |
| Notes left with no tags | 349 (26%) |
| Tags per note | 2.2 on average |
| Vocabulary | the starter list: 38 tags, 120 words |
| Time | about 54 minutes, about 2.5 s per note |
| Hardware | ThinkPad T14 laptop, Ryzen 5 PRO 8540U, 32 GB RAM, Radeon 740M iGPU through Vulkan |
| Model | qwen3:30b-a3b (Q4_K_M), 16K context, thinking off, temperature 0 |

"Nothing failed" only means nothing crashed. To measure whether the tags
are *right*, the author graded a random sample of 30 notes by hand with
`spot_check.py`:

| | Result | Likely range, with a sample of 30 |
|---|---|---|
| Tags that were right | **55 of 62 (89%)** | about 78–94% |
| Notes missing a tag they should have had | **15 of 30 (50%)** | about 33–67% |
| Missing items already in the vocabulary (the model missed them) | 6 of 26 | |
| Missing items not in the vocabulary (a vocabulary gap) | 20 of 26 | |
| Untagged notes that really had nothing to tag | 0 of 10 | |

Right by category: severity 12/12, context 2/2, body part 9/10,
symptom 32/38.

**What that says:** when the model tags something, it's usually right. It
misses a lot, but mostly because the starter vocabulary had no word for what
the note said. The 26% of notes with no tags aren't correctly empty; they
fell into vocabulary gaps. So the next step is a bigger vocabulary, not a
different model.

**In progress:** `draft_vocab.py` is reading all the notes to suggest
vocabulary. Then comes a `--retag` run with the new vocabulary, and a
re-grade of *the same 30 notes* (`--same-notes-as`) for a fair before and
after. Those numbers will be added here.

## What a tag looks like

These notes are invented. With this in `vocab.yaml`:

```yaml
body_part:
  joints: [knuckles, wrists, knees, ankles]
symptom:
  pain: [aching, sore, killing me]
  fatigue: [tired, wiped out, exhausted]
severity:
  severe: [absolutely, unbearable, worst]
```

| Note | Tags |
|---|---|
| ankles absolutely killing me today | joints, pain, severe |
| wiped out after the grocery run | fatigue |
| no knee pain today for once | *(none: a negated mention isn't tagged)* |
| made soup | *(none: nothing to tag)* |

A search for `joints` or `pain` now finds the first note.

## Could I point it at my own notes?

That's the goal: a journal, field notes, maintenance logs, a research
notebook, years of Markdown. **Not yet, though.** Today the only source is a
SardineTracker instance with its tags API, and that API isn't in the public
SardineTracker yet. Planned next:

- a **folder source**: a directory of Markdown or text files, with tags kept
  in a local SQLite index and a search command
- **profiles**: the prompt wording, categories and section names move out of
  the code (they currently say "health diary"), so a maintenance log gets
  its own
- a walkthrough of the **local model setup**: Ollama with Vulkan on an iGPU,
  thread count, context size, why thinking is off, and measured speeds

## Running it (SardineTracker)

You need Python 3 with PyYAML, [Ollama](https://ollama.com), and a
SardineTracker server with the tags API and a client secret for taggle-rock.

```
ollama pull qwen3:30b-a3b
printf 'FROM qwen3:30b-a3b\nPARAMETER num_ctx 16384\n' > qwen-local.Modelfile
ollama create qwen-local -f qwen-local.Modelfile
```

Put `server`, `client_id`, `secret` and `user_id` in `config.json`
(gitignored), and start your vocabulary from the example:

```
cp vocab.example.yaml vocab.yaml
```

`vocab.yaml` is gitignored too. It fills up with words from your own notes,
so it belongs to you, not to this repo. Then:

```
python3 tag_run.py --dry-run --limit 5 --show   try it: print tags, send nothing
python3 tag_run.py                              tag everything new or edited
python3 draft_vocab.py                          suggest vocabulary from your notes
python3 review_vocab.py                         accept or reject suggestions, writes vocab.yaml
python3 tag_run.py --retag                      after changing vocab.yaml
python3 spot_check.py                           grade a sample by hand (own terminal window)
python3 spot_check.py summary                   the numbers: counts only, safe to share
python3 tag_run.py undo RUN_ID                  take a run back out
```

The full manual: `man -l man/taggle-rock.1`. A nightly systemd user timer is
in `systemd/`.

## Files

```
taggle-rock/
├── tag_run.py          tag new and edited notes; --retag; undo a run
├── draft_vocab.py      suggest vocabulary from your notes, checked by rules
├── review_vocab.py     go through the suggestions and write vocab.yaml
├── spot_check.py       grade a sample by hand; summary numbers
├── qwen.py             prompts, JSON schemas and answer checks (Ollama)
├── vocab.py            load and check vocab.yaml
├── vocab.example.yaml  a starting vocabulary: copy it to vocab.yaml
├── tracker.py          the SardineTracker source: signed requests
├── api_signing.py      HMAC request signing, shared with SardineTracker
├── man/taggle-rock.1   the manual page
├── systemd/            nightly timer (catches up after sleep, mains power only)
├── tests/              63 tests: python3 -m unittest discover -s tests -t .
└── PLAN.md             design notes and decisions, as they happened
```

Gitignored and never committed: `config.json` (the secret), `vocab.yaml`
(your vocabulary, which ends up quoting your notes), `runs/` (run logs and
grades: dates, sections and tags), and `drafts/` (vocabulary drafts and the
model's cached answers).

## License

MIT: see [LICENSE](LICENSE). Point it at whatever notes you like.
