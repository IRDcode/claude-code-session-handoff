#!/usr/bin/env python3
"""Measure how heavy a Claude Code session has become.

Reads a session's JSONL transcript and scores it against the handoff
thresholds. Designed to be called two ways:

  * as a library, by the session-weight-watch hook (incremental, ~10ms)
  * as a CLI, by the long-session-handoff skill (full report)

    python session_weight.py --session-id <uuid> [--json]
    python session_weight.py --transcript <path.jsonl> [--json]

Nothing here writes to the transcript. Failure is always soft: a broken
measurement must never break a turn.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------- thresholds
# The context signal is a fraction of THE WALL THIS SESSION ACTUALLY HITS, not
# of the context window. Those are different numbers, and confusing them is the
# bug this whole project exists to fix. Because everything is expressed as a
# fraction, the thresholds re-scale themselves on any model, any account tier
# and any window size -- nothing here needs re-tuning per machine.
#
#   CTX_HARD_PCT 0.85 -> hand off with 15% of the wall still free.
#
# Executing a handoff costs roughly 25,000 tokens of context: the dump runs as
# a subprocess and returns counts rather than content, and the brief is written
# to disk. On a 1M-class window 15% is ~147,000 tokens, a 5x margin, so the
# handoff always wins the race against the wall. On a 200k window it is ~26,000
# -- tighter, which is why CTX_CRITICAL_PCT exists to skip the question and act.
#
# Do not lower these to "be safe". Every token below the wall that this session
# does not use is a token the next session has to re-establish, and
# re-establishing is the expensive half.

CTX_HARD_PCT = 0.85      # >= this share of the wall -> hand off now
CTX_SOFT_PCT = 0.62      # >= this share counts as one weight signal
CTX_CRITICAL_PCT = 0.95  # >= this share -> ignore the anti-nag, say it every turn

# THE GATE. No handoff is ever offered below this share of the wall, no matter
# how many other signals trip.
#
# Found by running this scorer against the session that wrote it: 4.3h active
# plus 2 earlier compactions scored 3 = DUE while context sat at 143,116 of a
# 977,000 wall. Handing off there would have abandoned 833,884 tokens of
# paid-for headroom to save nothing, and the continuation would have started at
# ~25,000 tokens to re-learn what the parent already knew.
#
# Turns, tool calls, hours and past compactions are PROXIES for context
# pressure, invented for a world where context could not be measured. Here it
# can be. So they no longer get a vote on WHETHER to move; they only sharpen
# the urgency once context itself says moving is due.
#
# A prior compaction is the clearest case. It is a permanent historical fact:
# scored as a trigger it fires forever, on a session whose context is now small
# precisely BECAUSE it was compacted. The right response to a past compaction is
# to recover the dropped rows from disk (05-dropped-context.md) and fix the
# window -- never to migrate a session that still has room.
CTX_GATE_PCT = CTX_SOFT_PCT

ASSISTANT_THRESHOLD = 900
TOOLCALL_THRESHOLD = 600
HOURS_THRESHOLD = 4.0   # ACTIVE hours, never wall-clock span
SUBTASK_THRESHOLD = 5   # judged by the model, not measurable here
SCORE_TO_FIRE = 3

IDLE_GAP_SECONDS = 10 * 60   # a gap this long or longer is not working time

# Reserves the client keeps between the window and each wall. Measured from
# observed behaviour, not guessed -- see docs/HOW-IT-WORKS.md for the method
# and README.md for how to re-derive them from your own transcripts if a future
# release moves them.
SUMMARY_BUFFER = 13000    # held back for the compaction summary
OUTPUT_RESERVE = 20000    # held back for the model's own reply
BLOCKED_RESERVE = 3000    # margin under the ceiling where sending is refused

# Fallback only, used when the client has not reported its window yet. Every
# code path prefers the live figure; see observed_window().
DEFAULT_WINDOW = 200000

TIER_ROWS = 200          # re-offer after this many more assistant rows
TIER_PCT_BANDS = 10      # ...or when context crosses another 1/10th
REOFFER_COOLDOWN_S = 900  # ...but never twice inside 15 minutes

# Honour CLAUDE_CONFIG_DIR: users who relocate their config expect every tool
# that reads it to follow, and silently reading the wrong settings.json would
# make every number below wrong in a way that is very hard to notice.
CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude"))


# ------------------------------------------------------------------ helpers
def _ts(value):
    """ISO timestamp -> epoch seconds, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# Paths are derived from CLAUDE_DIR through these helpers rather than bound as
