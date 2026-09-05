# How it works

This is the long version: what is measured, why each number is what it is, and
how to check any of it yourself. The [README](../README.md) is enough to use the
tool; this is for deciding whether to trust it.

## The two walls

A Claude Code session does not end when the context window is full. It ends
earlier, at one of two points:

```
compaction fires at    window  − reply reserve − summary buffer
sending is refused at  ceiling − reply reserve − margin
```

Observed reserves: about 20,000 tokens held back for the model's reply, about
13,000 for the compaction summary, about 3,000 of margin under the ceiling.

Which wall you are running at depends on whether auto-compaction is enabled:

| auto-compaction | wall on a 200k window | wall on a 1M window | what happens there |
|---|---|---|---|
| on | ~167,000 | ~967,000 | history replaced by a summary, session continues |
| off | ~177,000 | ~977,000 | the client refuses to send; nothing continues |

`session_weight.py --explain` prints your own numbers with every input named.
`limit_kind()` reports which of the two is live.

## The clamp

`autoCompactWindow` in `settings.json` is not the window. It is clamped to the
model ceiling, and the clamp is silent:

```
requested 1,000,000  ∧  ceiling 200,000  →  resolved 200,000  →  trigger 167,000
```

No warning is printed, `/config` shows the requested value, and the session
compacts at 167,000 while ~830,000 tokens of window go unused.

**How this was established.** Three consecutive compactions in one session
recorded `preTokens` of 167,398, 167,071 and 166,904, against a settings file
asking for 1,000,000. 167,000 is exactly `200,000 − 20,000 − 13,000`. Pointing
the scorer at a copy of those settings reproduces 167,000 to the token.

Corroboration that the larger window is genuinely available and the clamp is what
stands in the way: an older session on an earlier CLI release, same account and
same model family, reached exactly 1,000,000 tokens with **zero** compactions and
stopped with `model_context_window_exceeded`.

One configuration escapes the clamp: `DISABLE_COMPACT=1` together with
`CLAUDE_CODE_MAX_CONTEXT_TOKENS`. `CLAUDE_CODE_MAX_CONTEXT_TOKENS` alone does not
raise the ceiling.

### Preferring the client's own answer

Rather than relying on the inference above, `statusline-weight.py` reads
`context_window.context_window_size` from the status-line payload on every
render. That is the running client stating its own window; it cannot be wrong
about itself, and it stays correct when a release changes the arithmetic.

The value is cached to `~/.claude/runtime/observed-window.json` so the CLI tools,
which receive no payload, agree with the status line. Inference is the fallback,
not the primary source.

## Scoring

Seven signals. Context decides **whether** to move; the rest only decide **how
urgent**.

