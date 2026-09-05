---
name: long-session-handoff
description: Chat handoff for Claude Code — measure the session's real weight, migrate it into a fresh session with every requirement intact, and read the parent IN FULL before continuing. Use when a session has run for hours or hundreds of tool calls, when auto-compaction has fired, or whenever you are standing IN a continuation session.
---

# Long-session handoff

A long session degrades invisibly from the inside. Auto-compaction deletes the
middle of the conversation and hands you a summary of your own work; decisions
get re-litigated; the same bug gets fixed twice. The user sees it as slowness
and repetition long before any tool reports a problem.

The fix is not to keep going and not to compact. It is to **measure the weight,
ask permission, and continue in a fresh session that has read the parent** — so
nothing is lost and the lineage is visible.

## RULE ZERO — the continuation reads the parent IN FULL, always

Not conditional, not "if it seems relevant", not a summary. **Every handoff, the
continuation session reads the parent's own transcript before it does anything
else**, and it proves the read with counts.

Why the brief is never enough: a brief is written BY the agent that is handing
off, so it inherits that agent's blind spots — and those blind spots are exactly
what made the session run long. The transcript has the user's own wording, every
"no", every instruction they had to repeat, and the asks that were quietly
dropped. A brief has the agent's version of those.

One command does the export:

```bash
python ~/.claude/skills/long-session-handoff/scripts/dump_parent_session.py \
    <parent_session_id> [out_dir]
```

| file | what it is | read it? |
|---|---|---|
| `01-user-messages.md` | every user message, verbatim, deduped, mid-turn ones flagged | **IN FULL. No skimming.** |
| `02-decisions.md` | assistant messages >= 150 chars — decisions and findings | in full |
| `03-full-transcript.txt` | everything: text, THINKING, tool calls, tool results | **grep it**, never read top to bottom |
| `04-index.json` | counts to check your read against | check the numbers |
| `05-dropped-context.md` | what auto-compaction discarded, recovered from disk | in full, if it exists |

For scale, from a real 1,361-row session (424 assistant turns, 250 tool calls):
`01` was 5.3 KB, `02` 10 KB, `04` 1.3 KB, `05` 49 KB — and `03` 472 KB. The part
that must be read completely came to ~65 KB. There is no excuse for skipping it.

Gate, out loud, in the continuation's first message: *"parent read: N rows, U
unique user messages, M mid-turn corrections"*. If you cannot quote the counts,
you did not read it and the handoff is not done.
## When to use

Use this when a session is still productive in principle but has become heavy:
hours of active work, hundreds of tool calls, auto-compaction has fired, or the
user says it feels slow or that you forgot something. The work is unfinished and
must continue — just not here.

Also use it whenever you are standing IN a continuation session, because Rule
Zero says the parent gets read first.

Do NOT use it for a short session that merely changed topic, and never in the
middle of a running batch, deploy, or write.

## This skill fires itself

A skill is pulled by the model, so left alone nothing triggers it. That is fixed
from the runtime side by a hook:

```
~/.claude/hooks/session-weight-watch.py          registered on 4 events
~/.claude/skills/long-session-handoff/scripts/session_weight.py    the scorer
~/.claude/runtime/session-weight-watch.json      anti-nag state
~/.claude/runtime/session-weight-watch.log       what it decided and when
```

| event | what it does |
|---|---|
| `UserPromptSubmit` | measures every prompt; offers a handoff when the score fires, at most once per tier |
| `SessionStart` (resume/fork/compact) | the payload carries `context_tokens`, `seconds_since_last_response` and `prompt_cache_likely_expired` — fresher than the transcript, and the cheapest moment to decide |
| `PreCompact` | last chance before context is destroyed: tells you to dump the parent |
| `PostCompact` | context is already gone: tells you to recover it from disk instead of trusting the summary |

The notice is injected as `additionalContext` on the **user turn**, not the
system prompt, so prompt caching is untouched. It fails open: any exception
logs and returns `{"suppressOutput": true}`, because a weight watcher that
blocks a turn is worse than no weight watcher.

**When you see a `[SESSION WEIGHT …]` block, that is the runtime talking, not
the user.** Finish answering the current question first, then run the
measure → ask → migrate procedure below.

