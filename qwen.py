"""
Asking qwen-local for a note's tags, through Ollama on this laptop.

Nothing here leaves the laptop: Ollama listens on localhost only.

Qwen's answer is constrained by a JSON schema to the vocabulary's tag names,
then checked again here, because a constraint in the request is not a
promise. He never picks a tag's category (that comes from the vocabulary),
and he is never asked how confident he is: his confidence ratings mean
nothing, so they aren't collected.
"""

import hashlib
import json
import urllib.error
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen-local"
MAX_TAGS = 20   # the tracker's limit per note


class QwenError(Exception):
    """No usable answer for this note. The run skips it and reports it."""


def system_prompt(vocab) -> str:
    """The instructions and the vocabulary. The same text for every note, so
    Ollama can reuse its work on it and only the note itself is new each time."""
    lines = [
        "You tag short health diary notes written by one person with lupus.",
        "Pick tags ONLY from the vocabulary below. Each tag is followed by words",
        "that mean it; a note that uses one of those words, or plainly says the",
        "same thing in other words, gets that tag.",
        "",
        "Rules:",
        "- Every tag must be earned by words in the note. If you could not point",
        "  to the words that gave you a tag, leave it out.",
        "- Do not add what is merely likely, related, or usually true of this",
        "  illness. No causes, diagnoses or consequences the note does not state.",
        "- A negated mention does not get the tag: \"no rash today\" is not rash.",
        "- Fewer, well-earned tags beat a long list. Most notes need one to four.",
        "- If nothing in the vocabulary applies, return an empty list.",
        f"- At most {MAX_TAGS} tags. Answer with JSON only.",
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
    """A short hash of the exact instructions. Run logs record it, because
    comparing two runs only means something with the prompt held still."""
    return hashlib.sha256(system_prompt(vocab).encode("utf-8")).hexdigest()[:8]


def schema(vocab) -> dict:
    """The only shape Ollama will let Qwen answer in."""
    return {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "maxItems": MAX_TAGS,
                     "items": {"type": "string", "enum": vocab.tags}},
        },
        "required": ["tags"],
    }


def field_label(field: str) -> str:
    """ "rheumatic_notes" -> "rheumatic", "notes" -> "general"."""
    return "general" if field == "notes" else field.removesuffix("_notes").replace("_", " ")


def check_answer(vocab, content: str) -> list:
    """[(tag, category)] from Qwen's reply, or QwenError. Repeats are dropped."""
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        raise QwenError(f"answer is not JSON: {str(content)[:80]!r}") from None
    tags = data.get("tags") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        raise QwenError("answer has no tags list")
    unknown = [t for t in tags if t not in vocab.category_of]
    if unknown:
        raise QwenError(f"answer has tags not in the vocabulary: {unknown[:5]}")
    unique = list(dict.fromkeys(tags))
    if len(unique) > MAX_TAGS:
        raise QwenError(f"answer has {len(unique)} tags, more than {MAX_TAGS}")
    return [(t, vocab.category_of[t]) for t in unique]


def _chat(system: str, user: str, answer_schema: dict, url: str, model: str, timeout: int):
    """One question to Qwen through Ollama. Returns his answer's text. Raises QwenError."""
    body = {
        "model": model,
        "stream": False,
        # Thinking off. On a made-up note (2026-09-14) it gave the same tags in
        # 20 s instead of 190 s; local-llm-bench found the same.
        "think": False,
        "format": answer_schema,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:
        raise QwenError(f"Ollama said HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise QwenError(f"could not reach Ollama at {url}: {e}") from None
    except ValueError:
        raise QwenError("Ollama's reply was not JSON") from None
    message = reply.get("message") if isinstance(reply, dict) else None
    return (message or {}).get("content")


def ask(vocab, field: str, text: str, url: str = OLLAMA_URL, model: str = MODEL,
        timeout: int = 600) -> list:
    """Qwen's tags for one note, as [(tag, category)]. Raises QwenError."""
    content = _chat(system_prompt(vocab), f"Box: {field_label(field)}\nNote:\n{text}",
                    schema(vocab), url, model, timeout)
    return check_answer(vocab, content)


# ------------------------------------------------------------------
# Drafting the vocabulary (draft_vocab.py)
# ------------------------------------------------------------------

MAX_CANDIDATES = 10
CATEGORIES = ("body_part", "symptom", "severity", "context")


def candidates_prompt(vocab) -> str:
    """Instructions for suggesting vocabulary from a note. The same for every note."""
    lines = [
        "You help build a tag vocabulary for one person's lupus health diary.",
        "Read the note and list the health-related words or short phrases in it:",
        "symptoms, body parts, how bad something is, and context that might",
        "matter (medications, activities, sleep, weather, cycle, stress, sun).",
        "",
        "For each one:",
        "- phrase: copy it EXACTLY as written in the note, a few words at most.",
        "- tag: the vocabulary tag it belongs to if one fits; otherwise a short",
        "  new tag in lowercase plain English (no brand names unless that's the",
        "  common word).",
        "- category: body_part, symptom, severity or context.",
        "",
        "Skip anything negated (\"no rash today\"). Skip things that aren't about",
        f"health or its context. At most {MAX_CANDIDATES}. Answer with JSON only;",
        "an empty list if nothing fits.",
        "",
        "Current vocabulary (tag: words that mean it):",
    ]
    for tag, category in vocab.category_of.items():
        words = ", ".join(vocab.words_for[tag])
        lines.append(f"{tag} [{category}]: {words}" if words else f"{tag} [{category}]")
    return "\n".join(lines)


def candidates_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array", "maxItems": MAX_CANDIDATES,
                "items": {
                    "type": "object",
                    "properties": {
                        "phrase": {"type": "string"},
                        "tag": {"type": "string"},
                        "category": {"type": "string", "enum": list(CATEGORIES)},
                    },
                    "required": ["phrase", "tag", "category"],
                },
            },
        },
        "required": ["candidates"],
    }


def ask_candidates(vocab, field: str, text: str, url: str = OLLAMA_URL, model: str = MODEL,
                   timeout: int = 600) -> list:
    """Qwen's vocabulary suggestions for one note: [{phrase, tag, category}].

    Only the shape is checked here; draft_vocab.py decides what to keep (a
    phrase must really be in the note, a tag must be spelled right).
    Malformed items are dropped. Raises QwenError if the answer as a whole is.
    """
    content = _chat(candidates_prompt(vocab), f"Box: {field_label(field)}\nNote:\n{text}",
                    candidates_schema(), url, model, timeout)
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        raise QwenError(f"answer is not JSON: {str(content)[:80]!r}") from None
    items = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise QwenError("answer has no candidates list")
    return [{"phrase": c["phrase"], "tag": c["tag"], "category": c["category"]}
            for c in items[:MAX_CANDIDATES]
            if isinstance(c, dict) and isinstance(c.get("phrase"), str)
            and isinstance(c.get("tag"), str) and c.get("category") in CATEGORIES]
