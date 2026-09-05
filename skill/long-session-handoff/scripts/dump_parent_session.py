#!/usr/bin/env python3
"""Dump a Claude Code session's transcript into a readable handoff bundle.

    python dump_parent_session.py <parent_session_id|transcript.jsonl> [out_dir]

Writes five files so the continuation can read the parent IN FULL instead of
trusting a summary:

    01-user-messages.md    every real user message, verbatim, deduped
    02-decisions.md        every substantial assistant message
    03-full-transcript.txt everything, tool payloads clipped
    04-index.json          counts to verify the read against
    05-dropped-context.md  what auto-compaction discarded, if it ever fired

Run this as a SUBPROCESS and read the files from disk. Never pipe a
transcript through the model's context: that is the cost this whole skill
exists to avoid.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime

TOOL_CLIP = 600          # chars of any single tool payload kept in 03
TEXT_CLIP_03 = 4000      # chars of any single text block kept in 03

# Chars for an assistant message to count as a decision. Calibrated, not
# guessed: on a 325-row session only 2 visible text blocks cleared 400 chars,
# because a thinking model says little and thinks a lot (130 thinking blocks,
# 215,683 chars, against 37 text blocks). A 400 floor produced a two-entry
# 02-decisions.md -- a handoff with no "why" in it. 150 keeps 13 of the 37,
# which is every message that actually decided something, and still fits in a
# file the continuation can read start to finish.
DECISION_MIN = 150

CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude"))
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")


# ------------------------------------------------------------------ helpers
def find_transcript(session_id):
    best = None
    try:
        slugs = os.listdir(PROJECTS_DIR)
    except OSError:
        return None
    for slug in slugs:
        cand = os.path.join(PROJECTS_DIR, slug, session_id + ".jsonl")
        try:
            st = os.stat(cand)
        except OSError:
            continue
        if best is None or st.st_mtime > best[0]:
            best = (st.st_mtime, cand)
    return best[1] if best else None


def blocks_of(row):
    msg = row.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def text_of(row):
    return "\n".join(b.get("text", "") for b in blocks_of(row)
                     if b.get("type") == "text").strip()


def clip(s, n):
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n] + f"… (+{len(s) - n} chars)"


def flatten(value):
    """Any tool payload -> one string, without exploding the file."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for b in value:
            if isinstance(b, dict):
                parts.append(b.get("text") or b.get("content")
                             or json.dumps(b, ensure_ascii=False))
            else:
                parts.append(str(b))
        return "\n".join(str(p) for p in parts)
    if isinstance(value, dict):
        for key in ("stdout", "text", "content", "output", "result"):
            if key in value and value[key]:
                return flatten(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def stamp(row):
    ts = row.get("timestamp")
    if not isinstance(ts, str):
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%m-%d %H:%M")
    except ValueError:
        return ts[:16]


# Slash-command plumbing and local stdout are not user requirements. They are
# echoed into `user` rows verbatim, so they have to be filtered by shape.
_NOISE_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-stdout>", "<local-command-stderr>",
    "<user-prompt-submit-hook>", "<system-reminder>",
)


def is_noise(text):
    t = (text or "").strip()
    if not t:
        return True
    return t.startswith(_NOISE_PREFIXES)


# --------------------------------------------------------------------- read
def load(path):
    """One pass. Returns (rows, meta)."""
    rows = []
    meta = {"title": None, "model": None, "cwd": None, "git_branch": None,
            "session_id": None, "version": None}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rows.append(row)
            if row.get("aiTitle"):
                meta["title"] = row["aiTitle"]
            if row.get("customTitle"):
                meta["title"] = row["customTitle"]
            if row.get("cwd"):
                meta["cwd"] = row["cwd"]
            if row.get("gitBranch"):
                meta["git_branch"] = row["gitBranch"]
            if row.get("sessionId"):
                meta["session_id"] = row["sessionId"]
            if row.get("version"):
                meta["version"] = row["version"]
            if row.get("type") == "assistant":
                m = row.get("message") or {}
                if m.get("model") and m["model"] != "<synthetic>":
                    meta["model"] = m["model"]
    return rows, meta