It never fires inside a subagent (`agent_id` present ⇒ silent): a subagent has
its own short context and cannot hand anything off.

Anti-nag: state is keyed by session id and bucketed on two axes —
`assistant_rows // 200` plus `int(pct_of_trigger * 10)` — so a declined offer
does not return until the session grows another ~200 turns *or* crosses another
tenth of the trigger. Plus a hard 15-minute cooldown. At `>= 95%` of the trigger
the anti-nag is bypassed: at that point compaction is seconds away.
## Measuring the weight

Do not guess and do not eyeball `/context`. One command:

```bash
python ~/.claude/skills/long-session-handoff/scripts/session_weight.py \
    --session-id <this-session-id> [--json]
```

It parses the transcript once and prints every signal. Three or more points
means hand off — **but only if the context gate is open** (below):

| signal | threshold | points |
|---|---|---|
| context vs the live wall (compact trigger, or send-refusal when compaction is off) | >= 85% | 2 |
| " | >= 62% | 1 |
| assistant rows | >= 900 | 1 |
| tool calls | >= 600 | 1 |
| **active** working time (gaps < 10 min summed) | >= 4 h | 1 |
| auto-compaction has already fired | any | 2 |
| distinct sub-tasks completed (your judgement, not measurable) | >= 5 | 1 |

### The context gate — the one rule that stops this skill wasting tokens

**Nothing is offered below 62% of the wall, whatever else tripped.** Score can
reach 5 and urgency is still capped at `soon`.

Found by running the scorer against the session that built it: 4.3 h active plus
two earlier compactions = score 3 = DUE, at **147,527 of a 977,000 wall —
15.1%**. Handing off there would have abandoned 829,473 tokens of paid-for
headroom to save nothing, and the continuation would have restarted at ~25,000
tokens to re-learn what the parent already knew.

The other signals are **proxies** for context pressure, invented for a world
where context could not be measured directly. Here it can be. So they no longer
vote on whether to move; they only sharpen the urgency once context says moving
is due.

A prior compaction is the clearest case: it is a permanent historical fact, so
scored as a trigger it fires forever — on a session whose context is now small
*precisely because* it was compacted. The right response to a past compaction is
to recover the dropped rows from `05-dropped-context.md` and fix the window (see
the two-walls section), never to migrate a session that still has room.

Observed behaviour after the gate, on a 977,000 wall with `limit_kind = blocked`
and signals 696 assistant / 402 tool calls / 6.07 h active / 3 prior compactions:

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

`handoff.py` refuses on urgency, not on score, for the same reason.

Two or fewer, or gated: keep working. Write nothing, prepare nothing — a handoff
executed early throws away context you already paid for.

### The context signal is a fraction of the wall, not of the window

There are **two** walls, and which one you are running at depends on whether
auto-compaction is enabled:

```
compaction fires at   window  − output_reserve − summary_buffer
sending is refused at ceiling − output_reserve − blocked_reserve
```

The reserves are ~20,000 for the reply, ~13,000 for the summary, ~3,000 of margin
under the ceiling. So a 200,000 window compacts at about **167,000**.

The trap: **`autoCompactWindow` in `settings.json` is clamped to the ceiling,
silently.** Asking for 1,000,000 against a 200,000 ceiling gives you 200,000 —
and a 167,000 trigger — with nothing in the UI saying so.

That is not a theory. On the machine this was built for, three compactions landed
at `preTokens` **167,398 / 167,071 / 166,904** while `settings.json` said
`autoCompactWindow: 1000000`. Pointing the scorer at a copy of those old settings
reproduces **167,000** exactly.

Verify it yourself rather than trusting this text:

```bash
python ~/.claude/skills/long-session-handoff/scripts/session_weight.py --explain
```

One configuration escapes the clamp: **`DISABLE_COMPACT=1` together with
`CLAUDE_CODE_MAX_CONTEXT_TOKENS`**. On several models the ceiling only rises when
compaction is disabled; `CLAUDE_CODE_MAX_CONTEXT_TOKENS` alone is ignored for
this purpose. Corroborating evidence that the larger window is real and the clamp
is what stands in the way: an older session on an earlier CLI release reached
exactly 1,000,000 tokens with **zero** compactions, stopping with
`model_context_window_exceeded` — same account, same model family.

