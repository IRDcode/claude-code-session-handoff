# claude-code-session-handoff

[![tests](https://github.com/IRDcode/claude-code-session-handoff/actions/workflows/tests.yml/badge.svg)](https://github.com/IRDcode/claude-code-session-handoff/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python: 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

<p align="right">
  <a href="docs/README.zh-CN.md">简体中文</a> ·
  <a href="docs/README.ja.md">日本語</a> ·
  <a href="docs/README.ko.md">한국어</a> ·
  <a href="docs/README.es.md">Español</a> ·
  <a href="docs/README.fr.md">Français</a> ·
  <a href="docs/README.fa.md">فارسی</a>
</p>

Claude Code compacts a long session automatically: it deletes most of your
conversation, replaces it with a summary, and carries on. You usually notice
because the model starts re-asking things you settled two hours ago.

This repo takes the other approach. It measures how full the session really is,
and when the end is genuinely close it exports the whole session to disk and
continues in a fresh one that **reads the parent transcript in full** before
doing anything else. Nothing is summarised away, and the chain of sessions stays
visible and resumable.

It also diagnoses a configuration trap worth knowing about even if you never
install any of this — see [The clamp](#the-clamp).

---

## Install

Requires Python 3.8+ and Claude Code. Nothing else; no dependencies to install.

```bash
git clone https://github.com/IRDcode/claude-code-session-handoff
cd claude-code-session-handoff
python install.py --dry-run     # see every change first
python install.py
```

Then restart Claude Code and check what it found:

```bash
python ~/.claude/skills/long-session-handoff/scripts/session_weight.py --explain
```

The default install does **not** change how Claude Code manages context.
Auto-compaction stays exactly as it is; the guard simply hands off before it can
fire. To remove everything:

```bash
python install.py --uninstall
```

`settings.json` is backed up before it is touched, existing hooks and status
lines are left alone, and installing twice is a no-op.

## What gets installed

| path | what it is |
|---|---|
| `~/.claude/skills/long-session-handoff/` | the procedure the model follows, plus three scripts |
| `~/.claude/hooks/session-weight-watch.py` | the detector, on four events |
| `~/.claude/hooks/statusline-weight.py` | weight on the status line, every render |
| `~/.claude/runtime/` | anti-nag state, a log, and a measurement cache |
| `~/.claude/handoffs/` | the exports, and `chains.json` linking parent to child |

Four hook events are registered — `UserPromptSubmit`, `SessionStart`,
`PreCompact`, `PostCompact`. Your existing hooks on those events are preserved.

## How it differs from default behaviour

| | default Claude Code | with this installed |
|---|---|---|
| when the session fills | auto-compaction fires; most of the conversation is discarded and replaced by a summary | you are offered a handoff first, well before that point |
| what the next session knows | whatever the summary captured, written by the agent that was already losing track | the parent's own transcript, read in full and verified by counts |
| history of dropped context | unreferenced in the live session | recovered from disk into `05-dropped-context.md` |
| how full is it, really? | `/context` shows % of the window | the status line shows % of the wall that actually ends the session |
| finding the continuation later | scroll `/resume` | `chains.json` records parent, child, weight at migration, and whether the read was verified |

The status line looks like this:

```
Opus 5 | ████████░░ 85% 830k/977k | 696t 402tc 6.1h | HANDOFF DUE (5) | no-compact
```

Percent of the **wall**, not of the window. Those differ, sometimes by a factor
of five, which is the whole point of the next section.

## The clamp

Worth reading even if you install nothing.

Claude Code has two different points at which a session ends:

```
compaction fires at    window  − reply reserve (~20k) − summary buffer (~13k)
sending is refused at  ceiling − reply reserve (~20k) − margin (~3k)
```

The first applies when auto-compaction is on, the second when it is off. So a
200,000-token window compacts at roughly **167,000**.

Here is the trap: **`autoCompactWindow` in `settings.json` is clamped to the
model ceiling, silently.** Ask for 1,000,000 against a 200,000 ceiling and you
get 200,000 — with nothing in the UI saying so. A session configured for a
million tokens gets compacted at 167,000, three times in a row, while ~830,000
tokens of paid-for context sit unused.

That is not hypothetical. It is where this repo came from: three compactions at
`preTokens` **167,398 / 167,071 / 166,904**, against a settings file that said
`autoCompactWindow: 1000000`.

`--explain` shows you which case you are in:

```
WINDOW
  client reported  1,000,000
  ceiling          1,000,000   (source DISABLE_COMPACT+CLAUDE_CODE_MAX_CONTEXT_TOKENS)
  resolved         1,000,000   (source settings)

WALL -- the token count past which no more work happens here
  1,000,000 ceiling - 20,000 reply reserve - 3,000 margin
  = 977,000   then SENDING IS REFUSED (no summary; a handoff is the only exit)
```

If it says `settings CLAMPED to …`, your window setting is being floored.

One configuration escapes the clamp: `DISABLE_COMPACT=1` together with
`CLAUDE_CODE_MAX_CONTEXT_TOKENS`. The installer can set that for you, but it
asks first and tells you the cost, because **it also disables manual
`/compact`**:

```bash
python install.py --disable-compact --window 1000000
```

With that set, the session no longer ends in a summary — it ends in a refusal to
send. That is a real trade. A refusal is survivable: you hand off and carry on.
A silently destroyed history is not. But if you ignore every prompt all the way
to the wall, that session stops accepting turns, and you should know that going
in. The guard fires at 85%, leaving ~147,000 tokens of room, so in practice it
does not get there.

Skip this flag if you would rather keep `/compact`. The handoff still works.

## Compatibility

**Claude Code versions.** Nothing here patches or wraps Claude Code. It reads two
documented interfaces — the hook stdin/stdout contract and the status-line payload
— and parses the JSONL transcripts the client already writes. That is why it
survives updates that would break a tool built on internals.

Where an exact number is needed, it is taken from evidence rather than asserted:

1. The window comes from `context_window_size`, which the client reports about
   itself on every status-line render.
2. The compaction trigger, if the session has ever been compacted, comes from the
   `preTokens` recorded in the transcript at that moment — the trigger observed,
   not computed. `score()` prefers it over its own arithmetic and says
   `corrected from observed preTokens` when it does.
3. Only with neither of those does it fall back to the reserve arithmetic, and
   `--explain` shows every input so drift is visible rather than silent.

If a future release moves the reserves, a session that gets compacted once teaches
the tool the new trigger.

**Older Claude Code versions.** The four hook events and the status line have been
stable for many releases. If a hook event is missing on your version, that hook
never fires and the rest still works — the detector is additive, not a
replacement. `--disable-compact` is the one part that depends on specific setting
names; `--explain` will tell you if it had no effect.

**Operating systems.** Pure Python, no dependencies, no compiled parts. Paths go
through `os.path`, `CLAUDE_CONFIG_DIR` is honoured everywhere, and the installer
picks an interpreter name that works through *your* shell rather than hardcoding
one. The only platform-specific code is forcing UTF-8 on stdout, which Windows
needs and which is harmless elsewhere.

Verify the whole thing on your own machine:

```bash
python tests/test_session_weight.py    # arithmetic, the gate, the two traps
python tests/test_compat.py            # syntax floor, entry points, hook output
```

`test_compat.py` finds every other Python installed on your machine and re-runs the
suite under each, so a version difference shows up as a failure rather than a
surprise later.

## When it triggers

Seven signals are measured. Context has the only vote on **whether** to move;
the rest only sharpen **how urgent** it is.

| signal | threshold |
|---|---|
| context vs the wall | ≥ 85% → hand off, ≥ 95% → stop asking and act |
| assistant turns | ≥ 900 |
| tool calls | ≥ 600 |
| active working time | ≥ 4 h |
| auto-compaction already fired | any |

**Nothing is offered below 62% of the wall, whatever else tripped.** This gate
exists because the other signals are proxies for context pressure invented for a
world where context could not be measured directly. Measured against the session
that built this: 4.3 h of work plus two earlier compactions scored "hand off
now" while context sat at 147,527 of 977,000 — 15%. Moving then would have
thrown away 829,473 tokens to save nothing.

Two measurement details that matter more than they look:

- **Active time is the sum of gaps under 10 minutes**, never last-minus-first. A
  session left open overnight reads as 44 h of span and 11 h of work; scoring
  the span fires a handoff on an idle session.
- **Compactions are counted from the typed transcript row**, never by searching
  for a marker string. Search for the marker once and it appears in your own
  tool output, and the count inflates itself.

The prompt appears at most once per tier — 200 more turns, or another tenth of
the wall — with a 15-minute floor. It never fires inside a subagent.

## The handoff itself

```
measure  →  ask  →  export  →  create the continuation  →  it reads the parent
```

The export writes five files: every user message verbatim (including the ones
sent mid-turn, which are easy to lose), every substantial assistant message, the
full transcript with tool payloads clipped, an index of counts, and whatever
earlier compactions discarded.

The continuation is then created and **woken headlessly** to read the export
before you ever open it. It must reply with counts that match the index — if
they do not match, the read was partial and the handoff is not done. That read
happens in a session nobody is waiting on, so the expensive part of a migration
costs you no wall-clock time.

Afterwards you get the id and the name:

```
claude --resume 7157caa1-11ce-4f29-a46a-09913d483fb0
```

or search `/resume` for the name, which carries the parent's topic words plus
`(cont. 2)`.

## Verify it yourself

Every claim above is checkable on your own machine. The scripts print numbers,
not reassurance:

```bash
# where does my session actually end, and why?
session_weight.py --explain

# what is the current weight, with every signal named?
session_weight.py --session-id <uuid>

# machine-readable
session_weight.py --session-id <uuid> --json
```

To confirm a config change took effect, do not trust the file — read the
transcript. Find the first turn whose token total passes the old trigger and
check that no new compaction row follows it.

## Limitations

Stated plainly, because a tool that measures things should be honest about what
it has not measured:

- The reserves (~20k / ~13k / ~3k) are derived from observed behaviour. A future
  release could change them — see [Compatibility](#compatibility) for the three
  layers of evidence that protect against that, and note that layer 3 is the
  guess.
- The send-refusal wall has been computed and corroborated, not deliberately hit.
  The guard is designed so you never reach it.
- Tested on Windows with Python 3.11, 3.12 and 3.14, and syntax-checked against
  the 3.8 grammar. Linux and macOS should be fine — nothing platform-specific
  remains beyond console encoding — but neither has been run end to end.
- The five-file export and the scorer are exercised by the test suite. The headless
  wake turn is the part that depends on your `claude` binary being launchable; if
  it is not, the export still succeeds and the tool tells you what to do instead.
- Prompt caching: a handoff starts a new session, so its cache starts cold. For
  a session near the wall that is a good trade; it is still a cost.

## Contributing

Bug reports welcome, especially "the numbers were wrong on my setup" — include
the output of `--explain`. If a Claude Code release moves the arithmetic, that is
the report that fixes it fastest.

Before opening a PR, run both suites:

```bash
python tests/test_session_weight.py
python tests/test_compat.py
```

## Security

`SECURITY.md` documents exactly what this reads, what it writes, and what it
sends over the network (nothing). Worth a look before installing anything that
touches your session files.

## License and credit

MIT — see [LICENSE](LICENSE). Free to use, modify, and redistribute, including
commercially. The one condition is that the copyright notice and licence text
travel with it, so a fork or a repackaged copy still says where it came from.

If you use the approach or the findings — particularly the clamped-window
diagnosis — a link back is appreciated. `CITATION.cff` is there so GitHub's
"Cite this repository" button produces something correct.

Authored by [IRDkiya](https://github.com/IRDcode).
