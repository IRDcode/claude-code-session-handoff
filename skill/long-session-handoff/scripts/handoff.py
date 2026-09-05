#!/usr/bin/env python3
"""handoff.py -- migrate a heavy Claude Code session into a fresh one.

The mechanical half of long-session-handoff. The model writes the brief;
this script does every part that must not be done by hand:

  1. measure the parent, and refuse a migration that is still light unless
     --force -- a handoff fired early throws away usable context
  2. dump the parent transcript to disk as a SUBPROCESS, so the multi-MB
     transcript never enters anybody's context window
  3. mint the child session id and record the parent <-> child link, so the
     chain stays walkable months later
  4. WAKE the child with one headless turn whose only job is Rule Zero:
     read the dump in full and prove it with counts. That turn lands in the
     child's own transcript, so the context is already established before a
     human ever resumes it -- the expensive half of a handoff gets paid once,
     off the interactive path.
  5. print how to get there, because a CLI has no chat list

Usage:
  python handoff.py --parent <sid> --brief <brief.md> [--name "..."]
  python handoff.py --parent <sid> --prepare        # dump only, no child
  python handoff.py --list                          # every chain so far

Never pipe a transcript into a model. That is the cost this skill exists
to avoid.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_weight as sw  # noqa: E402

CLAUDE_DIR = (os.environ.get("CLAUDE_CONFIG_DIR")
              or os.path.join(os.path.expanduser("~"), ".claude"))
HOME = os.path.expanduser("~")
HANDOFF_ROOT = os.path.join(CLAUDE_DIR, "handoffs")
DUMPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "dump_parent_session.py")
WAKE_TIMEOUT = 900          # a Rule Zero read of 01+02+04 takes well under this
MIN_SCORE_TO_MIGRATE = 2    # kept for reference; the real guard is urgency


# ------------------------------------------------------------------ registry
def registry_path():
    return os.path.join(HANDOFF_ROOT, "chains.json")


def load_registry():
    try:
        with open(registry_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_registry(rows):
    """Atomic. The chain index is the only thing that makes a handoff
    reversible six weeks later; a half-written file loses the lineage."""
    import tempfile
    os.makedirs(HANDOFF_ROOT, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=HANDOFF_ROOT, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, registry_path())


def chain_of(session_id, rows=None):
    """Walk back to the root. Returns oldest-first list of session ids."""
    rows = rows if rows is not None else load_registry()
    parent_of = {r["child"]: r["parent"] for r in rows if r.get("child")}
    chain = [session_id]
    seen = {session_id}
    cur = session_id
    while cur in parent_of and parent_of[cur] not in seen:
        cur = parent_of[cur]
        seen.add(cur)
        chain.append(cur)
    return list(reversed(chain))


def generation_of(session_id, rows=None):
    return len(chain_of(session_id, rows))


# --------------------------------------------------------------------- steps
def find_claude():
    """The launcher, as something this Python can actually exec. (path)

    Order:
      1. CLAUDE_CODE_BIN, if it points at a real file. The escape hatch for any
         install layout not covered below.
      2. The newest ~/.local/share/claude/versions/*/claude{,.exe}.
      3. The install dir shim, ~/.local/bin/claude{,.exe}.
      4. Whatever `claude` resolves to on PATH.

    Why not just use PATH: on Windows `claude` is frequently a shell script with
    no extension, which CreateProcess cannot execute -- Python raises
    "not a valid Win32 application" and the wake turn never runs. On POSIX the
    PATH entry is usually fine, so step 4 is a real answer there and a last
    resort here.
    """
    override = os.environ.get("CLAUDE_CODE_BIN")
    if override and os.path.exists(override):
        return override

    exe_names = ("claude.exe", "claude.cmd", "claude") if os.name == "nt" \
        else ("claude",)

    vers = os.path.join(HOME, ".local", "share", "claude", "versions")
    best = None
    try:
        for name in os.listdir(vers):
            for exe_name in exe_names:
                exe = os.path.join(vers, name, exe_name)
                if os.path.exists(exe):
                    st = os.stat(exe)
                    if best is None or st.st_mtime > best[0]:
                        best = (st.st_mtime, exe)
    except OSError:
        pass
    if best:
        return best[1]

    for d in (os.path.join(HOME, ".local", "bin"),
              os.path.join(HOME, "bin")):
        for exe_name in exe_names:
            cand = os.path.join(d, exe_name)
            if os.path.exists(cand):
                return cand

    from shutil import which
    return which("claude") or "claude"


def dump_parent(transcript, out_dir):
    """Run the dumper as a subprocess and return its index. The transcript is
    megabytes; only the counts come back across this boundary."""
    os.makedirs(out_dir, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, DUMPER, transcript, out_dir],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    index_path = os.path.join(out_dir, "04-index.json")
    if proc.returncode != 0 or not os.path.exists(index_path):
        raise RuntimeError(
            f"dump failed (rc={proc.returncode})\n"
            f"stdout: {proc.stdout[-1500:]}\nstderr: {proc.stderr[-1500:]}")
    with open(index_path, encoding="utf-8") as fh:
        return json.load(fh), proc.stdout


WAKE_PROMPT = """\
You are the continuation of an earlier session that grew too heavy to keep
working in. Nothing of it is in your context. Everything of it is on disk.

RULE ZERO -- do this now, before anything else, and do not skip a file:

1. Read {dump}/01-user-messages.md IN FULL. Every requirement, correction and
   standing instruction the user ever gave is in there, in their own words.
   Messages marked [sent mid-turn] are corrections: they outrank anything
   earlier that contradicts them.
2. Read {dump}/02-decisions.md IN FULL -- what was decided and why.
3. Read {dump}/04-index.json and confirm your read against its counts.
4. Read {brief} IN FULL -- the outgoing session's own handoff brief.
{extra_step}
Read them with the Read tool, whole, no offset/limit, no skimming. 01 and 02
are small on purpose; 03-full-transcript.txt is large -- consult it only for a
specific question the others cannot answer, and grep it rather than reading it.

Then reply with exactly this and nothing else:

  HANDOFF ACCEPTED
  user messages read: <n>/<n_from_index>   unique: <n>
  mid-turn corrections: <n>
  decisions read: <n>
  parent: {parent}
  generation: {generation}
  open threads: <one line each, from the brief's Next Steps>
  standing constraints: <one line each -- the rules that outlive any task>

No other work. No tool calls beyond those reads. The human will drive from here.
"""


def wake_child(child_id, name, dump_dir, brief_path, parent_id, generation,
               cwd, timeout=WAKE_TIMEOUT, model=None):
    """Spend one headless turn establishing the child's context.

    This is the whole reason a CLI handoff can beat compaction: the read
    happens in a session nobody is waiting on, and its result is durable --
    the child's transcript now contains the full requirement set, so when the
    human resumes it the context is already there and already paid for.

    --permission-mode auto, never bypassPermissions: this turn only reads
    files, and a child that can act unsupervised is a different feature.
    """
    extra = ""
    dropped = os.path.join(dump_dir, "05-dropped-context.md")
    if os.path.exists(dropped):
        extra = (f"5. Read {dump_dir}/05-dropped-context.md IN FULL -- the parent"
                 f" was auto-compacted, and that file is what the compaction\n"
                 f"   threw away. Treat it as recovered context, not history.\n")

    prompt = WAKE_PROMPT.format(
        dump=dump_dir, brief=brief_path, parent=parent_id,
        generation=generation, extra_step=extra)

    cmd = [find_claude(), "-p", prompt,
           "--session-id", child_id,
           "--permission-mode", "auto"]
    if name:
        cmd += ["-n", name]
    if model:
        cmd += ["--model", model]

    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return {"ok": False, "seconds": round(time.time() - started, 1),
                "error": f"wake turn exceeded {timeout}s", "output": ""}
    except OSError as exc:
        # The launcher could not be executed at all -- wrong path, or a shell
        # shim CreateProcess will not run. The export already succeeded, so say
        # so plainly and tell the user the one thing that fixes it.
        return {"ok": False, "seconds": round(time.time() - started, 1),
                "error": f"could not run {cmd[0]!r}: {exc}. "
                         f"Set CLAUDE_CODE_BIN to the real executable, or open "
                         f"the continuation yourself and tell it to read the "
                         f"export.",
                "output": ""}
    out = (proc.stdout or "").strip()

    result = {
        "ok": proc.returncode == 0 and "HANDOFF ACCEPTED" in out,
        "returncode": proc.returncode,
        "seconds": round(time.time() - started, 1),
        "output": out[-4000:],
        "stderr": (proc.stderr or "")[-1500:],
    }
    if not result["ok"] and proc.returncode == 0 and out:
        # It ran and said something, but not the required acknowledgement. The
        # difference matters: the export is fine and the read may be partial,
        # which is a different problem from the launcher being broken.
        result["error"] = ("the continuation replied without 'HANDOFF ACCEPTED'."
                          " Treat the read as unverified and check it yourself.")
    elif not result["ok"] and not out:
        # An unsupported flag on an older or newer CLI lands here: exit code
        # non-zero, nothing on stdout, the reason on stderr.
        result["error"] = (f"the launcher exited {proc.returncode} with no output."
                           f" stderr: {(proc.stderr or '').strip()[-300:] or '(empty)'}")
    return result


# ------------------------------------------------------------------ commands
def cmd_migrate(args):
    parent = args["parent"]
    transcript = args.get("transcript") or sw.find_transcript(parent)
    if not transcript:
        return fail(f"no transcript found for parent session {parent}")

    sig = sw.measure(transcript)
    pts, reasons, urgency, extras = sw.score(sig)

    # The refusal is on URGENCY, not on score. Score counts proxies (turns,
    # hours, prior compactions); urgency has already been through the context
    # gate, which is the only signal that knows whether moving saves anything.
    # A session can score 5 and still be at 15% of its trigger.
    if urgency not in ("due", "critical") and not args["force"] \
            and not args["prepare"]:
        return fail(
            f"parent urgency is '{urgency}' (score {pts}) -- too light to "
            f"migrate.\n"
            f"  context {extras['ctx']:,}/{extras['threshold']:,} "
            f"({extras['pct']*100:.0f}%), {extras['headroom']:,} tokens of "
            f"headroom left unused.\n"
            + ("  The context gate is holding this back: other signals tripped, "
               "but\n  the window still has room and a handoff now would waste "
               "it.\n" if extras.get("gated") else "")
            + f"Finish using this session first, or pass --force if you have a "
              f"reason (topic change, cache already cold).")

    rows = load_registry()
    generation = generation_of(parent, rows) + 1
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dump_dir = os.path.join(HANDOFF_ROOT, f"{stamp}-{parent[:8]}")

    index, dump_log = dump_parent(transcript, dump_dir)

    brief_src = args.get("brief")
    brief_path = os.path.join(dump_dir, "00-handoff-brief.md")
    if brief_src and os.path.exists(brief_src):
        with open(brief_src, encoding="utf-8", errors="replace") as fh:
            brief_text = fh.read()
        with open(brief_path, "w", encoding="utf-8") as fh:
            fh.write(brief_text)
    elif args["prepare"]:
        brief_path = None
    else:
        return fail("--brief <path> is required to migrate (write the brief "
                    "first, then run this). Use --prepare to dump only.")

    result = {
        "parent": parent,
        "parent_transcript": transcript,
        "parent_title": sig["title"],
        "generation": generation,
        "dump_dir": dump_dir,
        "brief": brief_path,
        "index": index,
        "weight": {"score": pts, "urgency": urgency, "reasons": reasons,
                   **extras,
                   "assistant": sig["assistant"], "tool_calls": sig["tool_calls"],
                   "active_hours": sig["active_hours"],
                   "compactions": sig["compactions"],
                   "dropped_tokens": sig["dropped_tokens"]},
        "created": stamp,
    }

    if args["prepare"]:
        result["child"] = None
        result["note"] = "prepared only; no child session created"
        rows.append(result)
        save_registry(rows)
        return ok(result, args)

    child = args.get("child_id") or str(uuid.uuid4())
    name = args.get("name") or default_name(sig, generation)
    result["child"] = child
    result["child_name"] = name

    if args["no_wake"]:
        result["wake"] = {"ok": None, "note": "skipped (--no-wake)"}
    else:
        result["wake"] = wake_child(
            child, name, dump_dir, brief_path, parent, generation,
            cwd=args.get("cwd") or os.getcwd(),
            timeout=args["timeout"], model=args.get("model"))

    rows.append(result)
    save_registry(rows)
    return ok(result, args)


def default_name(sig, generation):
    """The name is the only handle `/resume` can search on, so keep the parent's
    title verbatim and stamp the generation onto it. A chain then sorts and reads
    correctly in the picker."""
    base = (sig.get("title") or "session").strip()
    for ch in '\r\n\t"':
        base = base.replace(ch, " ")
    base = " ".join(base.split())[:60]
    return f"{base} (cont. {generation})"
def cmd_list(args):
    rows = load_registry()
    if args["json"]:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("no handoffs recorded yet")
        return 0
    print(f"{len(rows)} handoff(s), newest last\n")
    for r in rows:
        w = r.get("weight") or {}
        idx = r.get("index") or {}
        child = r.get("child") or "(prepared only)"
        print(f"  {r.get('created')}  gen {r.get('generation')}")
        print(f"    parent {r.get('parent')}  {r.get('parent_title') or ''}")
        print(f"    child  {child}  {r.get('child_name') or ''}")
        print(f"    weight score {w.get('score')} / ctx {w.get('ctx', 0):,} "
              f"({(w.get('pct') or 0) * 100:.0f}% of {w.get('threshold', 0):,})")
        print(f"    dump   {r.get('dump_dir')}  "
              f"({idx.get('user_messages', '?')} user msgs, "
              f"{idx.get('decisions_written', '?')} decisions)")
        wake = r.get("wake") or {}
        if wake.get("ok") is not None:
            print(f"    wake   {'accepted' if wake['ok'] else 'FAILED'} "
                  f"in {wake.get('seconds')}s")
        print()
    return 0


def cmd_chain(args):
    rows = load_registry()
    ids = chain_of(args["parent"], rows)
    by_child = {r["child"]: r for r in rows if r.get("child")}
    print(f"chain of {len(ids)} session(s), oldest first:\n")
    for i, sid in enumerate(ids, 1):
        r = by_child.get(sid)
        title = (r or {}).get("child_name") or ""
        if i == 1:
            first = next((x for x in rows if x.get("parent") == sid), None)
            title = (first or {}).get("parent_title") or title
        print(f"  {i}. {sid}  {title}")
        if r:
            print(f"     dump {r.get('dump_dir')}")
    print("\nresume any of them with:  claude --resume <id>")
    return 0
def ok(result, args):
    if args["json"]:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    w = result["weight"]
    idx = result["index"]
    L = ["HANDOFF", ""]
    L.append(f"  parent      {result['parent']}  ({result.get('parent_title') or '?'})")
    L.append(f"  generation  {result['generation']}")
    L.append(f"  weight      score {w['score']} / urgency {w['urgency']}")
    L.append(f"  context     {w['ctx']:,} of {w['threshold']:,} "
             f"{'compact trigger' if w.get('limit_kind') == 'compact' else 'send-refusal wall'} "
             f"({w['pct']*100:.0f}%), {w['headroom']:,} unused")
    if w.get("compactions"):
        L.append(f"  compacted   {w['compactions']}x, {w['dropped_tokens']:,} "
                 f"tokens recovered into 05-dropped-context.md")
    L.append("")
    L.append(f"  dump        {result['dump_dir']}")
    L.append(f"              {idx.get('user_messages', '?')} user messages "
             f"({idx.get('user_unique', '?')} unique, "
             f"{idx.get('user_mid_turn', '?')} mid-turn), "
             f"{idx.get('decisions_written', '?')} decisions, "
             f"{idx.get('tool_calls', '?')} tool calls")
    L.append(f"  brief       {result.get('brief') or '(none)'}")
    if result.get("child"):
        L.append("")
        L.append(f"  CHILD       {result['child']}")
        L.append(f"  named       {result.get('child_name')}")
        wake = result.get("wake") or {}
        if wake.get("ok"):
            L.append(f"  woken       yes, in {wake['seconds']}s -- the child has "
                     f"already read the dump; its context is established")
        elif wake.get("ok") is None:
            L.append(f"  woken       {wake.get('note')}")
        else:
            L.append(f"  woken       NO -- "
                     f"{wake.get('error') or 'rc=' + str(wake.get('returncode'))}")
            L.append("              the child exists but has NOT read the dump.")
            L.append("              Resume it and paste Rule Zero yourself.")
        L.append("")
        L.append("  GO THERE:")
        L.append(f"    claude --resume {result['child']}")
        L.append(f"    or /resume  ->  search \"{result.get('child_name')}\"")
    else:
        L.append("")
        L.append(f"  {result.get('note')}")
    print("\n".join(L))
    return 0


def fail(msg):
    sys.stderr.write("handoff: " + msg + "\n")
    return 1
def main(argv):
    args = {"parent": None, "brief": None, "name": None, "child_id": None,
            "transcript": None, "cwd": None, "model": None,
            "json": False, "force": False, "prepare": False,
            "no_wake": False, "timeout": WAKE_TIMEOUT,
            "cmd": "migrate"}
    i = 0
    while i < len(argv):
        a = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if a in ("--parent", "-p") and nxt:
            args["parent"] = nxt; i += 1
        elif a == "--brief" and nxt:
            args["brief"] = nxt; i += 1
        elif a in ("--name", "-n") and nxt:
            args["name"] = nxt; i += 1
        elif a == "--child-id" and nxt:
            args["child_id"] = nxt; i += 1
        elif a == "--transcript" and nxt:
            args["transcript"] = nxt; i += 1
        elif a == "--cwd" and nxt:
            args["cwd"] = nxt; i += 1
        elif a == "--model" and nxt:
            args["model"] = nxt; i += 1
        elif a == "--timeout" and nxt:
            args["timeout"] = int(nxt); i += 1
        elif a == "--json":
            args["json"] = True
        elif a == "--force":
            args["force"] = True
        elif a == "--prepare":
            args["prepare"] = True
        elif a == "--no-wake":
            args["no_wake"] = True
        elif a == "--list":
            args["cmd"] = "list"
        elif a == "--chain":
            args["cmd"] = "chain"
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        i += 1

    if args["cmd"] == "list":
        return cmd_list(args)
    if args["cmd"] == "chain":
        if not args["parent"]:
            return fail("--chain needs --parent <session-id>")
        return cmd_chain(args)
    if not args["parent"]:
        sys.stderr.write(__doc__)
        return 2
    return cmd_migrate(args)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.exit(fail(str(exc)))



