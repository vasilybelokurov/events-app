"""Subject tagging by a local model, cached so the daily build never runs one.

Keyword matching cannot read.  It tagged a woodworking class "Computing & AI"
because the venue also runs digital media, and a session for children with
"sensory needs" the same way through the stem "sensor".  A small local model
reads the sentence instead.

How this stays cheap and reproducible:

* **The build never calls a model.**  `collector.build` reads the cache this
  writes and falls back to keywords for anything missing, so GitHub Actions
  needs no GPU, no download and no network beyond the venues themselves.
* **The cache key is the text, not the event id.**  Ids are not uniform across
  adapters -- an HTML row with no link is keyed on title and start -- and an
  id can outlive a rewritten summary.  Keying on a hash of what the model
  actually read means changed text is reclassified and unchanged text is not.
* **The prompt and model are part of the key.**  Editing either invalidates
  every entry, which is the point: a cached label should never be attributable
  to a prompt that no longer exists.

Usage::

    ollama serve                      # the HTTP API this talks to
    python -m collector.classify              # classify what is missing
    python -m collector.classify --limit 20   # a taste, for a quick look
    python -m collector.classify --recheck    # ignore the cache

The prompt's shape matters more than the model's size.  An earlier wording
said "if the text says too little, answer []", and both 8B and 14B then
returned nothing for "Martino Tirimo (piano) — a lunchtime piano recital":
they read a proper-noun title as "too little" and never reached the summary.
Saying so explicitly fixed every arts case at both sizes.

Version 2 went further: the model was still abstaining on summaries that
plainly described the event without naming a subject ("how can Facebook,
LinkedIn, YouTube help your business?"), so it is now told that a description
of what happens is enough.  Abstention on an absent or contentless summary
survived that change, which is the property worth keeping.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .careers import CAREER_KEYWORDS

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data/classifications.json"
EVENTS_PATH = ROOT / "docs/data/events.json"

DEFAULT_MODEL = "qwen3:14b"
#: Bump when the wording below changes; it is part of every cache key.
PROMPT_VERSION = 3

LABELS = tuple(CAREER_KEYWORDS)

PROMPT = """Tag this event with the subjects it is about.

Subjects (copy wording exactly, choose only from these):
{labels}

Read the title AND the summary. Choose 1 to 3 subjects that either of them
supports -- either one alone is enough. A name of a person, place or series
tells you nothing, but a title like "Socialism after AI" does. If the summary
describes what happens, that is enough to choose a subject: do not hold out
for the subject to be named.
Answer [] only when neither the title nor the summary says anything about
what the event is about. A summary that is only a date, a price or a venue
says nothing -- judge the title on its own in that case.

Title: {title}
Summary: {summary}

JSON array of subject names only."""


def prompt_for(title: str, summary: str | None) -> str:
    return PROMPT.format(labels="\n".join(f"- {l}" for l in LABELS),
                         title=title,
                         summary=summary or "(no description given)")


def cache_key(title: str, summary: str | None, model: str) -> str:
    """Hash of everything the answer depends on: text, model and prompt."""
    blob = "\x1f".join([model, str(PROMPT_VERSION), title, summary or ""])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def parse_reply(text: str) -> list[str]:
    """Pull a clean label list out of whatever the model said.

    Models wrap the array in prose, in a fenced block, or hand back objects
    (``[{"subject": "..."}]``).  Anything not in the vocabulary is dropped
    rather than guessed at: an invented label is exactly what this is meant to
    stop.
    """
    match = re.search(r"\[.*?\]", text, re.S)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            item = next((v for v in item.values() if isinstance(v, str)), "")
        if not isinstance(item, str):
            continue
        name = item.split(":")[0].strip()          # "Label: gloss" -> "Label"
        for label in LABELS:
            if name.lower() == label.lower() and label not in out:
                out.append(label)
    return out[:3]


#: Same text, same labels, every run.  The CLI leaves sampling at the model's
#: default, and it showed: "How to Start and Run a Successful Restaurant" came
#: back as Business once and as nothing the next time.  A cached label has to
#: be reproducible or the cache is just a record of one lucky roll.
OPTIONS = {"temperature": 0, "top_p": 1, "seed": 1, "num_predict": 128}
API = "http://127.0.0.1:11434/api/generate"


def ask(model: str, title: str, summary: str | None, *, timeout: int = 240) -> list[str]:
    body = json.dumps({
        "model": model,
        "prompt": prompt_for(title, summary),
        "stream": False,
        "think": False,
        "options": OPTIONS,
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return parse_reply(payload.get("response", ""))


def load_cache(path: Path = CACHE_PATH) -> dict:
    if not path.exists():
        return {"model": DEFAULT_MODEL, "prompt_version": PROMPT_VERSION, "entries": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_cache(cache: dict, path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(path)                              # atomic, as the build is


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--events", type=Path, default=EVENTS_PATH)
    ap.add_argument("--cache", type=Path, default=CACHE_PATH)
    ap.add_argument("--limit", type=int, help="classify at most this many")
    ap.add_argument("--recheck", action="store_true", help="ignore cached answers")
    args = ap.parse_args(argv)

    events = json.loads(args.events.read_text(encoding="utf-8"))["events"]
    cache = load_cache(args.cache)
    entries = cache.setdefault("entries", {})
    cache["model"] = args.model
    cache["prompt_version"] = PROMPT_VERSION

    # Drop answers from a superseded prompt or model.  Without this the cache
    # keeps every generation for ever and, worse, anything reading it sees two
    # sets of labels for the same event and silently mixes them.
    stale = [k for k, v in entries.items()
             if v.get("prompt_version") != PROMPT_VERSION or v.get("model") != args.model]
    for k in stale:
        del entries[k]

    todo = []
    for e in events:
        key = cache_key(e["title"], e.get("summary"), args.model)
        if args.recheck or key not in entries:
            todo.append((key, e))
    if args.limit:
        todo = todo[:args.limit]

    print(f"{len(events)} events, {len(entries)} cached "
          f"({len(stale)} stale dropped), {len(todo)} to classify")
    done = 0
    for key, e in todo:
        try:
            labels = ask(args.model, e["title"], e.get("summary"))
        except Exception as exc:                   # noqa: BLE001
            print(f"  ! {e['title'][:40]}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        entries[key] = {
            "labels": labels,
            "title": e["title"][:120],             # so the cache is readable
            "model": args.model,
            "prompt_version": PROMPT_VERSION,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        done += 1
        if done % 25 == 0:
            save_cache(cache, args.cache)
            print(f"  {done}/{len(todo)}")
    save_cache(cache, args.cache)
    print(f"classified {done}; cache now holds {len(entries)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
