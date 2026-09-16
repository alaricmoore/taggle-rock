# taggle-rock

Qwen, the local doozer, reads the free-text symptom notes, tags them so the
tracker's search can answer questions like "when did I first report swollen
lymph nodes?", and writes a short weekly recap of what's trending.

Everything health-related is processed on the laptop. Only tags go back to
the Pi, and only a short plain-text recap leaves your hardware, as an email
through Cloudflare.

## The map

```
 Pi: private-track app                 laptop: taggle-rock (Qwen)
┌────────────────────────────┐        ┌─────────────────────────────────┐
│ daily_observations (notes) │─read──►│ tag run (nightly, or whenever   │
│                            │ scope: │  the laptop wakes up)           │
│ note_tags  (new table)  ◄──┼─write──┤  - new or changed notes only    │
│                            │ scope: │  - Qwen picks tags from         │
│ /search  ── tags + dates   │ tags   │    vocab.yaml (your list)       │
│                            │        │  - posts the tags, signed       │
│ weekly stats (SQL)      ───┼─read──►│ recap run (weekly)              │
└────────────────────────────┘ scope: │  - SQL numbers -> Qwen prose    │
                               recap  │  - posts the text, signed ──────┼─┐
                                      └─────────────────────────────────┘ │
                        Cloudflare Email Worker ◄─────────────────────────┘
                        (checks the signature, sends only to your
                         verified address)  ──► plain-text email
```

Every arrow is an HMAC-signed request from the auth-hardening work. Qwen gets
his own secret and a permit that covers only reading notes, writing tags, and
reading recap stats. He cannot change notes, scores, medications or anything
else.

## Decisions (2026-09-14)

- **Auth hardening comes first**, and taggle-rock is built on it: HMAC-signed
  requests, a secret per client, and a permit (scope) per client.
- **Tags appear automatically.** No approval step, so search finds them right
  away. Every tag records which run wrote it, so a bad run can be undone in
  one go. If automatic proves a bad idea, switch to review.
- **The recap goes by email through Cloudflare.** Short and not detail-heavy.
  The Worker only sends to your own verified address.
- **Name: taggle-rock.**

## Phases

### 0. Auth hardening (private-track, branch `auth-hardening`)

Split so that only the server work blocks tags. Details and the wire format
are in private-track `notes/auth-hardening.md`.

```
0a  server: signed requests, per-client secrets, permits, user binding, tests  ◄── blocks tags
    ├──► 1. Tags (Qwen) can start once 0a is merged
0b  wearable firmware: pinned root CAs instead of setInsecure(), and signing
    (code changed now; reflash later, when the new case is ready)
0c  move clients over: clinic-triage → iOS → Android → wearable reflash;
    retire the iOS Shortcut docs
0d  stop accepting bearer tokens
```

- **0a, server.** One `@require_client("permit")` decorator replaces the
  copy-pasted token checks, and marks the endpoint as authenticating itself, so
  the login allowlist can no longer be forgotten.
  - Each client in config has its own secret, its permits, and the user ids
    it may act for.
  - The signature covers method, path, **sorted query string**, client id,
    timestamp, **nonce** and body hash. Without the query string, a captured
    `?user_id=1` request could be changed to `?user_id=2`. Without a nonce, a
    captured request could be replayed inside the 5-minute window.
  - 401 for a bad or missing signature; 403 for a valid client without the
    permit, or acting for a user it isn't bound to.
  - Bearer tokens keep working, mapped to the permits they open today.
- **Permits:** health_sync, flare_status, uv_ingest, backup, notes_read,
  tags_write, recap_read, plus **clinicians_read** and **documents_write** for
  clinic-triage (a caller the first notes missed).
