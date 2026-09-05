#!/usr/bin/env python3
"""session-weight-watch — the self-firing half of long-session-handoff.

Registered on four hook events. Reads the event from stdin, measures the
session, and injects a notice into the model's context when the session has
become heavy enough that continuing in it costs more than moving.

  UserPromptSubmit  every prompt: measure and offer, at most once per tier
  SessionStart      resume/fork/compact: the payload carries context_tokens,
                    which is fresher than the transcript on disk
  PreCompact        last chance: dump the parent before context is destroyed
  PostCompact       compaction already fired: say so loudly, point at the rows

Design rules, all of them learned the hard way:
  * fail open. A hook that throws blocks a turn. Everything is wrapped.
  * never score wall-clock span; score active time (see session_weight.py).
  * never fire from inside a subagent: agent_id present -> stay silent.
  * one offer per tier per session, so this is a signal and not a nag.
"""
from __future__ import annotations

import json
import os
import sys
import time

CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude"))
HOME = os.path.expanduser("~")
SKILL_DIR = os.path.join(CLAUDE_DIR, "skills", "long-session-handoff")
SCRIPTS = os.path.join(SKILL_DIR, "scripts")
sys.path.insert(0, SCRIPTS)

LOG = os.path.join(CLAUDE_DIR, "runtime", "session-weight-watch.log")


def log(msg):
    """Best effort. Never let logging break a turn."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def emit(event, context=None, system_message=None):
    """Write the one JSON object Claude Code expects, then exit 0.

    stdout is forced to UTF-8: on Windows it otherwise inherits the console
    codepage, which turns every em dash in these notices into a replacement
    character -- or raises UnicodeEncodeError mid-write and truncates the JSON.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    out = {"suppressOutput": True}
    if context:
        out["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": context,
        }
    if system_message:
        out["systemMessage"] = system_message
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    sys.exit(0)


def quiet():
    sys.stdout.write(json.dumps({"suppressOutput": True}))
    sys.exit(0)


# ------------------------------------------------------------------- notices
def notice_offer(sig, pts, reasons, urgency, extras, sid):
    """The block the model sees. Written as an instruction, not a suggestion."""
    head = ("[SESSION WEIGHT — runtime measurement, not a user message]"
            if urgency != "critical" else
            "[SESSION WEIGHT — CRITICAL, runtime measurement, not a user message]")
    lines = [head, ""]
    lines.append(f"score {pts}, urgency {urgency}. Measured signals:")
    for r in reasons:
        lines.append(f"  - {r}")
    lines.append("")
    wall = ("auto-compact trigger" if extras.get("limit_kind") == "compact"
            else "hard send-refusal wall — /compact is disabled too, so a "
                 "handoff is the ONLY exit")
    lines.append(f"  context   {extras['ctx']:,} / {extras['threshold']:,} tok "
                 f"({extras['pct']*100:.0f}% of the {wall}, "
                 f"{extras['headroom']:,} left)")
    lines.append(f"  rows      {sig['assistant']:,} assistant, "
                 f"{sig['tool_calls']:,} tool calls, "
                 f"{sig['user']:,} real user messages")
    lines.append(f"  time      {sig['active_hours']:.1f}h active "
                 f"(span {sig['span_hours']:.1f}h — span is not working time)")
    if sig["compactions"]:
        lines.append(f"  LOST      auto-compaction already fired "
                     f"{sig['compactions']}x and discarded "
                     f"{sig['dropped_tokens']:,} tokens from context")
    lines.append("")
    if urgency == "critical":
        if extras.get("limit_kind") == "blocked":
            lines.append("ACTION REQUIRED THIS TURN, BEFORE ANYTHING ELSE: this session is")
            lines.append("about to hit the hard send-refusal wall. Auto-compaction is")
            lines.append("disabled, so there is no summary to fall back on — the next turn")
            lines.append("simply will not be accepted. Load the `long-session-handoff`")
            lines.append("skill and run the handoff now. Do not ask permission first unless")
            lines.append("the user's last message forbade it — dump the parent, write the")
            lines.append("brief, then tell the user.")
        else:
            lines.append("ACTION REQUIRED THIS TURN, BEFORE ANYTHING ELSE: auto-compaction")
            lines.append("is about to fire and will discard most of this context. Load the")
            lines.append("`long-session-handoff` skill and run the handoff now. Do not ask")
            lines.append("permission first unless the user's last message forbade it —")
            lines.append("dump the parent, write the brief, then tell the user.")
    else:
        lines.append("ACTION REQUIRED THIS TURN: finish answering the user normally,")
        lines.append("then load the `long-session-handoff` skill and follow it —")
        lines.append("measure, then ask permission with ONE AskUserQuestion call")
        lines.append("(hand off now / keep going / stop asking). Do not start the")
        lines.append("handoff before the user answers, and do not ask twice.")
    lines.append("")
    lines.append(f"Session id: {sid}")
    lines.append(f"Transcript: {sig['transcript']}")
    return "\n".join(lines)


def notice_precompact(sig, extras, sid, trigger):
    return "\n".join([
        "[AUTO-COMPACTION IS ABOUT TO FIRE — runtime event, not a user message]",
        "",
        f"trigger: {trigger}. About to discard most of "
        f"{extras['ctx']:,} tokens of context and replace it with a summary.",
        "",
        "The raw rows survive on disk at:",
        f"  {sig['transcript']}",
        "",
        "After this turn, treat the session as handed-off-by-force: load the",
        "`long-session-handoff` skill, dump this transcript, and read the dump",
        "instead of trusting the compaction summary. The summary is a lossy",
        "index; the transcript is the evidence.",
        "",
        f"Session id: {sid}",
    ])


