# Troubleshooting

## The status line shows nothing

The command has to be runnable by your shell, not just by you. Check what the
installer wrote:

```bash
python -c "import json,os;print(json.load(open(os.path.expanduser('~/.claude/settings.json')))['statusLine'])"
```

Then run that exact command by hand with an empty payload:

```bash
echo '{}' | python ~/.claude/hooks/statusline-weight.py
```

You should get a single line back. If you get nothing, the interpreter name in
`settings.json` is wrong for your shell — `python3` where only `python` exists,
or the reverse. Edit the `statusLine.command` string to match what
`which python3` / `where python` tells you.

## The handoff prompt never appears

Three things have to be true. Check them in order.

**1. The hooks are registered.**

```bash
python -c "import json,os;d=json.load(open(os.path.expanduser('~/.claude/settings.json')));print(sorted(d.get('hooks',{})))"
```

You want `UserPromptSubmit`, `SessionStart`, `PreCompact`, `PostCompact` present.

**2. The detector runs.**

```bash
echo '{"session_id":"x","hook_event_name":"UserPromptSubmit","source":"user","context_tokens":900000}' \
  | python ~/.claude/hooks/session-weight-watch.py
```

At 900,000 tokens you should get back JSON containing `additionalContext`. If you
get only `{"suppressOutput": true}`, the score did not fire — which is the correct
behaviour if your wall is small. Check the log:

```bash
tail ~/.claude/runtime/session-weight-watch.log
```

Every decision is recorded there, including the ones that decided to stay quiet.

**3. The gate is open.** Below 62% of the wall nothing is ever offered, no matter
how long the session has run. This is deliberate — see
[HOW-IT-WORKS](HOW-IT-WORKS.md#the-gate). Run `--explain` to see where 62% falls
on your machine.

## It offered a handoff far too early

Run the scorer and read the reasons:

```bash
python ~/.claude/skills/long-session-handoff/scripts/session_weight.py \
    --session-id <uuid>
```

If `used` is a small percentage but urgency is `due`, the wall is being computed
too low. That usually means the client's window has not been observed yet and the
conservative 200,000 default is in use. Render the status line once (any keypress
in an interactive session) and re-run — `--explain` will then say
`client reported`.

## It never offers, even in a genuinely huge session

Check that context is being read at all:

```bash
python ~/.claude/skills/long-session-handoff/scripts/session_weight.py \
    --session-id <uuid> --json | python -c "import json,sys;d=json.load(sys.stdin);print(d['context'])"
```

`ctx` of 0 means the transcript had no `usage` rows the parser recognised. That is
worth reporting as a bug — include your Claude Code version.

## I disabled compaction and now a session refuses to send

Expected, and recoverable. The work is on disk. Hand off from it:

```bash
python ~/.claude/skills/long-session-handoff/scripts/handoff.py \
    --parent <session-id> --force
```

`--force` skips the weight check, which you do not need — you already know the
session is full.

To avoid it next time: do not dismiss the `HANDOFF DUE` prompts. They start at
85% of the wall, leaving roughly 147,000 tokens of room on a 1M window.

## I want compaction back

```bash
python install.py --uninstall
```

then remove these by hand from `~/.claude/settings.json`, because the uninstaller
deliberately does not touch them:

```json
"autoCompactEnabled": false,
"env": { "DISABLE_COMPACT": "1" }
```

The uninstaller leaves them because silently re-enabling compaction is exactly
the class of surprise this project exists to prevent. Restart Claude Code after.

## `--explain` says `settings CLAMPED to …`

Working as intended, and it just told you something useful: your
`autoCompactWindow` is larger than the model ceiling and is being floored. The
setting is not doing what it looks like it is doing. See
[the clamp](../README.md#the-clamp).

## The wake turn times out or fails

`handoff.py` runs the continuation with `claude -p`. If that binary cannot be
found or cannot start, the export still succeeded — only the automatic read did
not happen.

Point the script at the right binary:

```bash
export CLAUDE_CODE_BIN=/full/path/to/claude
```

On Windows, `claude` on `PATH` is often a shell shim that Python's
`CreateProcess` cannot execute; the script prefers
`~/.local/bin/claude.exe` or the newest
`~/.local/share/claude/versions/*/claude.exe`.

You can always finish by hand: open the continuation and tell it to read the
export directory, which `handoff.py` printed.

## Numbers look wrong after a Claude Code update

Most likely the reserves moved. Run `--explain` and compare its `WALL` section
against where compaction actually fires in a real session (`--json` reports
`compactions` and `dropped_tokens` from the typed transcript rows).

If they disagree, that is the bug report that helps most — include the `--explain`
output and your version.

## Everything is broken and I want it gone

```bash
python install.py --uninstall
```

Removes the skill, both hooks, the runtime state, and the settings entries it
added. `settings.json` is backed up first. Exports under `~/.claude/handoffs/` are
left alone — they are your data, not the tool's.
