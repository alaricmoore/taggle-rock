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
- Server: verify HMAC signatures, with a secret and permits per client in
  config. Keep accepting bearer tokens while the apps migrate.
- Permits: health_sync, flare_status, uv_ingest, backup, notes_read,
  tags_write, recap_read.
- Wearable firmware: check the server certificate instead of `setInsecure()`.
- Retire the iOS Shortcut docs.
- Tests: good signatures accepted; wrong secret, altered body, stale time,
  replayed request, and missing permit rejected.

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

### 2. Weekly recap
- SQL compares this week with the previous four: logged days, flare days,
  pain and fatigue, score trend, UV, RMSSD, and which tags rose or fell.
- Qwen writes 5-10 plain lines from those numbers only.
- A Cloudflare Email Worker takes the signed text and emails you. The Worker
  gets its own secret and a send-only permit.

### Afterwards
- Port the auth and tags work to sardine-track-public (scratch-folder method).
- Then the README and developer docs for both repos, then the cycle model.

## Open questions for later
- Should Qwen read notes over the new signed API or the existing read-only SSH
  bridge? The API means one auth system; the bridge already exists.
- Recap day and time.
- The first vocabulary: seed it from the words already used in the notes
  (Qwen drafts it, you edit it).