Disabling compaction does not remove the deadline. It moves it from ~167,000 to
**977,000** and changes the consequence from *your history is silently destroyed*
to *the next turn is refused*. A refusal is survivable — you hand off and
continue. A destroyed history is not. That is the whole trade, and it is only
safe because `compact_threshold()` returns whichever wall is live and
`limit_kind()` says which one it is.

### Why 85% and not 60%

On a 977,000 wall the handoff fires at ~830,000 and leaves 147,000 of headroom.
Executing a handoff costs ~25,000 tokens of context — the dump runs as a
subprocess and returns counts, not content. 147,000 is a 5x margin, so the
handoff always wins the race against the wall while leaving nothing meaningful
unused. On a 200,000 window the same 85% leaves ~26,000, which is tighter — that
is what `CTX_CRITICAL_PCT` is for: at 95% it stops asking and acts.

Do not lower these "to be safe". Every token below the wall that this session
does not use is a token the next session has to re-establish, and re-establishing
is the expensive half.

### Trap 1: elapsed time is not working time

Active time is the **sum of inter-row gaps under 10 minutes**, never MAX−MIN.
A session left open overnight reads as 44 h of span and 11 h of work; scoring
the span fires a handoff on an idle session. The scorer reports both and only
ever scores the active figure.

### Trap 2: a marker you READ is not a marker that happened

Count compactions from the **typed** row — `type: "system"`,
`subtype: "compact_boundary"` — and read the quantity from
`compactMetadata.cumulativeDroppedTokens`. Never grep for a marker string: the
moment anyone greps for it, the string appears verbatim inside tool output and
the count inflates itself. Counting the typed row is both exact *and*
quantitative: it says how many tokens were lost, not just that something
happened.

### Suppressing a proven false positive

```bash
# drop just this session's entry, keep the rest
python -c "import json,os,sys; p=os.path.expanduser('~/.claude/runtime/session-weight-watch.json'); d=json.load(open(p)); d.pop(sys.argv[1],None); json.dump(d,open(p,'w'),indent=1)" <session-id>
```

Or set `tier` absurdly high in that file to silence the session for good.
## Asking permission

**Ask, never decide alone.** Handing off is like running a destructive command:
the user says yes first. One `AskUserQuestion` call, three options, no prose
wall. State what was measured, what carries over, and what the new session will
be called.

```
header:   "Handoff"
question: "This session is at 822k of the 967k compact trigger (4.3h active,
           641 tool calls). I suggest continuing in a fresh session — every
           requirement, decision and measurement carries over, and the new
           session reads this one in full before it starts. What should I do?"
options:  "Hand off now (recommended)"  — dump, brief, create + wake the child
          "Keep going here"             — no offer again for 200 turns / 10% ctx
          "Finish this task first"      — then hand off at the boundary
```

Exactly one question, one call. If the answer is "keep going", respect it — the
anti-nag already enforces that, and asking twice in one session is the fastest
way to make the user disable the whole thing.

Skip the question in one case only: the `PostCompact` / critical notice, where
compaction has already fired or is seconds away. Then dump first and tell the
user afterwards — the context is being destroyed either way, and asking costs
the thing you are trying to save.

## Naming the continuation

The name is the only handle `/resume` can search on, so it has to carry the
topic words verbatim. Pattern:

```
<original topic> (cont. 2)
<original topic> (cont. 3)
```

Keep the original topic words **verbatim**: that is what the user searches by.
Never invent a fresh title, never use a date or a hash, and never number from 1
— the original session is 1, so the first continuation is 2. `handoff.py`
derives this from the parent's own `ai-title` row and stamps the generation on,
counting the real chain depth, so a 4th-generation session is named `(cont. 4)`
and not `(cont. 2)` again.

## Navigation — the CLI has no chat list

A desktop chat app puts conversations down the left edge. The CLI has no such
list, so the handoff prints the two ways in that matter and records the rest:

