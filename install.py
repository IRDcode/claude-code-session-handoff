#!/usr/bin/env python3
"""install.py -- install the long-session handoff guard into ~/.claude.

    python install.py                    install (safe default: no compaction changes)
    python install.py --dry-run          print every change, write nothing
    python install.py --uninstall        remove exactly what this added
    python install.py --disable-compact  also turn auto-compaction off (see below)
    python install.py --window 1000000   the real window of your model, with the above

The default install adds a detector, four hooks and a status line. It does not
change how Claude Code manages context: auto-compaction stays exactly as it is.
The guard simply hands the session off BEFORE compaction can fire, which is
enough to keep your history intact.

--disable-compact goes further: it stops compaction from firing at all, so the
session ends in a refusal to send rather than a silent summary. Read the warning
this prints before you accept it -- it also disables manual /compact.

Every change is reversible. settings.json is backed up before it is touched and
--uninstall puts back what was there.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")

SKILL_NAME = "long-session-handoff"
HOOK_SCRIPT = "session-weight-watch.py"
STATUS_SCRIPT = "statusline-weight.py"
HOOK_EVENTS = (("UserPromptSubmit", 10), ("SessionStart", 10),
               ("PreCompact", 15), ("PostCompact", 15))

GREEN, YELLOW, RED, DIM, RESET = (
    "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[2m", "\x1b[0m")

MIN_PY = (3, 8)


def preflight():
    """Refuse to install into an environment where it cannot work.

    Checked here rather than discovered later, because a half-installed guard is
    worse than none: the hooks would fire and fail silently on every turn.
    """
    problems = []
    if sys.version_info < MIN_PY:
        problems.append(
            f"this Python is {sys.version_info.major}.{sys.version_info.minor}; "
            f"{MIN_PY[0]}.{MIN_PY[1]}+ is required")

    py = python_cmd()
    try:
        r = subprocess.run(f'{py} -c "print(1)"', shell=True,
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or r.stdout.strip() != "1":
            problems.append(
                f"hooks run through a shell, and {py!r} does not work there. "
                f"Install Python so it is on PATH, or edit the commands in "
                f"settings.json afterwards.")
    except Exception as exc:
        problems.append(f"could not test {py!r} through a shell: {exc}")

    parent = os.path.dirname(CLAUDE_DIR) or "."
    if not os.path.isdir(CLAUDE_DIR) and not os.access(parent, os.W_OK):
        problems.append(f"cannot create {CLAUDE_DIR} (no write access to {parent})")
    elif os.path.isdir(CLAUDE_DIR) and not os.access(CLAUDE_DIR, os.W_OK):
        problems.append(f"no write access to {CLAUDE_DIR}")

    if problems:
        say("Cannot install:", RED)
        for p in problems:
            say(f"  - {p}")
        raise SystemExit(2)

    if not os.path.isdir(os.path.join(CLAUDE_DIR, "projects")):
        # Not fatal: a fresh install has no transcripts yet. But it usually means
        # Claude Code has never run here, or CLAUDE_CONFIG_DIR points elsewhere.
        say(f"note: no {os.path.join(CLAUDE_DIR, 'projects')} yet -- if Claude "
            f"Code stores its config elsewhere, set CLAUDE_CONFIG_DIR and "
            f"re-run.", YELLOW)


def say(msg, colour=""):
    sys.stdout.write(f"{colour}{msg}{RESET}\n" if colour else msg + "\n")


def python_cmd():
    """How the hooks should invoke Python.

    Hook commands run through a shell, so the interpreter has to be findable
    there rather than here. `py -3` is the launcher Windows installs; on other
    platforms python3 is the safe name. Quoted absolute paths break when the
    user later moves their Python install, so prefer a name over sys.executable.
    """
    if os.name == "nt":
        return "py -3" if shutil.which("py") else "python"
    return "python3" if shutil.which("python3") else "python"


def load_settings():
    if not os.path.exists(SETTINGS):
        return {}, False
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
        return (data if isinstance(data, dict) else {}), True
    except ValueError as exc:
        say(f"settings.json is not valid JSON ({exc}).", RED)
        say("Fix or move it first -- refusing to overwrite a file I cannot parse.")
        raise SystemExit(2)


def backup_settings():
    if not os.path.exists(SETTINGS):
        return None
    dest = f"{SETTINGS}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(SETTINGS, dest)
    return dest


def write_settings(data):
    tmp = SETTINGS + ".tmp"
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, SETTINGS)


def hook_command(py):
    # $HOME is expanded by the shell the hook runs in; CLAUDE_CONFIG_DIR users
    # get the literal path instead, since $HOME would be wrong for them.
    if CLAUDE_DIR == os.path.join(HOME, ".claude"):
        return f'{py} "$HOME/.claude/hooks/{HOOK_SCRIPT}"'
    return f'{py} "{os.path.join(CLAUDE_DIR, "hooks", HOOK_SCRIPT)}"'


def status_command(py):
    if CLAUDE_DIR == os.path.join(HOME, ".claude"):
        return f'{py} "$HOME/.claude/hooks/{STATUS_SCRIPT}"'
    return f'{py} "{os.path.join(CLAUDE_DIR, "hooks", STATUS_SCRIPT)}"'


def is_ours(command):
    return HOOK_SCRIPT in (command or "") or STATUS_SCRIPT in (command or "")

def copy_payload(dry):
    """Copy the skill and both hooks into CLAUDE_DIR. Returns list of paths."""
    written = []
    pairs = [
        (os.path.join(HERE, "skill", SKILL_NAME),
         os.path.join(CLAUDE_DIR, "skills", SKILL_NAME)),
    ]
    for name in (HOOK_SCRIPT, STATUS_SCRIPT):
        pairs.append((os.path.join(HERE, "hooks", name),
                      os.path.join(CLAUDE_DIR, "hooks", name)))

    for src, dst in pairs:
        if not os.path.exists(src):
            say(f"missing from this checkout: {src}", RED)
            raise SystemExit(2)
        written.append(dst)
        if dry:
            verb = "overwrite" if os.path.exists(dst) else "create"
            say(f"  would {verb}  {dst}")
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return written


def add_hooks(data, py, dry):
    """Register the four events without disturbing anyone else's hooks."""
    hooks = data.setdefault("hooks", {})
    cmd = hook_command(py)
    added, kept = [], 0
    for event, timeout in HOOK_EVENTS:
        blocks = hooks.setdefault(event, [])
        already = any(is_ours(h.get("command"))
                      for b in blocks if isinstance(b, dict)
                      for h in (b.get("hooks") or []))
        if already:
            kept += 1
            continue
        blocks.append({"hooks": [{"type": "command", "command": cmd,
                                 "timeout": timeout}]})
        added.append(event)
    if added:
        say(f"  hooks registered: {', '.join(added)}"
            + (f"  ({kept} already present)" if kept else ""))
    else:
        say(f"  hooks already registered on all {len(HOOK_EVENTS)} events")
    return data