def classify(rows):
    """Split rows into the buckets the four files need.

    Two Claude-Code-specific traps, both discovered by diffing a dump against
    the session that produced it:

    Trap A -- a message the user sends WHILE a turn is running never becomes a
    `user` row. It is recorded as an `attachment` row whose attachment.type is
    `queued_command`. These are disproportionately the important ones (mid-turn
    corrections, changes of mind), so a dumper that only reads `user` rows
    silently loses exactly the messages it can least afford to lose.

    Trap B -- slash-command plumbing (`<command-name>`, `<local-command-stdout>`)
    is stored as ordinary `user` rows. Copied into 01 verbatim it reads like a
    user requirement. Filter by shape, not by trust.
    """
    out = {"user": [], "assistant": [], "tool_use": [], "tool_result": [],
           "compaction": [], "by_type": {}, "tool_census": {},
           "noise_skipped": 0, "mid_turn": 0}
    for row in rows:
        kind = row.get("type")
        out["by_type"][kind] = out["by_type"].get(kind, 0) + 1

        if kind == "assistant":
            for b in blocks_of(row):
                if b.get("type") == "tool_use":
                    out["tool_use"].append((row, b))
                    n = b.get("name") or "?"
                    out["tool_census"][n] = out["tool_census"].get(n, 0) + 1
            t = text_of(row)
            if t:
                out["assistant"].append((row, t))

        elif kind == "user":
            bl = blocks_of(row)
            results = [b for b in bl if b.get("type") == "tool_result"]
            if results:
                for b in results:
                    out["tool_result"].append((row, b))
                continue
            if row.get("isCompactSummary"):
                out["compaction"].append(("summary", row, text_of(row)))
                continue
            if row.get("isMeta"):
                continue
            t = text_of(row)
            if is_noise(t):
                if t:
                    out["noise_skipped"] += 1
                continue
            out["user"].append((row, t))

        elif kind == "attachment":
            att = row.get("attachment") or {}
            if att.get("type") == "queued_command":
                prompt = (att.get("prompt") or "").strip()
                if prompt and not is_noise(prompt):
                    out["mid_turn"] += 1
                    out["user"].append((row, prompt))

        elif kind == "system" and row.get("subtype") == "compact_boundary":
            out["compaction"].append(("boundary", row, None))
    return out


# -------------------------------------------------------------------- write
def write_01(path, buckets, meta):
    """Every real user message, verbatim, in order, duplicates collapsed."""
    seen = {}
    order = []
    for row, text in buckets["user"]:
        key = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()
        if key in seen:
            seen[key]["count"] += 1
            continue
        seen[key] = {"count": 1, "text": text, "row": row}
        order.append(key)

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# Parent session — every user message\n\n")
        fh.write(f"Session `{meta['session_id']}` — «{meta['title'] or '(untitled)'}»\n\n")
        fh.write(f"{len(buckets['user'])} user messages, "
                 f"{len(order)} unique, "
                 f"{len(buckets['user']) - len(order)} duplicates collapsed.\n")
        if buckets.get("mid_turn"):
            fh.write(f"{buckets['mid_turn']} of them were sent MID-TURN "
                     f"(marked below) — those are usually corrections.\n")
        if buckets.get("noise_skipped"):
            fh.write(f"{buckets['noise_skipped']} slash-command / local-stdout "
                     f"rows were skipped: plumbing, not requirements.\n")
        fh.write("\nRead this file IN FULL. It is the user's own words: every\n"
                 "requirement, correction and standing instruction lives here.\n\n---\n\n")
        for i, key in enumerate(order, 1):
            e = seen[key]
            dup = f"  (repeated {e['count']}x)" if e["count"] > 1 else ""
            mid = "  **[sent mid-turn]**" if e["row"].get("type") == "attachment" else ""
            fh.write(f"## {i:03d} — {stamp(e['row'])}{dup}{mid}\n\n{e['text']}\n\n")
    return len(buckets["user"]), len(order)


