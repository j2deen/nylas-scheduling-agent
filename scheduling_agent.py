#!/usr/bin/env python3
"""A scheduling agent built on a Nylas agent account.

The agent owns its own mailbox and calendar. People email it, it proposes times
drawn from its own availability, and books the one they pick.

Two design choices are worth reading before the code.

**Polling, not webhooks.** Webhooks need public ingress, which a machine behind
a home NAT does not have. A five-minute timer costs a little latency and works
anywhere.

**The model is boxed in.** Inbound email is attacker-controlled text, and this
process can send mail and write calendar events. So the LLM's only job is to
turn a body into a JSON struct. It never chooses recipients — those come from
the message envelope. It never decides whether to send. It never emits prose
that reaches a third party; every outbound body is a template in this file. And
it does not choose which slot to book: that is a regex, because committing to a
calendar write is an action and actions stay on the code side of the boundary.

Dry-run is the default. Pass --send to actually reply.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WARNINGS = []


# --- config ----------------------------------------------------------------


@dataclass
class Config:
    agent_email: str
    allowed_senders: set
    model_url: str
    model_name: str
    connect_timeout: int = 10
    stall_timeout: int = 120
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("UTC"))
    search_days: int = 10
    default_duration: int = 30
    slots_to_offer: int = 3
    workday_start: int = 9
    workday_end: int = 17
    max_replies_per_thread: int = 3
    state_path: Path = Path("state.json")
    nylas_bin: str = "nylas"


def load_config(path):
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    account = raw.get("account", {})
    senders = raw.get("senders", {})
    model = raw.get("model", {})
    sched = raw.get("scheduling", {})
    limits = raw.get("limits", {})
    paths = raw.get("paths", {})

    allowed = {s.lower() for s in senders.get("allowed", [])}
    if not allowed:
        # No default on purpose. An agent that replies to anyone is an open
        # relay for calendar spam and an unbounded injection surface.
        raise ValueError("senders.allowed is empty — refusing to reply to everyone")

    state_path = Path(paths.get("state_file", "state.json")).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    return Config(
        agent_email=account["agent_email"].lower(),
        allowed_senders=allowed,
        model_url=model["url"],
        model_name=model["name"],
        connect_timeout=model.get("connect_timeout_seconds", 10),
        stall_timeout=model.get("stall_timeout_seconds", 120),
        tz=ZoneInfo(sched.get("timezone", "UTC")),
        search_days=sched.get("search_days", 10),
        default_duration=sched.get("default_duration_minutes", 30),
        slots_to_offer=sched.get("slots_to_offer", 3),
        workday_start=sched.get("workday_start_hour", 9),
        workday_end=sched.get("workday_end_hour", 17),
        max_replies_per_thread=limits.get("max_replies_per_thread", 3),
        state_path=state_path,
        nylas_bin=paths.get("nylas_bin", "nylas"),
    )


# --- plumbing --------------------------------------------------------------


def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


class StageError(Exception):
    def __init__(self, stage, detail):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


def attempt(stage, fn, attempts=3, backoff=(5, 15), validate=None):
    """Run fn with retries. validate() returns a problem string, or None if ok."""
    last = None
    for i in range(1, attempts + 1):
        try:
            result = fn()
            if validate:
                problem = validate(result)
                if problem:
                    raise RuntimeError(problem)
            if i > 1:
                log(f"[{stage}] recovered on attempt {i}")
            return result
        except (ImportError, TypeError, SyntaxError) as e:
            # Permanent: a retry cannot change the outcome, so fail fast rather
            # than burning the backoff budget.
            log(f"[{stage}] permanent error — {type(e).__name__}: {e}")
            raise StageError(stage, f"{type(e).__name__}: {e}")
        except Exception as e:
            last = e
            log(f"[{stage}] attempt {i}/{attempts} failed — {type(e).__name__}: {e}")
            if i < attempts:
                delay = backoff[min(i - 1, len(backoff) - 1)]
                log(f"[{stage}] retrying in {delay}s")
                time.sleep(delay)
    raise StageError(stage, f"{type(last).__name__}: {last}")


def optional(stage, fn):
    """Run a stage whose failure must not cost us the rest of the run."""
    try:
        return fn()
    except Exception as e:
        log(f"[{stage}] non-fatal — {e}")
        WARNINGS.append(f"{stage}: {e}")
        return None


def read_state(cfg):
    try:
        return json.loads(cfg.state_path.read_text())
    except (OSError, ValueError):
        return {"threads": {}}


def write_state(cfg, state):
    try:
        cfg.state_path.write_text(json.dumps(state, indent=2))
    except OSError as e:
        log(f"[state] could not persist — {e}")


# --- nylas cli -------------------------------------------------------------

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def clean_cli_error(raw):
    """The CLI wraps errors in spinner frames and blank padding. Keep the
    message, drop the animation, so logs say what actually went wrong."""
    lines = [ln.strip() for ln in raw.replace("\r", "\n").splitlines()]
    useful = [ln for ln in lines
              if ln and "Finding" not in ln and not ln.startswith(SPINNER_FRAMES)]
    return " ".join(useful)[:300] or raw.strip()[:300]


def nylas(cfg, *args, timeout=120):
    """Run the CLI and return parsed JSON.

    The file-store passphrase must be in the environment. systemd never sources
    a shell rc, so the unit passes it explicitly (see systemd/ in this repo)."""
    proc = subprocess.run(
        [cfg.nylas_bin, *args, "--json"],
        capture_output=True, text=True, timeout=timeout, env=dict(os.environ),
    )
    if proc.returncode != 0:
        raise RuntimeError(clean_cli_error(proc.stderr or proc.stdout))

    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        # Spinner frames precede the JSON body on stdout.
        brace = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
        if brace < 0:
            raise
        return json.loads(out[brace:])


def as_list(payload, *keys):
    """Responses vary: a bare list, {data: [...]}, or {data: {time_slots: [...]}}."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    for k in (*keys, "data", "items", "messages", "results"):
        v = payload.get(k)
        if isinstance(v, list):
            return v
    return []


