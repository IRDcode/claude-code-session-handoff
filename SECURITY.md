# Security

## Reporting an issue

Open a [private security advisory](https://github.com/IRDcode/claude-code-session-handoff/security/advisories/new),
or a regular issue if the problem is not sensitive. There is no bounty; this is a
free tool.

## What this software does on your machine

Stated plainly so you can decide whether that is acceptable before installing.

**Reads:**
- `~/.claude/settings.json` and `settings.local.json` — to determine the window
  and whether compaction is enabled. Only specific keys are read
  (`autoCompactEnabled`, `autoCompactWindow`, and four `env` entries). Nothing
  else is inspected, logged, or transmitted.
- `~/.claude/projects/*/*.jsonl` — your session transcripts, to count turns, tool
  calls, working time and compactions.

**Writes:**
- `~/.claude/skills/long-session-handoff/`, `~/.claude/hooks/` — the tool itself.
- `~/.claude/runtime/` — anti-nag state, a measurement cache, and a log of what
  the detector decided.
- `~/.claude/handoffs/` — session exports, when you accept a handoff.
- `~/.claude/settings.json` — hook registration and the status line, backed up
  first. Compaction settings are only touched with `--disable-compact`, which
  asks for typed confirmation.

**Sends over the network:** nothing. There is no telemetry, no update check, and
no outbound request of any kind. The only process it launches is your own
`claude` binary, locally, to have the continuation read the export.

## Handling of sensitive data

Your transcripts contain whatever you have discussed with the model, which may
include credentials pasted into a session. Two consequences:

- **Exports under `~/.claude/handoffs/` are as sensitive as the transcript they
  came from.** They are plain files with your umask's default permissions. Do not
  commit them, and delete directories you no longer need — nothing here prunes
  them for you.
- **Values from `settings.json` are never printed.** The installer's `--dry-run`
  shows only `DISABLE_COMPACT` and `CLAUDE_CODE_MAX_CONTEXT_TOKENS` from the
  `env` block, by explicit allowlist, so an API key in that block cannot appear
  in output you might paste into an issue. `session_weight.py --explain` prints
  token counts and setting *names*, never values.

If you attach output to a bug report, `--explain` and `--json` are safe to share.
Files under `~/.claude/handoffs/` are not.

## Trust boundaries

- **Transcripts are treated as data, never as instructions.** The scorer parses
  JSON rows and counts typed fields. Compactions are counted from typed
  `compact_boundary` rows rather than by searching for a marker string —
  partly for accuracy, and partly because a string search over content you did
  not write is a weaker foundation than a structural check.
- **Hook input is untrusted.** Malformed, empty and non-JSON stdin are all
  handled; every failure path returns `{"suppressOutput": true}` and exit 0, so a
  bad payload cannot block a turn. This is covered by `tests/test_compat.py`.
- **No shell interpolation of session data.** Subprocesses are invoked with
  argument lists, never a constructed command string, so a session title or path
  containing shell metacharacters cannot become a command.
- **The continuation runs with `--permission-mode auto`, never
  `bypassPermissions`.** Its wake turn only reads files from the export
  directory. A continuation that can act unsupervised would be a different
  feature with a different risk profile, and this is deliberately not that.

## Reverting

`python install.py --uninstall` removes the skill, both hooks, the runtime state
and the settings entries that were added, after backing up `settings.json`.

It deliberately does **not** revert `autoCompactEnabled` or `DISABLE_COMPACT`.
Silently re-enabling compaction would destroy context in an existing session
without warning — the exact failure this project exists to prevent. Those two
lines are yours to remove.

Exports under `~/.claude/handoffs/` are left alone: they are your data, not the
tool's.

## Supported versions

The `main` branch. This is a single-maintainer project with no backport policy;
fixes land on `main` and are tagged.