# default arguments, because a default argument is evaluated once at import and
# would then ignore any later change to CLAUDE_DIR -- silently reading the wrong
# settings.json, which makes every number in this module wrong in a way that is
# very hard to notice.
def _projects_dir():
    return os.path.join(CLAUDE_DIR, "projects")


def _runtime(name):
    return os.path.join(CLAUDE_DIR, "runtime", name)


def find_transcript(session_id, projects_dir=None):
    """Locate <projects>/*/<session_id>.jsonl. Newest match wins."""
    projects_dir = projects_dir or _projects_dir()
    if not session_id:
        return None
    best = None
    try:
        slugs = os.listdir(projects_dir)
    except OSError:
        return None
    for slug in slugs:
        cand = os.path.join(projects_dir, slug, session_id + ".jsonl")
        try:
            st = os.stat(cand)
        except OSError:
            continue
        if best is None or st.st_mtime > best[0]:
            best = (st.st_mtime, cand)
    return best[1] if best else None


def _settings_files():
    return (os.path.join(CLAUDE_DIR, "settings.json"),
            os.path.join(CLAUDE_DIR, "settings.local.json"))


def _setting(key, env_block=False):
    """First settings file that defines `key` (or env.<key>) wins."""
    for path in _settings_files():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        src = (data.get("env") or {}) if env_block else data
        if isinstance(src, dict) and key in src:
            return src[key], os.path.basename(path)
    return None, None


def _env(name):
    """Live env first, then settings.json's env block (which populates it)."""
    val = os.environ.get(name)
    if val not in (None, ""):
        return val
    val, _ = _setting(name, env_block=True)
    return val