def write_02(path, buckets, meta):
    """Substantial assistant messages: the reasoning and the decisions."""
    seen = set()
    kept = []
    for row, text in buckets["assistant"]:
        if len(text) < DECISION_MIN:
            continue
        key = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        kept.append((row, text))

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Parent session — decisions and findings\n\n")
        fh.write(f"{len(kept)} substantial assistant messages "
                 f"(>= {DECISION_MIN} chars) out of "
                 f"{len(buckets['assistant'])} with text.\n\n")
        fh.write("These are conclusions, not gospel: where this file and\n"
                 "`03-full-transcript.txt` disagree, the transcript is right.\n\n"
                 "A thinking model states little and reasons a lot, so the *why*\n"
                 "behind a decision is often not here at all -- it is in the\n"
                 "THINKING blocks of 03. Grep 03 for a term from this file when a\n"
                 "conclusion looks unmotivated.\n\n---\n\n")
        for i, (row, text) in enumerate(kept, 1):
            fh.write(f"## {i:03d} — {stamp(row)}\n\n{text}\n\n")
    return len(kept)


def write_03(path, rows, meta):
    """Everything, in order, tool payloads clipped. This is the ground truth.

    Grep this file. Do not read it start to finish -- it is routinely 1-3 MB,
    which is the whole reason the parent had to be handed off.
    """
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"# Full transcript — session {meta['session_id']}\n")
        fh.write(f"# title: {meta['title']}   model: {meta['model']}\n")
        fh.write(f"# cwd: {meta['cwd']}   branch: {meta['git_branch']}\n")
        fh.write(f"# tool payloads clipped to {TOOL_CLIP} chars, "
                 f"text blocks to {TEXT_CLIP_03}\n")
        fh.write("# GREP THIS FILE. Do not read it top to bottom.\n\n")
        for row in rows:
            kind = row.get("type")
            when = stamp(row)
            if kind == "assistant":
                for b in blocks_of(row):
                    bt = b.get("type")
                    if bt == "text" and b.get("text", "").strip():
                        fh.write(f"\n[{when}] ASSISTANT\n{clip(b['text'], TEXT_CLIP_03)}\n")
                        n += 1
                    elif bt == "thinking" and b.get("thinking"):
                        fh.write(f"\n[{when}] THINKING\n{clip(b['thinking'], TEXT_CLIP_03)}\n")
                        n += 1
                    elif bt == "tool_use":
                        args = json.dumps(b.get("input") or {}, ensure_ascii=False)
                        fh.write(f"\n[{when}] TOOL_USE {b.get('name')} "
                                 f"({b.get('id')})\n  {clip(args, TOOL_CLIP)}\n")
                        n += 1
            elif kind == "user":
                bl = blocks_of(row)
                results = [b for b in bl if b.get("type") == "tool_result"]
                if results:
                    for b in results:
                        payload = flatten(b.get("content"))
                        err = " ERROR" if b.get("is_error") else ""
                        fh.write(f"\n[{when}] TOOL_RESULT{err} "
                                 f"({b.get('tool_use_id')})\n  {clip(payload, TOOL_CLIP)}\n")
                        n += 1
                elif row.get("isCompactSummary"):
                    fh.write(f"\n[{when}] === AUTO-COMPACTION SUMMARY "
                             f"(the model's own summary, NOT the user) ===\n"
                             f"{text_of(row)}\n")
                    n += 1
                else:
                    t = text_of(row)
                    if t:
                        tag = "USER" if not row.get("isMeta") else "USER(meta)"
                        fh.write(f"\n[{when}] {tag}\n{clip(t, TEXT_CLIP_03)}\n")
                        n += 1
            elif kind == "system":
                sub = row.get("subtype")
                if sub == "compact_boundary":
                    cm = row.get("compactMetadata") or {}
                    fh.write(f"\n[{when}] === COMPACT_BOUNDARY trigger="
                             f"{cm.get('trigger')} pre={cm.get('preTokens')} "
                             f"post={cm.get('postTokens')} "
                             f"dropped={cm.get('cumulativeDroppedTokens')} ===\n")
                    n += 1
                elif sub in ("turn_duration", "stop_hook_summary"):
                    fh.write(f"\n[{when}] SYSTEM {sub} "
                             f"{clip(json.dumps({k: v for k, v in row.items() if k in ('durationMs', 'messageCount', 'content')}, ensure_ascii=False), 300)}\n")
                    n += 1
            elif kind == "attachment":
                att = row.get("attachment") or {}
                if att.get("type") == "queued_command":
                    prompt = (att.get("prompt") or "").strip()
                    if prompt:
                        fh.write(f"\n[{when}] USER (sent mid-turn, queued)\n"
                                 f"{clip(prompt, TEXT_CLIP_03)}\n")
                        n += 1
    return n


