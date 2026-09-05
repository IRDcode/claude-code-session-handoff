#!/usr/bin/env python3
"""statusline-weight.py -- show the session's weight on every render.

Claude Code pipes a JSON blob to the status line command on stdin. The fields
this reads, all documented in the status-line reference:

    context_window.{used, used_percentage, remaining, context_window_size}
    exceeds_200k_tokens
    prompt_cache.{warm, hit_ratio, cache_write_tokens}
    session_id, transcript_path, model.display_name, cost.total_lines_*

Every field is read defensively: a status line that crashes shows nothing, and
nothing looks identical to broken.

Why not just print `used_percentage`: it is a percentage of the WINDOW. The
number that decides whether work can continue here is the percentage of the
WALL, which is either

    window  - output_reserve - summary_buffer   (auto-compaction fires), or
    ceiling - output_reserve - blocked_reserve  (the client refuses to send)

and can be far below the window. A bar reading 20% of a 1M window can be at
100% of a 200k trigger. That exact misreading is why this project exists: a
session was compacted three times at 167k while its settings file claimed a
1,000,000-token window.

Output is one line, ANSI-coloured, no trailing newline.
"""
from __future__ import annotations

import json
import os
import sys

CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude"))
HOME = os.path.expanduser("~")
sys.path.insert(0, os.path.join(CLAUDE_DIR, "skills",
                                "long-session-handoff", "scripts"))

DIM = "\x1b[2m"
RESET = "\x1b[0m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
BOLD_RED = "\x1b[1;31m"
CYAN = "\x1b[36m"


def bar(frac, width=10):
    """A block bar. Reads at a glance; the number is for when it matters."""
    frac = max(0.0, min(1.0, frac))
    full = int(frac * width)
    return "█" * full + "░" * (width - full)


def colour_for(pct):
    if pct >= 0.85:
        return BOLD_RED
    if pct >= 0.62:
        return YELLOW
    return GREEN


def human(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1000:.0f}k"
    return str(n)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    cw = payload.get("context_window") or {}
    used = cw.get("used")
    sid = payload.get("session_id") or ""
    tpath = payload.get("transcript_path")

    import session_weight as sw

    # The client tells us its own window on every render. Prefer that over
    # anything this code can infer from settings: a running process cannot be
    # wrong about its own window, and preferring it keeps the wall correct across
    # releases. Cache it so the CLI tools, which get no such payload, agree.
    live_window = cw.get("context_window_size")
    if not (isinstance(live_window, int) and live_window > 0):
        live_window = None
    else:
        sw.remember_window(live_window)

    window, wsource = sw.auto_compact_window()
    if live_window and live_window != window:
        window, wsource = live_window, "client-reported"
    thresh = sw.compact_threshold(window, live_window=live_window)
    kind, why = sw.limit_kind()

    # The live `used` is authoritative for context; the transcript is the only
    # source for turns, active time and compactions. Read both, cheaply --
    # measure_cached() keys on (size, mtime), so an unchanged transcript costs
    # a JSON load instead of a 25 ms reparse.
    path = tpath if (tpath and os.path.exists(tpath)) else sw.find_transcript(sid)
    sig = None
    if path:
        try:
            sig = sw.measure_cached(path)
        except Exception:
            sig = None
    if not isinstance(used, int) or used <= 0:
        used = (sig or {}).get("ctx_tokens") or 0

    pct = (used / thresh) if thresh else 0.0
    col = colour_for(pct)
    headroom = max(thresh - used, 0)

    parts = []

    model = (payload.get("model") or {}).get("display_name")
    if model:
        parts.append(f"{CYAN}{model}{RESET}")

    parts.append(f"{col}{bar(pct)} {pct*100:.0f}%{RESET}"
                 f"{DIM} {human(used)}/{human(thresh)}{RESET}")

    if sig:
        parts.append(f"{DIM}{sig['assistant']}t {sig['tool_calls']}tc "
                     f"{sig['active_hours']:.1f}h{RESET}")
        if sig["compactions"]:
            parts.append(f"{RED}compacted {sig['compactions']}x "
                         f"-{human(sig['dropped_tokens'])}{RESET}")

    cache = payload.get("prompt_cache") or {}
    if cache.get("warm") is False:
        parts.append(f"{YELLOW}cache cold{RESET}")

    # The verdict, last, where the eye lands. Score needs the transcript; when
    # it is unreadable fall back to the context fraction alone.
    if sig:
        try:
            pts, _, urgency, _ = sw.score(sig, window=window, ctx_override=used,
                                          live_window=live_window)
        except Exception:
            pts, urgency = 0, "none"
    else:
        pts = 2 if pct >= sw.CTX_HARD_PCT else (1 if pct >= sw.CTX_SOFT_PCT else 0)
        urgency = ("critical" if pct >= sw.CTX_CRITICAL_PCT
                   else "due" if pct >= sw.CTX_HARD_PCT
                   else "soon" if pct >= sw.CTX_SOFT_PCT else "none")

    if urgency == "critical":
        parts.append(f"{BOLD_RED}HANDOFF NOW{RESET}")
    elif urgency == "due":
        parts.append(f"{RED}HANDOFF DUE ({pts}){RESET}")
    elif urgency == "soon":
        parts.append(f"{YELLOW}handoff soon ({pts}){RESET}")
    else:
        parts.append(f"{DIM}{human(headroom)} free{RESET}")

    if "CLAMPED" in wsource:
        # The failure this project was built after: autoCompactWindow IS set,
        # and the client silently floored it to the ceiling. Loudest marker on
        # the line, because nothing else in the UI mentions it.
        parts.append(f"{BOLD_RED}window CLAMPED to {human(window)}{RESET}")
    elif kind == "blocked":
        # No auto-compaction: the session ends in a send refusal instead, so the
        # handoff is the only exit. Say so where the eye already is.
        parts.append(f"{DIM}no-compact{RESET}")

    sys.stdout.write(f"{DIM} | {RESET}".join(parts))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.stdout.write(f"\x1b[2mweight: {exc}\x1b[0m")
    sys.exit(0)
