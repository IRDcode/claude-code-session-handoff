#!/usr/bin/env python3
"""Compatibility checks: syntax floor, and the commands the installer writes.

    python tests/test_compat.py

Two things are verified here, both of which are claims the README makes and so
must not be guesses:

  1. Every shipped file parses under the oldest Python we claim to support.
     ast.parse(feature_version=...) rejects syntax newer than that version, so
     this is a real check rather than "it worked on my machine".

  2. The interpreter command the installer would write is actually runnable
     through a shell -- which is where hooks run, not where this script runs.
"""
from __future__ import annotations

import ast
import io
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# The floor the README claims. Raise it here and the README must change too.
MIN_PY = (3, 8)

FILES = [
    "install.py",
    "hooks/session-weight-watch.py",
    "hooks/statusline-weight.py",
    "skill/long-session-handoff/scripts/session_weight.py",
    "skill/long-session-handoff/scripts/handoff.py",
    "skill/long-session-handoff/scripts/dump_parent_session.py",
    "tests/test_session_weight.py",
    "tests/test_compat.py",
]

FAILURES = []


def ok(label):
    print(f"  ok    {label}")


def fail(label, detail=""):
    print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))
    FAILURES.append(label)


def test_syntax_floor():
    """Parse every file with the grammar of the oldest supported version.

    LIMITATION, learned by shipping a bug past this exact check:
    feature_version gates the GRAMMAR, not the tokenizer. PEP 701 f-strings
    (nested same-quote expressions, multi-line replacement fields) tokenize fine
    under any feature_version and only fail on a real pre-3.12 interpreter.

    So this test is necessary and not sufficient. test_real_interpreters() below
    is the one that actually caught it.
    """
    print(f"\nsyntax parses under Python {MIN_PY[0]}.{MIN_PY[1]} (grammar only)")
    for rel in FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            fail(rel, "missing")
            continue
        src = io.open(path, encoding="utf-8").read()
        try:
            ast.parse(src, filename=rel, feature_version=MIN_PY)
        except SyntaxError as exc:
            fail(rel, f"line {exc.lineno}: {exc.msg}")
            continue
        ok(rel)