- **0b, wearable:** checks the server against a short list of root CAs
  (Cloudflare's certificate authorities) and signs with a boot-id and
  millisecond counter, since it has no clock.
- Tests: good signatures accepted; wrong secret, altered body, altered query,
  stale time, replayed nonce, replayed counter, missing permit and wrong user
  rejected.

### 1. Tags
- App: a `note_tags` table (date, note field, tag, run id, model, vocabulary
  version), read and write endpoints behind the notes_read and tags_write
  permits, tags in /search with dates, and "undo run".
- `vocab.yaml`: the canonical tags and their synonyms, which you control
  ("glands", "nodes", "neck lumps" become lymph nodes). Categories: body part,
  symptom, severity, context (menstruation, steroids, sun, stress...).
- Qwen output is constrained to that vocabulary with a JSON schema. Spot-check
  a sample of the first full run before trusting it; Qwen's own confidence
  score means nothing.
- Laptop: a systemd user timer with Persistent=true, so a missed run happens
  at the next wake.
- Tracker side (private-track branch `tags`, done 2026-09-14): note fields in
  one list, tag tables, the signed notes/tags/undo API, and search by tag.
  Details: private-track `notes/tags.md`.
- Laptop side (done 2026-09-14): `tag_run.py` with `vocab.py`, `qwen.py`,
  `tracker.py`, and 34 tests (`python3 -m unittest discover -s tests -t .`).
  Runs on the system python3 (needs only PyYAML, already installed).

  ```
  python3 tag_run.py --dry-run --limit 5 --show   try it: print tags, send nothing
  python3 tag_run.py --limit 20                   a first real batch to spot-check
  python3 tag_run.py                              everything new or edited
  python3 tag_run.py --retag                      after a vocab.yaml change: every note not yet
                                                  tagged with it (old tags stay until replaced)
  python3 tag_run.py undo run-YYYYMMDD-HHMMSS     take a run back out
  ```

  **Before a real run, the Pi must run the `tags` branch**; `auth-hardening`
  has no `/api/notes`.

  Measured on made-up notes (2026-09-14), thinking off, 38-tag vocabulary:
  the first note 12 s (reading the instructions once), then 2-4 s each, because
  Ollama reuses the unchanged instructions. About 20-30 minutes for ~430 notes the
  first time; seconds per night after. Correct on a negation ("no rash today":
  no tags), a note with nothing to tag, and synonyms ("glands", "wiped out").
- Vocabulary drafting (done 2026-09-14): `draft_vocab.py` has Qwen read every
  note on the laptop and suggest phrases and tags, then writes
  `drafts/vocab_draft.yaml` (gitignored, owner-only, never printed) for you to
  edit and copy into vocab.yaml. vocab.yaml itself is never changed.

  ```
  python3 draft_vocab.py --limit 50      try it on 50 notes
  python3 draft_vocab.py                 all notes (answers cached; Ctrl-C and rerun to carry on)
  python3 draft_vocab.py --min-notes 3   only what came up in 3+ notes
  ```

  Suggestions are checked by rules, not trusted: a phrase that isn't really
  in the note is thrown out, known words are skipped, badly spelled tags are
  dropped, and a "new" tag that's already a word for an existing tag goes
  under that tag. On four made-up notes (55 s) it found night sweats, new words
  for swelling and exertion, and some one-off noise that the default
  `--min-notes 2` hides.
- First real run (2026-09-15, `run-20260915-093312`): 1,322 notes from 12
  boxes, 0 failed, 349 (26%) with no tags, 2.2 tags per note, about 54 minutes.
  That says nothing crashed, not that the tags are right; the spot-check
  below measures that.
- Spot-check (2026-09-15): `spot_check.py` shows you a random sample of a
  run's notes with their tags, in your own terminal, and records right/wrong
  per tag and anything missing. Run it in its own window, not with `!` in
  Claude Code, because it shows note text. The summary is counts only, so it
  can be shared, and its numbers are the ones for the public README.

  ```
  python3 spot_check.py                  grade 30 notes from the latest run (q stops, rerun resumes)
  python3 spot_check.py --sample 50      grade more; the first 30 are kept
  python3 spot_check.py summary          the numbers: run totals, % right, missed vs vocabulary gap
  ```
- Reviewing the draft (2026-09-15): the first full draft had 227 suggested
  tags and 948 words, far too much to hand-edit, so `review_vocab.py` shows
  one suggestion at a time (most notes first) and writes `vocab.yaml` itself:
  add, reject for good, skip, rename, recategorise, merge into an existing
  tag, or edit the words. Decisions live in `drafts/decisions.jsonl`, so it
  resumes; vocab.yaml is backed up to `.bak` and only written if it parses.

  ```
  python3 review_vocab.py --min-notes 5    the 94 tags seen in 5+ notes first
  python3 review_vocab.py                  everything, most common first
  python3 review_vocab.py summary          decided, rejected, left to do
  ```
- Nightly timer (written 2026-09-14, not yet enabled): `systemd/taggle-rock.timer`
  runs `tag_run.py` at 04:30, after the 04:00 backup pull, and catches up after
  sleep (Persistent=true). It waits for Ollama and only runs on mains power; a
  skipped night is picked up by the next run. To turn it on, once a first
  hand run has been spot-checked:

  ```
  cp systemd/taggle-rock.service systemd/taggle-rock.timer ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now taggle-rock.timer
  systemctl --user list-timers taggle-rock.timer      when it runs next
  journalctl --user -u taggle-rock                    what the last run did
  ```
- Tag runs page on the tracker: `/tags/runs` lists runs with undo.
- **The laptop reaches the Pi over Tailscale** (`server` is
  `http://your-server-name:5000`), not the public address. Cloudflare ends HTTPS at
  its edge and can read what passes through, so reading every note that way
  would pass them all through Cloudflare. Tailscale is encrypted from the laptop
  to the Pi. Checked live 2026-09-14 over Tailscale: 403 for Qwen on an
  endpoint he has no permit for, 401 for a wrong secret and for a replay, and
  every reply straight from the app, not Cloudflare.
- Client setup (done 2026-09-14): `config.json` here holds server, client_id,
  secret and user_id, and is gitignored. Sign with private-track's
  `api_signing.py`. Send a named User-Agent (e.g. `taggle-rock/0.1`):
  Cloudflare blocks Python's default one with error 1010. Checked live: a
  signed request is refused 403 on an endpoint Qwen has no permit for, and a
  wrong secret or a replay is refused 401.

### 2. Weekly recap
- SQL compares this week with the previous four: logged days, flare days,
  pain and fatigue, score trend, UV, RMSSD, and which tags rose or fell.
- Qwen writes 5-10 plain lines from those numbers only.
- A Cloudflare Email Worker takes the signed text and emails you. The Worker
  gets its own secret and a send-only permit.

### Afterwards
- Port the auth and tags work to sardine-track-public (scratch-folder method).
- Then the README and developer docs for both repos, then the cycle model.

### Later: notes for future us (2026-09-14, while Qwen ran the first draft)
- **Anonymize before anything goes public.** Real names and addresses become
  placeholders ("your-server-name:5000", "app.example.com"): the Pi's
  Tailscale name `your-server-name`, `app.example.com`, Tailscale 100.x
  addresses, `/home/you` paths (e.g. the systemd units here), account and
  team names. Find them with:
  `git grep -n -I -E "your-server-name|sardinetracker\.com|duckdns|100\.[0-9]+\.[0-9]+\.[0-9]+|/home/you|your-account-name"`
  in each repo. Private-track reaches the public repo by the scratch-folder
  method, so scrub there, not in private history.
  First count (2026-09-14, tracked files): private-track mostly
  runbook-site.md (17) and notes/discoverability-checklist.md (16), plus a few
  in README, help, notes and one route; uv-wearable mostly notes/last_error.txt
  (36) and REMOTE_ACCESS.md (7), plus firmware comments; taggle-rock only the
  systemd unit path and this file; sardine-track-public mostly site/. Careful
  there: `sardinetracker.com` is the public project site's own domain and is
  meant to stay. What to scrub is the private instance (`app.` subdomain,
  Tailscale names and addresses, home paths).
- **Give taggle-rock a GitHub repo** once it's finished (private first, public
  later, after the scrub). config.json, runs/ and drafts/ are already gitignored.
- **Write a public walkthrough of the local Qwen setup**: Ollama as a user
  service, Vulkan on the Radeon 740M (`OLLAMA_VULKAN=1`), 6 threads and why,
  the qwen-local Modelfile (16K context, the "you are running locally" system
  prompt), thinking off, prompt reuse, and the measured speeds. Sources:
  ~/projects/local-llm-bench README and modelfiles, ~/.config/systemd/user/ollama.service.
- **Rework the README for private-track and sardine-track-public.** It's too
  long for anyone to read in full. Short front page: what it is, who it's for,
  a map, quick start; everything else moves to linked docs. Includes
  correcting the documents tree for the auth-hardening and tags files.

## Open questions for later
- Should Qwen read notes over the new signed API or the existing read-only SSH
  bridge? The API means one auth system; the bridge already exists.
- Recap day and time.
- The first vocabulary: seed it from the words already used in the notes
  (Qwen drafts it, you edit it).