| how | when to use it |
|---|---|
| `claude --resume <child-id>` | you have the id — `handoff.py` prints it, and it is in `~/.claude/handoffs/chains.json` |
| `/resume` then type the name | interactive picker, searchable; this is why the name must contain the topic words |
| `claude -c` | "just put me back in the last session" |
| `/branch`, `/fork` | split the *current* session instead of migrating it — no dump, no read, so not a handoff |
| `handoff.py --list` | every handoff ever made: parent, child, dump dir, whether the wake turn was accepted |
| `handoff.py --chain --parent <id>` | walk a lineage oldest-first, so a 5-generation chain is still legible months later |

The registry is the real answer to the navigation problem. A chat list shows you
titles; `chains.json` records parent → child, the dump directory, the weight at
the moment of migration, and whether the child accepted the handoff. That is
strictly more than the sidebar gave you.
## The handoff brief

Write the brief to a file **before** creating the child, so it survives even if
something goes wrong. Under a page. Contents, in this order:

1. **What we are building** — two sentences.
2. **Where it runs** — paths, the process, how to restart it.
3. **Decisions already made** — each with the measurement that justified it.
   This is the part that stops settled questions being re-litigated.
4. **Root causes already found** — one line each, so no bug is re-diagnosed.
5. **State of the work right now** — done, verified, committed.
6. **Open items** — what is still broken, and what the user asked for last.
7. **Standing instructions from the user** — tone, language, what they refuse.

Put the measurements in verbatim (token counts, timings, HTTP codes, ids). The
value of a long session is the facts it established; opinions do not carry over.

## Executing the handoff

One command does all of it:

```bash
python ~/.claude/skills/long-session-handoff/scripts/handoff.py \
    --parent <this-session-id> \
    --brief  <path-to-brief.md> \
    [--name "<topic> (cont. N)"]
```

It refuses to migrate a session scoring below 2 unless you pass `--force`, and
tells you how much headroom you would be throwing away. That refusal is a
feature: it is the guard against the failure mode where the skill exists and
therefore gets used.

What it does, in order:

1. **measures** the parent and records the numbers into the registry
2. **dumps** the parent as a subprocess — the multi-MB transcript never touches
   anyone's context; only `04-index.json`'s counts come back
3. **mints** the child session id and records parent → child in
   `~/.claude/handoffs/chains.json`
4. **wakes** the child with one headless turn: `claude -p "<Rule Zero>"
   --session-id <new-uuid> -n "<name>" --permission-mode auto`
5. **prints** the two ways to get there

Variants:

- `--prepare` — dump and register, create nothing. Use when the user wants the
  parent preserved but is not ready to move.
- `--no-wake` — create the child cold. Only when the user is about to open it
  themselves and a wake turn would race their first message.
- `--force` — migrate a light session anyway (topic change, cold cache).
- `--json` — machine-readable result, for chaining.

`--permission-mode auto`, never `bypassPermissions`. The wake turn only reads
files; a child that can act unsupervised is a different feature and not this one.

### The wake turn is the whole trick

This is what makes a CLI handoff beat compaction outright. The wake turn spends
one headless request having the child read `01`, `02`, `04`, the brief and
`05` — and then reply with a fixed acknowledgement and nothing else.

That read lands in the child's own transcript. So when the human resumes the
child, the requirement set is **already in its context and already paid for**.
The expensive half of a handoff happens off the interactive path, in a session
nobody is waiting on. The read is durable rather than a one-shot report: it lives
in the continuation's own transcript, so it survives being resumed later.

The child must reply exactly:

```
HANDOFF ACCEPTED
user messages read: <n>/<n_from_index>   unique: <n>
mid-turn corrections: <n>
decisions read: <n>
parent: <id>
generation: <n>
open threads: <one line each>
standing constraints: <one line each>
```

`handoff.py` greps for `HANDOFF ACCEPTED` and reports the wake as FAILED without
it. A child that exists but never read the dump is the worst outcome of this
skill — worse than not handing off — because it looks done and is not.
## Variant: the user already opened the new session

Sometimes the user starts a fresh session themselves and says "this chat got too
long — read from the old one". That is a *pull* handoff: the continuation already
exists and you are standing in it. Skip creation and skip the wake turn (nothing
is asleep; the user is right here).