def notice_postcompact(sig, extras, sid, summary_len):
    return "\n".join([
        "[AUTO-COMPACTION HAS ALREADY FIRED — runtime event, not a user message]",
        "",
        f"{sig['dropped_tokens']:,} tokens were discarded across "
        f"{sig['compactions']} compaction(s) and replaced by a "
        f"~{summary_len // 4:,}-token summary.",
        "",
        "You are now working from a summary, not from the conversation. Anything",
        "not in that summary is gone from context but still on disk:",
        f"  {sig['transcript']}",
        "",
        "Do this before continuing substantive work: load the",
        "`long-session-handoff` skill, run scripts/dump_parent_session.py against",
        "the transcript above, and read 01-user-messages.md IN FULL. That file is",
        "every requirement the user ever stated, including the ones the summary",
        "dropped. Then hand off to a fresh session rather than compacting again.",
        "",
        f"Session id: {sid}",
    ])


def notice_resume(sig, extras, sid, payload):
    """SessionStart(resume|fork) carries cost fields the transcript lacks."""
    lines = ["[RESUMED SESSION — runtime measurement, not a user message]", ""]
    ctx = payload.get("context_tokens")
    if isinstance(ctx, int):
        lines.append(f"resumed with {ctx:,} tokens of context already in the window "
                     f"({extras['pct']*100:.0f}% of the "
                     f"{extras['threshold']:,} compact trigger)")
    secs = payload.get("seconds_since_last_response")
    if isinstance(secs, (int, float)):
        lines.append(f"idle for {secs / 3600:.1f}h before this resume")
    if payload.get("prompt_cache_likely_expired"):
        usd = payload.get("estimated_cache_write_usd")
        cost = f" (~${usd:.2f} to re-cache)" if isinstance(usd, (int, float)) else ""
        lines.append(f"the prompt cache has expired, so the first request "
                     f"re-sends the whole window{cost}")
    lines.append("")
    lines.append("This is the cheapest moment in the session to decide: a handoff")
    lines.append("now costs one dump; a handoff in an hour costs the same dump plus")
    lines.append("everything added in between. Load `long-session-handoff` and offer")
    lines.append("it with ONE AskUserQuestion call after you answer.")
    lines.append("")
    lines.append(f"Session id: {sid}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- main
def run(payload):
    import session_weight as sw

    event = payload.get("hook_event_name") or ""
    sid = payload.get("session_id") or ""

    # Never fire inside a subagent: it has its own short context and cannot
    # hand anything off. agent_id is the documented discriminator.
    if payload.get("agent_id"):
        quiet()

    # Never fire for machine-injected turns; only a human can accept an offer.
    if event == "UserPromptSubmit" and payload.get("source") not in (None, "user"):
        quiet()

    path = payload.get("transcript_path")
    if not path or not os.path.exists(path):
        path = sw.find_transcript(sid)
    if not path:
        quiet()

    sig = sw.measure(path)

    # The transcript is written asynchronously and lags the running turn, so
    # prefer a live reading when the payload has one.
    live = payload.get("context_tokens")
    ctx_override = live if isinstance(live, int) and live > 0 else None
    pts, reasons, urgency, extras = sw.score(sig, ctx_override=ctx_override)

    if event == "PreCompact":
        log(f"{sid} PreCompact trigger={payload.get('trigger')} ctx={extras['ctx']}")
        emit(event, notice_precompact(sig, extras, sid, payload.get("trigger")))

    if event == "PostCompact":
        summary = payload.get("compact_summary") or ""
        log(f"{sid} PostCompact dropped={sig['dropped_tokens']} "
            f"summary={len(summary)}")
        # Always speak: this is the one event where context was just destroyed.
        emit(event, notice_postcompact(sig, extras, sid, len(summary)))

    if event == "SessionStart":
        source = payload.get("source")
        if source not in ("resume", "fork", "compact"):
            quiet()
        fire, tier = sw.should_notify(sid, sig, extras, urgency)
        if not fire:
            quiet()
        log(f"{sid} SessionStart/{source} tier={tier} urgency={urgency} "
            f"ctx={extras['ctx']}")
        emit(event, notice_resume(sig, extras, sid, payload))

    # UserPromptSubmit
    fire, tier = sw.should_notify(sid, sig, extras, urgency)
    if not fire:
        quiet()
    log(f"{sid} offer tier={tier} score={pts} urgency={urgency} "
        f"ctx={extras['ctx']}/{extras['threshold']} "
        f"asst={sig['assistant']} tools={sig['tool_calls']} "
        f"active={sig['active_hours']}h")
    emit(event,
         notice_offer(sig, pts, reasons, urgency, extras, sid),
         system_message=(
             f"Session weight: {extras['pct']*100:.0f}% of the "
             f"{'auto-compact trigger' if extras.get('limit_kind') == 'compact' else 'send-refusal wall'}"
             f", {sig['active_hours']:.1f}h active. Handoff offered."))


def main():
    """Fail open, always. A weight watcher that blocks turns is worse than no
    weight watcher at all, so every failure path ends in quiet()."""
    try:
        raw = sys.stdin.read()
    except Exception:
        quiet()
    if not raw or not raw.strip():
        quiet()
    try:
        payload = json.loads(raw)
    except Exception as exc:
        log(f"unparseable stdin ({exc}): {raw[:200]!r}")
        quiet()
    if not isinstance(payload, dict):
        quiet()
    try:
        run(payload)
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        log(f"ERROR {payload.get('hook_event_name')} "
            f"{payload.get('session_id')}: {exc}")
        log(traceback.format_exc().replace("\n", " | "))
        quiet()
    quiet()


if __name__ == "__main__":
    main()