# --- filtering -------------------------------------------------------------

AUTOMATED_HINTS = ("no-reply", "noreply", "do-not-reply", "donotreply",
                   "mailer-daemon", "postmaster", "bounce")


def sender_of(msg):
    frm = msg.get("from") or []
    if isinstance(frm, list) and frm:
        first = frm[0]
        if isinstance(first, dict):
            return (first.get("email") or "").lower()
        return str(first).lower()
    if isinstance(frm, str):
        return frm.lower()
    return ""


def should_handle(cfg, msg, state):
    """Return None to proceed, or a string reason to skip."""
    sender = sender_of(msg)
    if not sender:
        return "no sender"
    if sender == cfg.agent_email:
        return "message from self (loop guard)"
    if any(h in sender for h in AUTOMATED_HINTS):
        return f"automated sender ({sender})"
    if sender not in cfg.allowed_senders:
        return f"sender not allowlisted ({sender})"

    thread_id = msg.get("thread_id") or msg.get("id")
    seen = state["threads"].get(thread_id, {})
    if seen.get("replies", 0) >= cfg.max_replies_per_thread:
        return f"thread already answered {seen['replies']}x (loop guard)"
    if msg.get("id") in seen.get("handled_messages", []):
        return "message already handled"
    return None


# --- extraction ------------------------------------------------------------

# Note the placeholders are __TODAY__/__BODY__ rather than {today}/{body}:
# str.format() would read the literal JSON braces below as replacement fields.
EXTRACT_PROMPT = """Extract meeting request details from this email.

Reply with ONLY a JSON object, no prose, no code fence:
{"is_meeting_request": true/false, "duration_minutes": <int>, "topic": "<short>", "earliest_date": "YYYY-MM-DD or null", "latest_date": "YYYY-MM-DD or null"}

Rules:
- is_meeting_request is false unless they are asking to meet, call, or schedule.
- duration_minutes: use 30 if unstated.
- topic: at most 8 words, drawn only from the email's subject matter.
- Dates: null unless the email names a specific date or range. Today is __TODAY__.

Email:
---
__BODY__
---"""


def extract_request(cfg, body, today):
    """Ask the model for structured fields only — never for text we will send."""
    import requests

    prompt = (EXTRACT_PROMPT
              .replace("__TODAY__", today)
              .replace("__BODY__", body[:4000]))
    r = requests.post(
        cfg.model_url,
        json={"model": cfg.model_name,
              "messages": [{"role": "user", "content": prompt}],
              "stream": True, "temperature": 0},
        timeout=(cfg.connect_timeout, cfg.stall_timeout),
        stream=True,
    )
    r.raise_for_status()

    # Decode the SSE stream as UTF-8 explicitly. requests' decode_unicode=True
    # falls back to ISO-8859-1 for text/* with no charset, which turns every
    # curly quote into mojibake before it reaches us.
    parts = []
    for chunk in r.iter_lines():
        raw = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            break
        try:
            delta = json.loads(payload)["choices"][0]["delta"].get("content")
        except (ValueError, KeyError, IndexError):
            continue
        if delta:
            parts.append(delta)
    r.close()

    text = "".join(parts).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"no JSON in model output: {text[:200]}")
    return json.loads(match.group(0))


def validate_request(req):
    if not isinstance(req, dict):
        return "not an object"
    if "is_meeting_request" not in req:
        return "missing is_meeting_request"
    return None