1. Find the parent: `python .../handoff.py --list`, or by title:
   `grep -l '"aiTitle":"<words>"' ~/.claude/projects/*/*.jsonl`
2. Dump it: `python .../dump_parent_session.py <parent-id> <out-dir>`
3. Read `01` and `02` in full, `05` if it exists, then state the counts.
4. Record the link so the chain stays walkable:
   `python .../handoff.py --parent <parent-id> --child-id <this-id> --prepare`
5. Re-verify live state with one cheap command, report three lines, stop.

## Continuing in the new session

- **Read the parent in full first — the brief is not enough.** Rule Zero, before
  the brief, before `git status`, before answering anything.
- Treat any conflict as **the transcript being right** and the brief stale.
- What briefs routinely lose: every time the user said "no" and why;
  instructions they had to repeat; requests never completed; and the numbers —
  token counts, timings, exit codes — that exist only in tool output.
- Messages marked `[sent mid-turn]` in `01` outrank anything earlier that
  contradicts them. Those are corrections: the user interrupted to say it.
- A thinking model states little and reasons a lot. On this session: 37 text
  blocks against 130 thinking blocks and 215,683 characters of reasoning. So the
  *why* behind a decision is often not in `02` at all — grep `03` for
  `THINKING` near the term.
- Load the same skills the parent used, explicitly, by name.
- Re-verify live state with one cheap command rather than trusting the brief.
- State the parent's session id once so the user can jump back.

## Pitfalls

- **Trusting the `[SESSION WEIGHT …]` notice without checking it.** It is a
  measurement and measurements have bugs — this one shipped with two
  (elapsed-vs-active time, markers counted inside tool output) and both had to be
  fixed. When the user says the numbers feel wrong, run
  `session_weight.py` yourself and compare. A handoff offered on fabricated
  numbers is a mislabelled success.
- **Handing off silently.** The user loses trust instantly if a session "moves"
  without being asked. One `AskUserQuestion` first, every time except critical.
- **Handing off mid-operation.** Never switch while a batch, deploy, or write is
  in flight. Finish or cleanly cancel, confirm the system is at rest, then go.
- **Handing off too early.** The mirror-image failure, and the one this skill is
  most likely to commit, because the machinery is right there. Below score 2
  `handoff.py` refuses and prints the headroom you would waste. Believe it.
- **A brief that is a transcript.** Longer than a page and it will not be read;
  the new session starts as confused as the old one ended.
- **Reading the brief and calling that "reading the parent".** The brief is step
  2 of the read, never step 1, and never the whole of it.
- **Skipping `03` because "the prose restates it".** It frequently does not.
  Exit codes, byte counts, the exact JSON a hook emitted — those exist only in
  tool output.
- **Numbering from 1.** The original session is 1; the first continuation is 2.
- **Handing off and stopping there.** Brief written, child created, nothing
  happens because it was never woken. The user's approval is permission to MOVE
  the work, not to pause it. Always let the wake turn run unless the user is
  about to open the child themselves.
- **Leaving a process attached to the parent.** `subprocess.run` blocks until the
  wake turn finishes — that is deliberate, so the result can be verified — but it
  finishes and exits. Never background a long-running child from the parent
  session; nothing should stay owned by a conversation that is being retired.
- **Assuming `claude` on PATH is executable from Python.** On Windows it is a
  bash shim and `CreateProcess` cannot run it. `handoff.py` resolves the newest
  `~/.local/share/claude/versions/*/claude.exe`; override with `CLAUDE_CODE_BIN`.
- **Forcing stdout encoding on Windows.** A hook that emits an em dash through
  the console codepage either mangles the JSON or raises mid-write and truncates
  it. `emit()` reconfigures stdout to UTF-8 first. Verified: without it every
  em dash in the notice became `U+FFFD`.
- **Believing a config change took effect because the file says so.** Prove it
  from the transcript instead: find the first `usage` row whose
  `input + cache_creation + cache_read` exceeds the *old* trigger, and confirm no
  new `compact_boundary` row follows it. Worked example: three compactions at
  `preTokens` 167,398 / 167,071 / 166,904, settings changed at 00:21, first turn
  above 167,000 was 167,968 at 01:43 with no fourth compaction, peak 187,360.
  That is proof; a file on disk is not.