def add_statusline(data, py, dry):
    existing = data.get("statusLine")
    cmd = status_command(py)
    if isinstance(existing, dict) and is_ours(existing.get("command")):
        say("  status line already ours")
        return data
    if isinstance(existing, dict) and existing.get("command"):
        say("  status line already set to something else -- LEAVING IT ALONE", YELLOW)
        # json.dumps so the suggestion is copy-pasteable: the command itself
        # contains double quotes around the script path.
        snippet = json.dumps({"statusLine": {"type": "command", "command": cmd}})
        say(f"    to use ours instead, put this in settings.json: {snippet}", DIM)
        return data
    data["statusLine"] = {"type": "command", "command": cmd, "padding": 0}
    say("  status line installed")
    return data

def current_wall():
    """Where this machine's session actually ends TODAY, before any change.

    Imported from the payload rather than recomputed, so the installer and the
    scorer can never disagree. Returns (tokens, kind) or (None, None) -- the
    installer must work even if the import fails.
    """
    try:
        sys.path.insert(0, os.path.join(HERE, "skill", SKILL_NAME, "scripts"))
        import session_weight as sw
        return sw.compact_threshold(), sw.limit_kind()[0]
    except Exception:
        return None, None
    finally:
        if sys.path and sys.path[0].endswith("scripts"):
            sys.path.pop(0)