def write_05(path, buckets, rows):
    """What auto-compaction threw away, and where to go read it.

    A session may already have been compacted before anyone noticed. The raw
    rows are still on disk, just unreferenced by the live context, so nothing is
    actually lost until someone assumes the summary is all there is. Point at
    them.
    """
    events = buckets["compaction"]
    if not events:
        return 0
    uuid_index = {}
    for i, row in enumerate(rows):
        if row.get("uuid"):
            uuid_index[row["uuid"]] = i

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Context dropped by auto-compaction\n\n")
        fh.write("Auto-compaction fired in the parent. The rows below the\n"
                 "boundary were removed from the live context but are STILL IN\n"
                 "`03-full-transcript.txt`. If an answer seems to be missing\n"
                 "from `01`/`02`, it is probably here.\n\n")
        boundary_no = 0
        for kind, row, text in events:
            if kind == "boundary":
                boundary_no += 1
                cm = row.get("compactMetadata") or {}
                seg = cm.get("preservedSegment") or {}
                head = seg.get("headUuid")
                fh.write(f"## Compaction {boundary_no} — {stamp(row)}\n\n")
                fh.write(f"- trigger: `{cm.get('trigger')}`\n")
                fh.write(f"- context before: {cm.get('preTokens'):,} tokens\n"
                         if isinstance(cm.get("preTokens"), int) else "")
                fh.write(f"- context after: {cm.get('postTokens'):,} tokens\n"
                         if isinstance(cm.get("postTokens"), int) else "")
                fh.write(f"- dropped cumulatively: "
                         f"{cm.get('cumulativeDroppedTokens'):,} tokens\n"
                         if isinstance(cm.get("cumulativeDroppedTokens"), int) else "")
                fh.write(f"- stall: {(cm.get('durationMs') or 0) / 1000:.0f}s\n")
                kept = (cm.get("preservedMessages") or {}).get("uuids") or []
                fh.write(f"- messages kept verbatim: {len(kept)}\n")
                if head in uuid_index:
                    fh.write(f"- everything before transcript row "
                             f"~{uuid_index[head]} was summarised away\n")
                fh.write("\n")
            else:
                fh.write("### The summary compaction produced\n\n")
                fh.write("Treat this as a lossy index, not as evidence.\n\n")
                fh.write("```\n" + (text or "") + "\n```\n\n")
    return boundary_no