- **Assuming the client's own window figure is wrong.**
  `context_window.context_window_size` in the status-line payload is the running
  process answering the exact question `model_window()` otherwise has to infer.
  `statusline-weight.py` prefers it, caches it for the CLI tools, and only falls
  back to inference when it is absent — which is what keeps the wall correct
  across releases that move the arithmetic.
- **Trusting `autoCompactWindow` to be the window.** It is clamped to the model
  ceiling, silently. A settings file asking for 1,000,000 against a 200,000
  ceiling produced a 167,000 trigger and three compactions. The status line
  prints `window CLAMPED to 200k` when this happens.
- **`DISABLE_COMPACT` removes the emergency brake — deliberately.** It disables
  manual `/compact` too, so there is no summarise-in-place fallback: at the wall
  the client simply refuses to send. That is the *point*. Where the ceiling
  cannot be raised any other way, a fallback that destroys hundreds of thousands
  of tokens of history is worse than a refusal you can hand off ahead of. What
  makes it safe is the wall being measured and the handoff firing at 85% of it —
  not the brake existing.
## Configuration this skill assumes

`~/.claude/settings.json`:

```json
"autoCompactEnabled": false,
"env": {
  "DISABLE_COMPACT": "1",
  "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"
},
"statusLine": { "type": "command", "command": "python \"$HOME/.claude/hooks/statusline-weight.py\"" },
"hooks": {
  "UserPromptSubmit": [ { "hooks": [ { "type": "command", "command": "python \"$HOME/.claude/hooks/session-weight-watch.py\"", "timeout": 10 } ] } ],
  "SessionStart":     [ { "hooks": [ { "type": "command", "command": "python \"$HOME/.claude/hooks/session-weight-watch.py\"", "timeout": 10 } ] } ],
  "PreCompact":       [ { "hooks": [ { "type": "command", "command": "python \"$HOME/.claude/hooks/session-weight-watch.py\"", "timeout": 15 } ] } ],
  "PostCompact":      [ { "hooks": [ { "type": "command", "command": "python \"$HOME/.claude/hooks/session-weight-watch.py\"", "timeout": 15 } ] } ]
}
```

Set `CLAUDE_CODE_MAX_CONTEXT_TOKENS` to your model's real window — 1000000 where
that is available, 200000 otherwise. The skill works either way; it only needs to
know where the wall is.

**`env.DISABLE_COMPACT` is the load-bearing line, not `autoCompactWindow`.** The
window setting alone is floored to the ceiling without warning, which is exactly
how a session configured for 1,000,000 gets compacted at 167,000. `DISABLE_COMPACT`
is the one configuration where `CLAUDE_CODE_MAX_CONTEXT_TOKENS` raises the
ceiling, and setting it moves the end of the session from a ~167,000 compaction to
a ~977,000 send refusal. `autoCompactEnabled: false` is kept alongside it so
`/config` shows the same truth the env var enforces.

`PreCompact`/`PostCompact` stay registered even with compaction disabled: they
cost nothing when they never fire, and they are the audit trail if a future CLI
version resurrects the trigger.

The status line shows the weight every render: context used, percent of the live
wall, headroom, active hours, a `HANDOFF DUE` marker when the score fires, and
`window CLAMPED to …` if the clamp ever comes back.

## Verification

- [ ] The weight was measured with real numbers, not a feeling.
- [ ] Permission was asked in exactly one `AskUserQuestion` call — or the notice
      was `critical`/`PostCompact`, where asking costs what you are saving.
- [ ] Nothing was in flight at the moment of handoff.
- [ ] `dump_parent_session.py` ran and all five files exist on disk.
- [ ] The brief exists on disk and is under a page.
- [ ] The child was created BY YOU, its id recorded in `chains.json`, and its
      name contains the original topic words plus `(cont. N)`.
- [ ] The wake turn returned `HANDOFF ACCEPTED` with counts that match
      `04-index.json`. Counts that do not match mean the read was partial.
- [ ] The parent's `05-dropped-context.md` was read if auto-compaction had fired.
- [ ] The user was told what now exists and where — not asked to open a session.
- [ ] No process remains attached to the parent.