def _truthy(value):
    """Truthiness the way the client reads env flags: "1"/"true"/"yes"/"on"."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def compact_enabled():
    """Can threshold-triggered compaction fire at all? (bool, why).

    False when DISABLE_COMPACT or DISABLE_AUTO_COMPACT is set, else the
    autoCompactEnabled setting, which defaults to on.

    DISABLE_COMPACT also disables manual /compact. With it set a handoff is the
    ONLY way out of a full session, which is exactly why this module measures
    the hard wall rather than assuming a summary will save you.
    """
    for var in ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT"):
        if _truthy(_env(var)):
            return False, var
    val, where = _setting("autoCompactEnabled")
    if val is False:
        return False, f"autoCompactEnabled=false ({where})"
    return True, "default"


def remember_window(size, path=None):
    """Record the window the client reported for itself. Never raises.

    The status line receives `context_window.context_window_size` on every
    render: the running client's own answer to the question this module
    otherwise has to infer. Cache it so the CLI paths (which get no such
    payload) can use the same figure instead of guessing.
    """
    path = path or _runtime("observed-window.json")
    if not isinstance(size, int) or size <= 0:
        return
    try:
        prev = None
        try:
            with open(path, encoding="utf-8") as fh:
                prev = (json.load(fh) or {}).get("window")
        except (OSError, ValueError):
            pass
        if prev == size:
            return
        import tempfile
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"window": size, "at": int(time.time())}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def observed_window(path=None):
    """The last window the client reported, or None."""
    path = path or _runtime("observed-window.json")
    try:
        with open(path, encoding="utf-8") as fh:
            n = (json.load(fh) or {}).get("window")
        return n if isinstance(n, int) and n > 0 else None
    except (OSError, ValueError):
        return None


def model_window():
    """The ceiling every other window setting is clamped to. (n, source).

    Resolution order, most trustworthy first:

    1. What the client reported about itself (see remember_window). It cannot be
       wrong about its own window and it stays correct across releases.
    2. DISABLE_COMPACT + CLAUDE_CODE_MAX_CONTEXT_TOKENS. On several models the
       ceiling is only raised when compaction is disabled; CLAUDE_CODE_MAX_
       CONTEXT_TOKENS on its own is ignored for this purpose.
    3. A conservative default.

    Why this ordering matters: `autoCompactWindow` in settings.json is clamped
    to this ceiling, silently. A file asking for 1,000,000 against a 200,000
    ceiling yields 200,000 -- and a compaction trigger of 167,000 -- with no
    warning anywhere in the UI. See docs/HOW-IT-WORKS.md, and verify it on your
    own machine with `session_weight.py --explain`.
    """
    live = observed_window()
    if live:
        return live, "client-reported"
    if _truthy(_env("DISABLE_COMPACT")):
        n = _parse_window(_env("CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
        if n:
            return n, "DISABLE_COMPACT+CLAUDE_CODE_MAX_CONTEXT_TOKENS"
    return DEFAULT_WINDOW, "default"


def auto_compact_window():
    """The window the client will actually use. (window, source).

    Resolution order, matching what the client does:
        CLAUDE_CODE_AUTO_COMPACT_WINDOW -> settings autoCompactWindow -> ceiling
    with every branch clamped to model_window() -- and THAT is the part people
    lose days to. Asking for a larger window than the ceiling does not raise the
    ceiling; it is floored silently, with no warning in the UI.

    When the clamp bites, the source is reported as "settings CLAMPED to ..." so
    callers can surface it. That single string is the difference between an
    afternoon of confusion and a one-line diagnosis.
    """
    ceiling, csource = model_window()

    raw = _env("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if raw:
        parsed = _parse_window(raw)
        if parsed:
            return min(ceiling, parsed), "env"

    val, where = _setting("autoCompactWindow")
    parsed = _parse_window(val)
    if parsed:
        clamped = min(ceiling, parsed)
        if clamped < parsed:
            return clamped, f"settings CLAMPED to {csource}"
        return clamped, "settings"

    return ceiling, csource


def _parse_window(value):
    """Accept 1000000, "1M", "500k", "200" (k shorthand). None if unusable."""
    if isinstance(value, (int, float)):
        n = int(value)
    elif isinstance(value, str):
        t = value.strip().lower()
        if not t or t == "auto":
            return None
        try:
            if t.endswith("m"):
                n = int(float(t[:-1]) * 1_000_000)
            elif t.endswith("k"):
                n = int(float(t[:-1]) * 1_000)
            else:
                n = int(float(t))
                if 100 <= n <= 1000:
                    n *= 1000
        except ValueError:
            return None
    else:
        return None
    return n if 100_000 <= n <= 1_000_000 else None


def compact_threshold(window=None, live_window=None):
    """The token count that ENDS this session, whatever ends it. (int)

    There are two different walls, and which one you are running at depends on
    whether auto-compaction is enabled:

        compaction fires at  window  - output_reserve - summary_buffer
        sending is refused at ceiling - output_reserve - blocked_reserve

    With compaction ON the session ends at the first: history is replaced by a
    summary and work continues in a diminished session. With compaction OFF it
    ends at the second: the client refuses to send and nothing continues at all.

    Either way this returns the number the context fraction must be measured
    against, because either way it is the point past which no more work happens
    in this session.

    Disabling compaction does not remove the deadline -- it moves it much later
    and changes the consequence from "your history is silently destroyed" to
    "the next turn is refused". A refusal is survivable: you hand off and carry
    on. A destroyed history is not. That is the whole trade, and it is only safe
    because this function tells the handoff where the wall is.

    live_window: the window the client reported for itself this render. It
    overrides all local inference -- a running process cannot be wrong about its
    own window, and preferring it keeps this correct across releases that move
    the arithmetic.
    """
    enabled, _why = compact_enabled()
    if window is None:
        window, _ = auto_compact_window()
    if isinstance(live_window, int) and live_window > 0:
        window = min(window, live_window) if enabled else live_window

    if enabled:
        limit = window - OUTPUT_RESERVE - SUMMARY_BUFFER
        pct = _env("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
        if pct:
            try:
                p = float(pct)
                if 0 < p <= 100:
                    limit = min(int(window * (p / 100.0)), limit)
            except ValueError:
                pass
    else:
        if isinstance(live_window, int) and live_window > 0:
            ceiling = live_window
        else:
            ceiling, _ = model_window()
        limit = ceiling - OUTPUT_RESERVE - BLOCKED_RESERVE
        blk = _env("CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE")
        if blk:
            try:
                n = int(float(blk))
                if n > 0:
                    limit = n
            except ValueError:
                pass

    return max(limit, 1)


def observed_trigger(sig):
    """Where compaction ACTUALLY fired in this session, from evidence. (n|None)

    Every compaction leaves a typed row recording `preTokens` -- the context size
    at the moment it fired. That is not an estimate of the trigger, it IS the
    trigger, observed. If a release changes the reserves, this notices without
    anyone editing a constant.

    The largest observed value is used: compaction fires at or just above the
    trigger, so the maximum is the tightest lower bound the evidence supports.

    Returns None when the session has never been compacted, which is the common
    case and the reason the arithmetic below still has to exist.
    """
    n = (sig or {}).get("max_pre_tokens")
    return n if isinstance(n, int) and n > 0 else None


def limit_kind():
    """What happens at compact_threshold(): 'compact' or 'blocked'."""
    enabled, why = compact_enabled()
    return ("compact" if enabled else "blocked"), why


# ------------------------------------------------------------------ measure
def measure(transcript_path):
    """Parse a transcript once and return every weight signal.

    Trap 1: working time is the SUM of inter-row gaps under IDLE_GAP_SECONDS,
    never MAX-MIN. A session left open overnight reads as 44h of span and 11h
    of work; scoring the span fires the handoff on an idle session.

    Trap 2: compaction is counted from the TYPED compact_boundary row and its
    cumulativeDroppedTokens field -- never from a marker string, which appears
    verbatim inside tool output whenever anyone greps for it.
    """
    sig = {
        "rows": 0, "assistant": 0, "user": 0, "tool_calls": 0,
        "ctx_tokens": 0, "ctx_max": 0, "compactions": 0, "dropped_tokens": 0,
        "max_pre_tokens": 0,
        "active_hours": 0.0, "span_hours": 0.0, "title": None, "model": None,
        "started": None, "last_activity": None, "transcript": transcript_path,
        "bytes": 0, "tool_census": {},
    }
    if not transcript_path or not os.path.exists(transcript_path):
        return sig

    try:
        sig["bytes"] = os.path.getsize(transcript_path)
    except OSError:
        pass

    stamps = []
    census = {}
    try:
        fh = open(transcript_path, encoding="utf-8", errors="replace")
    except OSError:
        return sig

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            sig["rows"] += 1
            try:
                row = json.loads(line)
            except ValueError:
                continue

            t = _ts(row.get("timestamp"))
            if t:
                stamps.append(t)

            kind = row.get("type")

            if kind == "assistant":
                sig["assistant"] += 1
                msg = row.get("message") or {}
                if msg.get("model"):
                    sig["model"] = msg["model"]
                for block in msg.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        sig["tool_calls"] += 1
                        name = block.get("name") or "?"
                        census[name] = census.get(name, 0) + 1
                usage = msg.get("usage") or {}
                if usage.get("input_tokens") is not None:
                    total = ((usage.get("input_tokens") or 0)
                             + (usage.get("cache_read_input_tokens") or 0)
                             + (usage.get("cache_creation_input_tokens") or 0)
                             + (usage.get("output_tokens") or 0))
                    sig["ctx_tokens"] = total          # last wins = current
                    sig["ctx_max"] = max(sig["ctx_max"], total)

            elif kind == "user":
                msg = row.get("message") or {}
                content = msg.get("content")
                is_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content)
                if not is_result and not row.get("isMeta") \
                        and not row.get("isCompactSummary"):
                    sig["user"] += 1

            elif kind == "system" and row.get("subtype") == "compact_boundary":
                sig["compactions"] += 1
                meta = row.get("compactMetadata") or {}
                dropped = meta.get("cumulativeDroppedTokens")
                if isinstance(dropped, int):
                    sig["dropped_tokens"] = max(sig["dropped_tokens"], dropped)
                # preTokens is the context size at the instant compaction fired,
                # i.e. the trigger, observed rather than computed. Kept so
                # observed_trigger() can correct the arithmetic from evidence if
                # a release moves the reserves.
                pre = meta.get("preTokens")
                if isinstance(pre, int) and pre > 0:
                    sig["max_pre_tokens"] = max(sig["max_pre_tokens"], pre)

            elif kind == "ai-title" and row.get("aiTitle"):
                sig["title"] = row["aiTitle"]
            elif kind == "custom-title" and row.get("customTitle"):
                sig["title"] = row["customTitle"]

    if stamps:
        stamps.sort()
        sig["active_hours"] = round(sum(
            b - a for a, b in zip(stamps, stamps[1:])
            if 0 <= b - a < IDLE_GAP_SECONDS) / 3600.0, 2)
        sig["span_hours"] = round((stamps[-1] - stamps[0]) / 3600.0, 2)
        sig["started"] = datetime.fromtimestamp(stamps[0]).strftime("%Y-%m-%d %H:%M")
        sig["last_activity"] = datetime.fromtimestamp(stamps[-1]).strftime("%Y-%m-%d %H:%M")

    sig["tool_census"] = dict(sorted(census.items(), key=lambda kv: -kv[1]))
    return sig


def measure_cached(transcript_path, cache_path=None):
    """measure(), memoised on (size, mtime).

    The status line renders far more often than a transcript changes, and a
    transcript only grows. Measured on a 5.6 MB transcript, whole
    status-line render including Python startup:

        cache MISS (deleted each run)   91, 89, 91 ms   median 91
        cache HIT                       44, 40, 39, 39, 39, 39 ms   median 39
        raw measure() in-process        35, 35, 35 ms   median 35

    So the parse is 35 ms of a 91 ms render and the cache removes essentially
    all of it: 52 ms saved per render, on every render, for one stat() call.

    Keyed on size+mtime so a stale entry is impossible: any append changes both.
    Falls straight through to measure() on any cache trouble. A corrupt cache
    must cost accuracy nothing.
    """
    cache_path = cache_path or _runtime("session-weight-cache.json")
    try:
        st = os.stat(transcript_path)
        key = f"{transcript_path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return measure(transcript_path)

    try:
        with open(cache_path, encoding="utf-8") as fh:
            entry = json.load(fh)
        if isinstance(entry, dict) and entry.get("key") == key:
            sig = entry.get("sig")
            if isinstance(sig, dict) and "ctx_tokens" in sig:
                return sig
    except (OSError, ValueError):
        pass

    sig = measure(transcript_path)
    try:
        import tempfile
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cache_path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": key, "sig": sig}, fh, ensure_ascii=False)
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return sig


# -------------------------------------------------------------------- score
def score(sig, window=None, ctx_override=None, live_window=None):
    """Turn signals into (score, reasons, urgency, extras).

    ctx_override lets a live caller (statusline / hook) supply a context
    reading fresher than the transcript, which lags the current turn.
    live_window lets it supply the CLI's own resolved window, which beats
    anything this module can infer. See compact_threshold().
    """
    if window is None:
        window, wsource = auto_compact_window()
    else:
        wsource = "given"
    if isinstance(live_window, int) and live_window > 0 and live_window != window:
        window, wsource = live_window, "client-reported"
    thresh = compact_threshold(window, live_window=live_window)
    kind, why = limit_kind()

    # Evidence beats arithmetic. If compaction has fired in THIS session, its
    # preTokens is the trigger -- observed, not derived -- so trust it over the
    # computed value whenever the two disagree by more than rounding. This is
    # what keeps the tool honest across releases that move the reserves.
    seen = observed_trigger(sig)
    if kind == "compact" and seen and abs(seen - thresh) > 2000:
        thresh, wsource = seen, wsource + " (corrected from observed preTokens)"

    ctx = ctx_override if ctx_override else sig.get("ctx_tokens") or 0
    pct = (ctx / thresh) if thresh else 0.0

    reasons = []
    pts = 0

    wall = ("auto-compact trigger" if kind == "compact"
            else "hard send-refusal wall (no /compact escape)")

    if pct >= CTX_HARD_PCT:
        pts += 2
        reasons.append(
            f"context {ctx:,} tok = {pct*100:.0f}% of the {thresh:,} {wall} "
            f"({window:,} window, source {wsource})")
    elif pct >= CTX_SOFT_PCT:
        pts += 1
        reasons.append(f"context {ctx:,} tok = {pct*100:.0f}% of the {thresh:,} {wall}")

    if sig["assistant"] >= ASSISTANT_THRESHOLD:
        pts += 1
        reasons.append(f"{sig['assistant']} assistant rows (>= {ASSISTANT_THRESHOLD})")

    if sig["tool_calls"] >= TOOLCALL_THRESHOLD:
        pts += 1
        reasons.append(f"{sig['tool_calls']} tool calls (>= {TOOLCALL_THRESHOLD})")

    if sig["active_hours"] >= HOURS_THRESHOLD:
        pts += 1
        reasons.append(
            f"{sig['active_hours']:.1f}h ACTIVE working time "
            f"(>= {HOURS_THRESHOLD}h; wall-clock span was {sig['span_hours']:.1f}h)")

    if sig["compactions"]:
        pts += 2
        reasons.append(
            f"already auto-compacted {sig['compactions']}x, "
            f"{sig['dropped_tokens']:,} tokens dropped and unreferenced")

    urgency = "none"
    if pct >= CTX_CRITICAL_PCT:
        urgency = "critical"
    elif pts >= SCORE_TO_FIRE or pct >= CTX_HARD_PCT:
        urgency = "due"
    elif pts == SCORE_TO_FIRE - 1 or pct >= CTX_SOFT_PCT:
        urgency = "soon"

    # THE GATE. Context has the only vote on whether to move; everything else
    # only sharpens the urgency once context says it is time. See CTX_GATE_PCT.
    gated = False
    if pct < CTX_GATE_PCT and urgency in ("due", "critical"):
        gated = True
        urgency = "soon"
        reasons.append(
            f"GATED: {pct*100:.1f}% of the trigger leaves {max(thresh-ctx,0):,} "
            f"tokens of paid-for headroom. Other signals tripped, but moving now "
            f"would abandon that headroom to save nothing.")

    return pts, reasons, urgency, {
        "window": window, "window_source": wsource, "threshold": thresh,
        "ctx": ctx, "pct": round(pct, 4),
        "headroom": max(thresh - ctx, 0),
        "gated": gated,
        "limit_kind": kind, "limit_why": why,
    }


# --------------------------------------------------------------- anti-nag
def load_state(path=None):
    path = path or _runtime("session-weight-watch.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, path=None):
    """Atomic write; never raises."""
    import tempfile
    path = path or _runtime("session-weight-watch.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def tier_of(sig, extras):
    """A monotone tier. Crossing into a new tier re-arms one offer.

    Two axes, so either kind of growth re-arms it: more turns, or more context.
    Turn count alone is not enough -- context is the signal that actually decides
    whether a handoff is needed, so a session that grows heavy without adding
    turns must still be able to re-ask.
    """
    return (sig["assistant"] // TIER_ROWS) + int(extras["pct"] * TIER_PCT_BANDS)


def should_notify(session_id, sig, extras, urgency, now=None):
    """One offer per tier per session, plus a hard cooldown. Fails open."""
    now = now or time.time()
    if urgency in ("none", "soon"):
        return False, None
    state = load_state()
    entry = state.get(session_id) or {}
    tier = tier_of(sig, extras)
    if urgency != "critical":
        if entry.get("tier") is not None and tier <= entry["tier"]:
            return False, None
        if now - (entry.get("at") or 0) < REOFFER_COOLDOWN_S:
            return False, None
    state[session_id] = {
        "at": int(now), "tier": tier, "urgency": urgency,
        "signals": {
            "assistant": sig["assistant"], "tool_calls": sig["tool_calls"],
            "active_hours": sig["active_hours"], "span_hours": sig["span_hours"],
            "ctx": extras["ctx"], "pct": extras["pct"],
            "compactions": sig["compactions"],
        },
    }
    save_state(state)
    return True, tier


# --------------------------------------------------------------------- CLI
def report(sig, pts, reasons, urgency, extras):
    L = []
    L.append("SESSION WEIGHT")
    L.append(f"  transcript      {sig['transcript']}")
    L.append(f"  title           {sig['title'] or '(none yet)'}")
    L.append(f"  model           {sig['model'] or '?'}")
    L.append(f"  rows            {sig['rows']:,}  ({sig['bytes']:,} bytes on disk)")
    L.append(f"  assistant       {sig['assistant']:,}   threshold {ASSISTANT_THRESHOLD}")
    L.append(f"  real user msgs  {sig['user']:,}")
    L.append(f"  tool calls      {sig['tool_calls']:,}   threshold {TOOLCALL_THRESHOLD}")
    L.append(f"  ACTIVE time     {sig['active_hours']:.2f}h   threshold {HOURS_THRESHOLD}h")
    L.append(f"  wall-clock span {sig['span_hours']:.2f}h   (reported, never scored)")
    L.append(f"  context now     {extras['ctx']:,} tok")
    L.append(f"  ends at         {extras['threshold']:,} tok "
             f"({extras['limit_kind'].upper()}: "
             f"{'auto-compact fires' if extras['limit_kind'] == 'compact' else 'client refuses to send'}"
             f", {extras['limit_why']})")
    L.append(f"  window          {extras['window']:,} (source {extras['window_source']})")
    L.append(f"  used            {extras['pct']*100:.1f}% of the wall, "
             f"{extras['headroom']:,} tok headroom")
    L.append(f"  compactions     {sig['compactions']}  "
             f"({sig['dropped_tokens']:,} tokens dropped)")
    if extras.get("gated"):
        L.append(f"  GATE            below {CTX_GATE_PCT*100:.0f}% of the wall -> "
                 f"urgency capped at 'soon' whatever else tripped")
    if sig["tool_census"]:
        top = ", ".join(f"{k} {v}" for k, v in list(sig["tool_census"].items())[:8])
        L.append(f"  top tools       {top}")
    L.append("")
    L.append(f"  SCORE {pts} / {SCORE_TO_FIRE} to fire        URGENCY: {urgency.upper()}")
    for r in reasons:
        L.append(f"    - {r}")
    if not reasons:
        L.append("    - nothing tripped")
    return "\n".join(L)


def explain():
    """Print the wall arithmetic for THIS machine, with every input named.

    The point of this subcommand is that nobody should take the README's word
    for any of it. It shows where each number came from, so a reader can see at
    a glance whether their window setting is being honoured or floored.
    """
    L = []
    enabled, why = compact_enabled()
    live = observed_window()
    ceiling, csource = model_window()
    window, wsource = auto_compact_window()
    thresh = compact_threshold()
    kind, _ = limit_kind()

    L.append("AUTO-COMPACTION")
    L.append(f"  enabled          {enabled}   ({why})")
    L.append("")
    L.append("WINDOW")
    # Built outside the f-string on purpose: a nested same-quote expression
    # spanning lines is PEP 701 syntax and only parses on Python 3.12+.
    live_txt = (format(live, ",") if live
                else "(not seen yet -- render the status line once)")
    L.append(f"  client reported  {live_txt}")
    L.append(f"  ceiling          {ceiling:,}   (source {csource})")
    L.append(f"  resolved         {window:,}   (source {wsource})")
    if "CLAMPED" in wsource:
        asked, _ = _setting("autoCompactWindow")
        L.append(f"  !! autoCompactWindow asks for {asked}, but the ceiling is "
                 f"{ceiling:,}, so it was floored. Nothing in the UI says so.")
    L.append("")
    L.append("WALL -- the token count past which no more work happens here")
    if enabled:
        L.append(f"  {window:,} window - {OUTPUT_RESERVE:,} reply reserve "
                 f"- {SUMMARY_BUFFER:,} summary buffer")
        L.append(f"  = {thresh:,}   then AUTO-COMPACTION FIRES "
                 f"(history replaced by a summary)")
    else:
        L.append(f"  {ceiling:,} ceiling - {OUTPUT_RESERVE:,} reply reserve "
                 f"- {BLOCKED_RESERVE:,} margin")
        L.append(f"  = {thresh:,}   then SENDING IS REFUSED "
                 f"(no summary; a handoff is the only exit)")
    L.append("")
    L.append("HANDOFF THRESHOLDS")
    L.append(f"  gate    {CTX_GATE_PCT*100:.0f}%  {int(thresh*CTX_GATE_PCT):,} tok"
             f"   nothing is offered below this, whatever else trips")
    L.append(f"  due     {CTX_HARD_PCT*100:.0f}%  {int(thresh*CTX_HARD_PCT):,} tok"
             f"   hand off now")
    L.append(f"  urgent  {CTX_CRITICAL_PCT*100:.0f}%  "
             f"{int(thresh*CTX_CRITICAL_PCT):,} tok   stop asking, act")
    L.append("")
    L.append("VERIFY THIS YOURSELF")
    L.append("  Compactions leave a typed row in the transcript. To see where "
             "yours fired:")
    L.append("    python session_weight.py --session-id <uuid> --json")
    L.append("  and compare 'compactions' and 'dropped_tokens' against the wall "
             "above.")
    return "\n".join(L)


def main(argv):
    if "--explain" in argv:
        print(explain())
        return 0
    sid = None
    path = None
    as_json = "--json" in argv
    for i, a in enumerate(argv):
        if a in ("--session-id", "-s") and i + 1 < len(argv):
            sid = argv[i + 1]
        elif a in ("--transcript", "-t") and i + 1 < len(argv):
            path = argv[i + 1]
    if not path:
        path = find_transcript(sid) if sid else None
    if not path:
        sys.stderr.write("usage: session_weight.py --explain\n"
                         "       session_weight.py --session-id <uuid> [--json]\n"
                         "       session_weight.py --transcript <path.jsonl> [--json]\n")
        return 2
    sig = measure(path)
    pts, reasons, urgency, extras = score(sig)
    if as_json:
        print(json.dumps({"signals": sig, "score": pts, "reasons": reasons,
                          "urgency": urgency, "context": extras},
                         ensure_ascii=False, indent=1))
    else:
        print(report(sig, pts, reasons, urgency, extras))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:          # never break a caller
        sys.stderr.write(f"session_weight: {exc}\n")
        sys.exit(0)