def sanitize_topic(value):
    """Strip anything that isn't a word, space or dash. The topic is the only
    model-derived string that reaches a recipient, so it gets scrubbed."""
    return re.sub(r"[^\w\s-]", "", str(value or ""))[:60].strip()


# --- calendar --------------------------------------------------------------


def round_to_5(dt, up=True):
    """The availability API rejects a start_time that is not a multiple of five
    minutes, and datetime.now() never is."""
    dt = dt.replace(second=0, microsecond=0)
    if up:
        return dt + timedelta(minutes=(5 - dt.minute % 5) % 5)
    return dt.replace(minute=dt.minute // 5 * 5)


def pick_slots(cfg, raw_slots):
    """Filter API slots down to the few we will offer.

    Selection stays in code: the model does not choose what we put on a
    calendar. Slots arrive every 15 minutes, so without the one-per-day rule
    the 'choice' would be 9:00, 9:15 and 9:30."""
    slots = []
    for entry in raw_slots:
        begin = entry.get("start_time") or entry.get("start")
        if begin is None:
            continue
        when = (datetime.fromtimestamp(begin, tz=timezone.utc)
                if isinstance(begin, (int, float))
                else datetime.fromisoformat(str(begin).replace("Z", "+00:00")))
        local = when.astimezone(cfg.tz)
        if local.weekday() >= 5 or not (cfg.workday_start <= local.hour < cfg.workday_end):
            continue
        if any(s.date() == local.date() for s in slots):
            continue
        slots.append(local)
        if len(slots) >= cfg.slots_to_offer:
            break
    return slots


def find_slots(cfg, duration_min, earliest, latest):
    start = round_to_5(earliest or datetime.now(timezone.utc), up=True)
    end = round_to_5(latest or (start + timedelta(days=cfg.search_days)), up=False)

    payload = nylas(
        cfg, "calendar", "availability", "find",
        "--duration", str(duration_min),
        # A bare YYYY-MM-DD is rejected (E006) — the parser needs a time.
        "--start", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end", end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        # --participants is required. There is no --timezone flag; slots come
        # back as UTC epochs and pick_slots() converts them.
        "--participants", cfg.agent_email,
    )
    return pick_slots(cfg, as_list(payload, "time_slots", "slots"))


def compose_reply(cfg, topic, duration_min, slots):
    """Built from a template. No model output reaches the recipient."""
    if not slots:
        return (f"Thanks for the note. I don't have a free {duration_min}-minute "
                f"window in the next {cfg.search_days} days during working hours.\n\n"
                "Reply with a time that suits you and I'll check it directly.\n\n"
                "-- scheduling agent")

    lines = [f"  {i}. {s.strftime('%A, %B %-d at %-I:%M %p %Z')}"
             for i, s in enumerate(slots, 1)]
    about = f" about {topic}" if topic else ""
    return (f"Happy to find time{about}. Here are {len(slots)} open "
            f"{duration_min}-minute slots:\n\n"
            + "\n".join(lines)
            + "\n\nReply with the number that works and I'll send an invitation.\n\n"
              "-- scheduling agent")


def compose_confirmation(slot, duration_min):
    return (f"Booked: {slot.strftime('%A, %B %-d at %-I:%M %p %Z')} "
            f"({duration_min} minutes). An invitation is on its way.\n\n"
            "-- scheduling agent")


# --- booking ---------------------------------------------------------------

# Choosing a slot is an action, so it is a regex rather than a model call — the
# worst a crafted reply can do is pick a slot we already offered.
#
# The negative lookahead earns its keep: "can we do 1 hour instead?" is a NEW
# request, and a bare \b([1-9])\b cheerfully books slot 1 for it.
_UNIT = r"(?!\s*(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes|am|pm|st|nd|rd|th|[:/\-\d]))"
CONFIRM_EXPLICIT = re.compile(rf"\b(?:option|slot|number|no\.?|#)\s*([1-9]){_UNIT}", re.I)
CONFIRM_BARE = re.compile(rf"\b([1-9]){_UNIT}\b")

# A confirmation is terse. Longer text is prose that merely contains a digit.
CONFIRM_MAX_CHARS = 120


def detect_choice(body, offered):
    """Return the chosen slot datetime, or None if this is not a confirmation."""
    head = body.strip()[:400]

    match = CONFIRM_EXPLICIT.search(head)
    if not match:
        if len(head) > CONFIRM_MAX_CHARS:
            return None
        match = CONFIRM_BARE.search(head)
    if not match:
        return None

    index = int(match.group(1)) - 1
    if not (0 <= index < len(offered)):
        return None
    return datetime.fromisoformat(offered[index])


def book(cfg, slot, duration_min, topic, attendee):
    end = slot + timedelta(minutes=duration_min)
    return nylas(
        cfg, "calendar", "events", "create",
        "--title", f"{topic or 'Meeting'} — {attendee}"[:100],
        "--start", slot.strftime("%Y-%m-%d %H:%M"),
        "--end", end.strftime("%Y-%m-%d %H:%M"),
        "--timezone", str(cfg.tz),
        "--participant", attendee,
    )


# --- main ------------------------------------------------------------------


def message_body(cfg, msg):
    """Prefer the quoted-reply-stripped body; fall back to what we already have."""
    cleaned = optional("clean", lambda: nylas(cfg, "email", "clean", msg.get("id")))
    body = ""
    if isinstance(cleaned, dict):
        body = cleaned.get("body") or cleaned.get("text") or ""
    return body or msg.get("snippet") or msg.get("body") or ""


def handle_confirmation(cfg, msg, entry, body, sender, send):
    """Returns True if this message was a slot confirmation and was handled."""
    offered = entry.get("offered") or []
    if not offered:
        return False
    choice = detect_choice(body, offered)
    if not choice:
        return False

    duration = int(entry.get("duration") or cfg.default_duration)
    topic = entry.get("topic") or ""
    log(f"  confirmation: {choice.strftime('%a %b %d %H:%M %Z')}")
    if not send:
        log("  DRY RUN — would book and confirm")
        return True

    msg_id = msg.get("id")
    attempt("book", lambda: book(cfg, choice, duration, topic, sender))
    attempt("confirm", lambda: nylas(cfg, "email", "reply", msg_id,
                                     "--body", compose_confirmation(choice, duration)))
    optional("mark-read", lambda: nylas(cfg, "email", "mark", "read", msg_id))

    entry["replies"] = entry.get("replies", 0) + 1
    entry.setdefault("handled_messages", []).append(msg_id)
    entry["booked"] = choice.isoformat()
    entry.pop("offered", None)
    log("  booked and confirmed")
    return True


def handle_message(cfg, msg, state, send):
    msg_id = msg.get("id")
    thread_id = msg.get("thread_id") or msg_id
    sender = sender_of(msg)
    log(f"[msg] {sender} — {(msg.get('subject') or '(no subject)')[:60]}")

    skip = should_handle(cfg, msg, state)
    if skip:
        log(f"  skipped: {skip}")
        return False

    body = message_body(cfg, msg)
    if not body.strip():
        log("  skipped: empty body")
        return False

    entry = state["threads"].setdefault(thread_id, {})

    # A short reply naming a slot we already offered is a confirmation, not a
    # fresh request, so check that before spending a model call.
    if handle_confirmation(cfg, msg, entry, body, sender, send):
        return True

    req = attempt("extract",
                  lambda: extract_request(cfg, body, datetime.now().strftime("%Y-%m-%d")),
                  validate=validate_request)

    if not req.get("is_meeting_request"):
        log("  not a meeting request")
        entry.setdefault("handled_messages", []).append(msg_id)
        return False

    duration = max(15, min(int(req.get("duration_minutes") or cfg.default_duration), 240))
    topic = sanitize_topic(req.get("topic"))

    def parse_date(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    slots = attempt("availability", lambda: find_slots(
        cfg, duration, parse_date(req.get("earliest_date")), parse_date(req.get("latest_date"))))
    log(f"  meeting request: {duration}min, {len(slots)} slot(s) found")

    reply = compose_reply(cfg, topic, duration, slots)
    if not send:
        log("  DRY RUN — would reply:")
        for line in reply.splitlines():
            log(f"    | {line}")
        return True

    attempt("reply", lambda: nylas(cfg, "email", "reply", msg_id, "--body", reply))
    optional("mark-read", lambda: nylas(cfg, "email", "mark", "read", msg_id))

    entry["replies"] = entry.get("replies", 0) + 1
    entry.setdefault("handled_messages", []).append(msg_id)
    entry["last_reply"] = datetime.now().isoformat(timespec="seconds")
    entry["offered"] = [s.isoformat() for s in slots]
    entry["duration"] = duration
    entry["topic"] = topic
    log("  replied")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--send", action="store_true",
                    help="actually reply and book (default is a dry run)")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not args.send:
        log("DRY RUN — nothing will be sent. Pass --send to enable.")

    state = read_state(cfg)
    messages = as_list(attempt("inbox", lambda: nylas(
        cfg, "email", "list", "--limit", str(args.limit), "--unread")))
    log(f"{len(messages)} unread message(s)")

    acted = 0
    for msg in messages:
        try:
            if handle_message(cfg, msg, state, args.send):
                acted += 1
        except StageError as e:
            log(f"  FAILED at {e.stage}: {e.detail}")
            WARNINGS.append(f"{sender_of(msg)}: {e}")

    write_state(cfg, state)
    log(f"done — {acted} handled, {len(WARNINGS)} warning(s)")
    for w in WARNINGS:
        log(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