| signal | threshold | points |
|---|---|---|
| context vs the wall | ≥ 85% | 2 |
| context vs the wall | ≥ 62% | 1 |
| assistant turns | ≥ 900 | 1 |
| tool calls | ≥ 600 | 1 |
| active working time | ≥ 4 h | 1 |
| auto-compaction already fired | any | 2 |
| distinct sub-tasks completed (model's judgement) | ≥ 5 | 1 |

Three points fires. Urgency: `soon` → `due` at 3 points or 85%, `critical` at
95%.

### The gate

**Nothing is offered below 62% of the wall, whatever else tripped.** Score can
reach 5 and urgency is still capped at `soon`.

This is the rule that keeps the tool from wasting what it is meant to protect.
Measured against the session that built it: 4.3 h of active work plus two earlier
compactions scored 3 = "hand off now", while context sat at **147,527 of 977,000
— 15.1%**. Handing off there would have abandoned 829,473 tokens of paid-for
headroom to save nothing, and the continuation would have restarted at ~25,000
tokens to re-learn what the parent already knew.

Turns, tool calls, hours and past compactions are *proxies* for context pressure,
invented for a world where context could not be measured directly. Here it can
be, so they no longer get a vote.

A prior compaction is the clearest case. It is a permanent historical fact:
scored as a trigger it fires forever, on a session whose context is now small
*precisely because* it was compacted. The right response to a past compaction is
to recover the dropped rows from disk and fix the window — not to migrate a
session that still has room.

Observed sweep, 977,000-token wall, signals 696 assistant / 402 tool calls /
6.07 h active / 3 prior compactions:

```
ctx  100,000   10.2%  score 3  soon      GATED
ctx  500,000   51.2%  score 3  soon      GATED
ctx  600,000   61.4%  score 3  soon      GATED
ctx  606,000   62.0%  score 4  due
ctx  700,000   71.7%  score 4  due
ctx  830,450   85.0%  score 5  due
ctx  928,150   95.0%  score 5  critical
ctx  977,000  100.0%  score 5  critical
```

### Why 85%

A handoff costs about 25,000 tokens of context: the export runs as a subprocess
and returns counts rather than content, and the brief is written to disk. At 85%
of a 977,000 wall that leaves ~147,000 tokens of headroom — a 5× margin, so the
handoff always wins the race.

On a 200,000 window the same 85% leaves ~26,000, which is tighter. That is what
the 95% level is for: it stops asking and acts.

Lowering these "to be safe" is counterproductive. Every token below the wall that
this session does not use is a token the next session has to re-establish, and
re-establishing is the expensive half.

## Two measurement traps

**Elapsed time is not working time.** Active time is the sum of gaps between
transcript rows under 10 minutes, never last-minus-first. One session measured
44 h of span against 11 h of work — scoring the span fires a handoff on a session
that was merely left open overnight. Both figures are reported; only the active
one is scored.

**A marker you read is not a marker that happened.** Compactions are counted from
the typed transcript row (`type: "system"`, `subtype: "compact_boundary"`) and
the quantity comes from `compactMetadata.cumulativeDroppedTokens`. Never search
for a marker string: the moment you do, the string appears verbatim in your own
tool output and the count inflates itself. Counting the typed row is both exact
and quantitative — it says how many tokens were lost, not just that something
happened.

A third one worth naming: **user messages sent mid-turn are not `user` rows.**
They arrive as attachments with `type: "queued_command"`. An exporter that reads
only `user` rows loses exactly the messages where the user corrected course —
which are the most important ones in the file.

## Verifying a configuration change

Do not trust `settings.json` to tell you what the running client is doing. Read
the transcript:

1. Find the first `usage` row whose
   `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`
   exceeds the *old* trigger.
2. Confirm no new `compact_boundary` row appears after it.

Worked example: three compactions at 167,398 / 167,071 / 166,904; settings
changed at 00:21; the first turn above 167,000 was 167,968 at 01:43; no fourth
compaction; peak 187,360. The change took effect without a restart — the client
re-resolves this state at each turn start.

## The handoff

```
measure  →  ask  →  export  →  create the continuation  →  it reads the parent
```

The export writes five files:

| file | contents | how the continuation reads it |
|---|---|---|
| `01-user-messages.md` | every user message verbatim, deduped, mid-turn ones flagged | in full, no skimming |
| `02-decisions.md` | assistant messages over 150 characters | in full |
| `03-full-transcript.txt` | everything, tool payloads clipped | grep, never top to bottom |
| `04-index.json` | counts to verify the read against | check the numbers |
| `05-dropped-context.md` | what earlier compactions discarded | in full, if it exists |

The 150-character floor for `02` is calibrated, not guessed. A reasoning model
writes little and thinks a lot: one session produced 37 visible text blocks
against 130 thinking blocks totalling 215,683 characters. A 400-character floor
kept 2 of the 37 — a decisions file with no "why" in it. 150 keeps 13–16.

### Rule Zero

The continuation reads the parent's transcript **in full**, every time, and
proves it with counts.

A brief is written by the agent that is handing off, so it inherits that agent's
blind spots — and those blind spots are what made the session run long. The
transcript has the user's own wording, every "no", every instruction they had to
repeat, and the asks that were quietly dropped.

This is not theoretical. In testing, a continuation woken on a real export
recovered two open threads from `05-dropped-context.md` that the brief did not
mention, and surfaced a standing instruction the handing-off agent had lost.

### The wake turn

The continuation is created and driven headlessly before anyone opens it:

```
claude -p "<the read instruction>" --session-id <uuid> -n "<name>" --permission-mode auto
```

It must reply `HANDOFF ACCEPTED` with counts matching `04-index.json`. Counts
that do not match mean the read was partial and the handoff is not done.

Doing this headlessly matters: the read lands in the continuation's own
transcript, already paid for, in a session nobody is waiting on. Measured at 33
seconds for a 9-message, 16-decision export.

## Navigation

There is no chat list in a terminal, so the chain is recorded instead. After a
handoff you get:

```bash
claude --resume <child-id>          # printed by handoff.py
handoff.py --list                   # every handoff, newest first
handoff.py --chain --parent <id>    # walk a lineage
```

`~/.claude/handoffs/chains.json` records more than a sidebar would: parent → child
links, the export directory, the measured weight at the moment of migration, and
whether the wake was accepted.

`/branch` and `/fork` are **not** handoffs. They copy context without an export
and without Rule Zero, so nothing verifies what carried over.

## Performance

The detector runs on every prompt, so it has to be cheap. Measured on a 5.6 MB
transcript:

```
full status-line render, cache miss   91 ms
full status-line render, cache hit    39 ms
transcript parse alone                35 ms
```

The parse is memoised on `(size, mtime)` — any append changes both, so a stale
entry is impossible. Hook latency is 36–84 ms including Python startup.

Every failure path is soft. A detector that blocks a turn is worse than no
detector, so any exception logs and returns `{"suppressOutput": true}`.