COMPACT_WARNING = """
--disable-compact changes how Claude Code ends a full session.

  BEFORE  {before}

  AFTER   compaction never fires. At ~{wall} tokens the client refuses to
          send instead, and the guard hands the session off long before that.

What you give up:
  * manual /compact stops working too. There is no summarise-in-place escape.
  * if you ignore the handoff prompts all the way to the wall, the session
    stops accepting turns. The work is still on disk, and you can hand off
    from it, but you cannot keep typing in that session.

Why it can be worth it: on some models the larger context window is only
reachable with compaction disabled -- the window setting alone is silently
floored. Verify which case you are in AFTER installing:

    {py} {weight} --explain
"""


def set_compaction(data, window, dry):
    wall, kind = current_wall()
    if wall and kind == "compact":
        before = (f"at ~{wall:,} tokens auto-compaction fires: your\n"
                  f"          conversation is replaced by a summary and work "
                  f"continues in a\n          diminished session.")
        if window and wall < window * 0.5:
            # The clamp, stated plainly. This is the number people do not
            # expect, and seeing it next to what they asked for is the point.
            before += (f"\n          NOTE: you asked for a {window:,} window, "
                       f"but compaction fires at\n          {wall:,} -- the "
                       f"setting is being floored. That is the bug.")
    elif wall:
        before = (f"compaction is already disabled; the session ends at "
                  f"~{wall:,} tokens.")
    else:
        before = "auto-compaction fires well before the window is full."

    wall_after = max(window - 20000 - 3000, 1) if window else 977000
    say(COMPACT_WARNING.format(
        before=before, wall=f"{wall_after:,}", py=python_cmd(),
        weight=os.path.join(CLAUDE_DIR, "skills", SKILL_NAME, "scripts",
                            "session_weight.py")), YELLOW)
    if not dry:
        reply = input("Type 'yes' to disable compaction: ").strip().lower()
        if reply != "yes":
            say("  left compaction alone.", GREEN)
            return data, False
    data["autoCompactEnabled"] = False
    env = data.setdefault("env", {})
    env["DISABLE_COMPACT"] = "1"
    if window:
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(window)
    say(f"  compaction disabled; window set to {window:,}" if window
        else "  compaction disabled", GREEN)
    return data, True