def find_interpreters():
    """Every Python on this machine that is not the one running us.

    Deliberately opportunistic: whatever is here gets tested, and the report says
    what was found. A contributor with only one Python still gets a useful run;
    one with several gets real coverage.
    """
    import glob
    found = {}
    names = ["python3.8", "python3.9", "python3.10", "python3.11", "python3.12",
             "python3.13", "python3.14", "python3", "python"]
    globs = []
    if os.name == "nt":
        globs = [
            r"C:\Python3*\python.exe",
            r"C:\Program Files\Python3*\python.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe"),
            os.path.expandvars(r"%APPDATA%\uv\python\cpython-3.*\python.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\uv\python\cpython-3.*\python.exe"),
        ]
    else:
        names += ["/usr/bin/python3.%d" % m for m in range(8, 15)]
        globs = ["/usr/bin/python3.*", "/usr/local/bin/python3.*",
                 os.path.expanduser("~/.local/share/uv/python/cpython-3.*/bin/python3")]

    cands = []
    for n in names:
        w = shutil.which(n)
        if w:
            cands.append(w)
    for g in globs:
        cands.extend(glob.glob(g))

    for c in cands:
        try:
            r = subprocess.run([c, "-c",
                                "import sys;print('%d.%d' % sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            v = r.stdout.strip()
            # First interpreter wins per version; they are interchangeable for
            # a syntax check and running six copies of 3.12 proves nothing.
            found.setdefault(v, c)
    found.pop("%d.%d" % sys.version_info[:2], None)
    return found


def test_real_interpreters():
    """Run the real test suite under every other Python on this machine.

    This is the check with teeth. A compile-only check misses tokenizer-level
    differences; actually executing under 3.11 is what surfaced a PEP 701
    f-string that ast.parse(feature_version=(3,8)) had happily accepted.
    """
    print("\nreal interpreters found on this machine")
    interps = find_interpreters()
    if not interps:
        ok("none besides this one -- nothing further to check here")
        return
    suite = os.path.join(HERE, "test_session_weight.py")
    for ver in sorted(interps, key=lambda s: [int(x) for x in s.split(".")]):
        exe = interps[ver]
        # Compile everything first: a syntax error names the file and line,
        # where a failing suite only says something went wrong.
        bad = []
        for rel in FILES:
            r = subprocess.run(
                [exe, "-c",
                 "import py_compile,sys;py_compile.compile(sys.argv[1],"
                 "doraise=True)", os.path.join(ROOT, rel)],
                capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                last = [ln for ln in (r.stderr or "").strip().splitlines() if ln]
                bad.append(f"{rel}: {last[-1] if last else 'compile failed'}")
        if bad:
            fail(f"Python {ver} compile", "; ".join(bad)[:300])
            continue
        r = subprocess.run([exe, suite], capture_output=True, text=True,
                           timeout=600, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            ok(f"Python {ver}: compiles, suite passes")
        else:
            tail = (r.stdout or "").strip().splitlines()[-4:]
            fail(f"Python {ver} suite", " | ".join(tail)[:300])


def test_no_stdlib_newer_than_floor():
    """Imports that do not exist on the floor version.

    Not exhaustive -- it cannot be, without running every version. It catches
    the ones that are easy to reach for and silently raise ImportError on an
    older interpreter, which in a hook means a turn with no weight check.
    """
    print("\nno imports newer than the floor")
    banned = {
        "graphlib": (3, 9),          # 3.9
        "zoneinfo": (3, 9),          # 3.9
        "tomllib": (3, 11),          # 3.11
        "importlib.resources.files": (3, 9),
    }
    hits = []
    for rel in FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        src = io.open(path, encoding="utf-8").read()
        for name, since in banned.items():
            if since > MIN_PY and f"import {name}" in src:
                hits.append(f"{rel}: {name} needs {since[0]}.{since[1]}")
    if hits:
        for h in hits:
            fail(h)
    else:
        ok(f"none of {', '.join(sorted(banned))}")


def test_installer_command_runs_in_a_shell():
    """The hook command must work where hooks run: through a shell.

    A command that works when typed by hand can still fail from a hook if the
    interpreter name differs -- `python3` on a machine that only has `python`,
    or the reverse. The installer picks a name; this confirms the pick.
    """
    print("\nthe interpreter name the installer would write")
    sys.path.insert(0, ROOT)
    try:
        import install
    except Exception as exc:
        fail("import install.py", str(exc))
        return
    py = install.python_cmd()
    try:
        r = subprocess.run(f'{py} -c "print(1+1)"', shell=True,
                           capture_output=True, text=True, timeout=60)
    except Exception as exc:
        fail(f"{py!r} through a shell", str(exc))
        return
    if r.returncode == 0 and r.stdout.strip() == "2":
        ok(f"{py!r} runs through a shell")
    else:
        fail(f"{py!r} through a shell",
             f"rc={r.returncode} out={r.stdout.strip()!r} "
             f"err={r.stderr.strip()[:120]!r}")


def test_scripts_run_standalone():
    """Each entry point must survive being executed with no arguments.

    The hooks are the important case: Claude Code runs them with whatever it
    has, and a traceback there costs the user a turn.
    """
    print("\nentry points survive a bare invocation")
    cases = [
        ("hooks/statusline-weight.py", "{}"),
        ("hooks/session-weight-watch.py", "{}"),
        ("hooks/statusline-weight.py", "not json at all"),
        ("hooks/session-weight-watch.py", "not json at all"),
        ("hooks/session-weight-watch.py", '{"session_id":"x"}'),
    ]
    for rel, payload in cases:
        path = os.path.join(ROOT, rel)
        try:
            r = subprocess.run([sys.executable, path], input=payload,
                               capture_output=True, text=True, timeout=120,
                               encoding="utf-8")
        except Exception as exc:
            fail(f"{rel} <- {payload[:18]!r}", str(exc))
            continue
        if r.returncode != 0:
            fail(f"{rel} <- {payload[:18]!r}",
                 f"exit {r.returncode}: {r.stderr.strip()[:160]}")
        elif "Traceback" in (r.stderr or ""):
            fail(f"{rel} <- {payload[:18]!r}", "traceback on stderr")
        else:
            ok(f"{rel} <- {payload[:18]!r}  exit 0")


def test_hook_output_is_valid_json():
    """Whatever the hook decides, stdout must be one parseable object.

    On Windows stdout inherits the console codepage, which mangles non-ASCII
    and can truncate the JSON mid-write. That failure is invisible until a hook
    silently stops working, so it is asserted here.
    """
    print("\nhook stdout is valid JSON, always")
    import json
    path = os.path.join(ROOT, "hooks/session-weight-watch.py")
    payloads = [
        "{}",
        '{"session_id":"x","hook_event_name":"UserPromptSubmit","source":"user"}',
        '{"session_id":"x","hook_event_name":"PreCompact","trigger":"auto"}',
        '{"session_id":"x","hook_event_name":"PostCompact"}',
        '{"agent_id":"sub","session_id":"x","context_tokens":999999}',
    ]
    for p in payloads:
        r = subprocess.run([sys.executable, path], input=p, capture_output=True,
                           text=True, timeout=120, encoding="utf-8")
        try:
            obj = json.loads(r.stdout or "{}")
        except ValueError as exc:
            fail(f"parse stdout for {p[:30]!r}", str(exc))
            continue
        if not isinstance(obj, dict):
            fail(f"stdout for {p[:30]!r}", "not an object")
        elif "�" in r.stdout:
            fail(f"stdout for {p[:30]!r}", "contains U+FFFD (encoding mangled)")
        else:
            ok(f"{p[:34]!r} -> valid JSON, no mojibake")


def main():
    print(f"compatibility tests (this interpreter: "
          f"{sys.version_info.major}.{sys.version_info.minor})")
    for fn in (test_syntax_floor, test_real_interpreters,
               test_no_stdlib_newer_than_floor,
               test_installer_command_runs_in_a_shell,
               test_scripts_run_standalone, test_hook_output_is_valid_json):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