# --------------------------------------------------------------------- main
def main(argv):
    if not argv:
        sys.stderr.write(__doc__ or "")
        return 2

    target = argv[0]
    if target.endswith(".jsonl") and os.path.exists(target):
        path = target
        sid = os.path.splitext(os.path.basename(target))[0]
    else:
        sid = target
        path = find_transcript(sid)
    if not path or not os.path.exists(path):
        sys.stderr.write(f"no transcript found for {target}\n")
        return 1

    out_dir = argv[1] if len(argv) > 1 else os.path.join(
        ".claude-handoff", f"parent-{sid[:8]}")
    os.makedirs(out_dir, exist_ok=True)

    rows, meta = load(path)
    meta["session_id"] = meta["session_id"] or sid
    buckets = classify(rows)

    p01 = os.path.join(out_dir, "01-user-messages.md")
    p02 = os.path.join(out_dir, "02-decisions.md")
    p03 = os.path.join(out_dir, "03-full-transcript.txt")
    p04 = os.path.join(out_dir, "04-index.json")
    p05 = os.path.join(out_dir, "05-dropped-context.md")

    user_total, user_unique = write_01(p01, buckets, meta)
    decisions = write_02(p02, buckets, meta)
    entries = write_03(p03, rows, meta)
    compactions = write_05(p05, buckets, rows)

    # timestamps: active vs span, the same rule the weight watcher uses
    stamps = []
    for row in rows:
        ts = row.get("timestamp")
        if isinstance(ts, str):
            try:
                stamps.append(datetime.fromisoformat(
                    ts.replace("Z", "+00:00")).timestamp())
            except ValueError:
                pass
    stamps.sort()
    active = sum(b - a for a, b in zip(stamps, stamps[1:])
                 if 0 <= b - a < 600) / 3600 if len(stamps) > 1 else 0.0
    span = (stamps[-1] - stamps[0]) / 3600 if len(stamps) > 1 else 0.0

    index = {
        "session_id": meta["session_id"],
        "title": meta["title"],
        "model": meta["model"],
        "cwd": meta["cwd"],
        "git_branch": meta["git_branch"],
        "cli_version": meta["version"],
        "transcript": os.path.abspath(path),
        "transcript_bytes": os.path.getsize(path),
        "rows_total": len(rows),
        "rows_by_type": buckets["by_type"],
        "transcript_entries_written": entries,
        "user_messages": user_total,
        "user_unique": user_unique,
        "user_duplicates_collapsed": user_total - user_unique,
        "user_mid_turn": buckets.get("mid_turn", 0),
        "user_noise_skipped": buckets.get("noise_skipped", 0),
        "assistant_with_text": len(buckets["assistant"]),
        "decisions_written": decisions,
        "tool_calls": len(buckets["tool_use"]),
        "tool_results": len(buckets["tool_result"]),
        "tool_census": dict(sorted(buckets["tool_census"].items(),
                                   key=lambda kv: -kv[1])),
        "compactions": compactions,
        "active_hours": round(active, 2),
        "span_hours": round(span, 2),
        "started": datetime.fromtimestamp(stamps[0]).strftime("%Y-%m-%d %H:%M")
                   if stamps else None,
        "last_activity": datetime.fromtimestamp(stamps[-1]).strftime("%Y-%m-%d %H:%M")
                         if stamps else None,
        "out_dir": os.path.abspath(out_dir),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    with open(p04, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)

    def size(p):
        try:
            return f"{os.path.getsize(p):,} B"
        except OSError:
            return "missing"

    print(f"parent session : {meta['session_id']}  «{meta['title']}»")
    print(f"transcript     : {path}  ({index['transcript_bytes']:,} B)")
    print(f"rows           : {len(rows):,}  {buckets['by_type']}")
    print(f"user messages  : {user_total} ({user_unique} unique, "
          f"{user_total - user_unique} duplicates collapsed, "
          f"{buckets.get('mid_turn', 0)} sent mid-turn)")
    print(f"tool calls     : {len(buckets['tool_use'])}  "
          f"results {len(buckets['tool_result'])}")
    print(f"active / span  : {active:.2f}h / {span:.2f}h")
    print(f"compactions    : {compactions}")
    print()
    print(f"  01-user-messages.md    {size(p01)}")
    print(f"  02-decisions.md        {size(p02)}")
    print(f"  03-full-transcript.txt {size(p03)}")
    print(f"  04-index.json          {size(p04)}")
    if compactions:
        print(f"  05-dropped-context.md  {size(p05)}")
    print()
    print(f"out_dir: {os.path.abspath(out_dir)}")
    print()
    print("READ 01-user-messages.md IN FULL before doing anything else.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