def install(args):
    dry = args["dry_run"]
    say(f"{'DRY RUN -- nothing will be written' if dry else 'Installing'} "
        f"into {CLAUDE_DIR}", DIM)
    preflight()
    py = python_cmd()

    copy_payload(dry)
    data, existed = load_settings()
    backup = None if dry else backup_settings()
    if backup:
        say(f"  settings backed up to {os.path.basename(backup)}", DIM)

    data = add_hooks(data, py, dry)
    data = add_statusline(data, py, dry)

    changed_compaction = False
    if args["disable_compact"]:
        data, changed_compaction = set_compaction(data, args["window"], dry)

    if dry:
        say("\nresulting settings.json (relevant keys only):", DIM)
        view = {k: data.get(k) for k in
                ("autoCompactEnabled", "statusLine") if k in data}
        view["hooks"] = {e: f"<{len(data.get('hooks', {}).get(e, []))} block(s)>"
                         for e, _ in HOOK_EVENTS}
        if "env" in data:
            view["env"] = {k: v for k, v in data["env"].items()
                           if k in ("DISABLE_COMPACT",
                                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS")}
        say(json.dumps(view, indent=2))
        return 0

    write_settings(data)
    say("\ndone.", GREEN)
    say("Next:")
    say("  1. restart Claude Code, or start a new session")
    say(f"  2. confirm the wall it will use:")
    say(f"       {py} {os.path.join(CLAUDE_DIR, 'skills', SKILL_NAME, 'scripts', 'session_weight.py')} --explain")
    if not changed_compaction:
        say(DIM + "  (compaction left as-is. The guard hands off before it can "
            "fire; pass --disable-compact if you want it off entirely.)" + RESET)
    return 0

def uninstall(args):
    """Remove exactly what install added, and nothing else."""
    dry = args["dry_run"]
    say(f"{'DRY RUN -- ' if dry else ''}Removing from {CLAUDE_DIR}", DIM)

    targets = [os.path.join(CLAUDE_DIR, "skills", SKILL_NAME),
               os.path.join(CLAUDE_DIR, "hooks", HOOK_SCRIPT),
               os.path.join(CLAUDE_DIR, "hooks", STATUS_SCRIPT)]
    for t in targets:
        if not os.path.exists(t):
            continue
        if dry:
            say(f"  would delete  {t}")
            continue
        if os.path.isdir(t):
            shutil.rmtree(t)
        else:
            os.remove(t)
        say(f"  deleted  {os.path.basename(t)}")

    for name in ("session-weight-watch.json", "session-weight-watch.log",
                 "session-weight-cache.json", "observed-window.json"):
        p = os.path.join(CLAUDE_DIR, "runtime", name)
        if os.path.exists(p):
            if dry:
                say(f"  would delete  {p}")
            else:
                os.remove(p)

    data, existed = load_settings()
    if not existed:
        say("no settings.json to clean.", GREEN)
        return 0
    backup = None if dry else backup_settings()
    if backup:
        say(f"  settings backed up to {os.path.basename(backup)}", DIM)

    hooks = data.get("hooks") or {}
    for event, _ in HOOK_EVENTS:
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            continue
        kept = []
        for b in blocks:
            inner = [h for h in ((b or {}).get("hooks") or [])
                     if not is_ours(h.get("command"))]
            if inner:
                kept.append({**b, "hooks": inner})
            elif not (b or {}).get("hooks"):
                kept.append(b)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)

    sl = data.get("statusLine")
    if isinstance(sl, dict) and is_ours(sl.get("command")):
        data.pop("statusLine", None)
        say("  status line removed")

    # Compaction settings are deliberately NOT reverted: by the time someone
    # uninstalls they may be relying on the larger window for other reasons, and
    # silently re-enabling compaction is exactly the kind of surprise this whole
    # project exists to prevent. Tell them instead.
    touched = []
    if data.get("autoCompactEnabled") is False:
        touched.append("autoCompactEnabled: false")
    if (data.get("env") or {}).get("DISABLE_COMPACT"):
        touched.append("env.DISABLE_COMPACT")
    if touched:
        say("\n  LEFT IN PLACE (yours to decide): " + ", ".join(touched), YELLOW)
        say("  remove those by hand if you want auto-compaction back.", DIM)

    if dry:
        say("\n(dry run -- settings.json untouched)")
        return 0
    write_settings(data)
    say("\ndone. Restart Claude Code.", GREEN)
    return 0


def parse(argv):
    args = {"dry_run": False, "uninstall": False, "disable_compact": False,
            "window": None}
    for i, a in enumerate(argv):
        if a in ("--dry-run", "-n"):
            args["dry_run"] = True
        elif a == "--uninstall":
            args["uninstall"] = True
        elif a == "--disable-compact":
            args["disable_compact"] = True
        elif a == "--window" and i + 1 < len(argv):
            try:
                args["window"] = int(argv[i + 1].replace(",", "").replace("_", ""))
            except ValueError:
                say(f"--window needs a token count, got {argv[i + 1]!r}", RED)
                raise SystemExit(2)
        elif a in ("-h", "--help"):
            say(__doc__.strip())
            raise SystemExit(0)
    if args["disable_compact"] and not args["window"]:
        # Without a window there is nothing to raise the ceiling to, so the
        # only effect would be losing /compact. Refuse rather than half-do it.
        say("--disable-compact needs --window <tokens> (e.g. --window 1000000).", RED)
        say("Run with --help for what that means.", DIM)
        raise SystemExit(2)
    return args


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse(argv)
    return uninstall(args) if args["uninstall"] else install(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
