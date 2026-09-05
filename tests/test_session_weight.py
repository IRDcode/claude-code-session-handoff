#!/usr/bin/env python3
"""Tests for the wall arithmetic, the gate, and the transcript measurement.

    python tests/test_session_weight.py

No test framework required -- these run anywhere Python does, which matters for
a tool people install by cloning a repo. Each test builds a synthetic transcript
or a throwaway config directory, so nothing here reads your real sessions.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "skill",
                                "long-session-handoff", "scripts"))

FAILURES = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        FAILURES.append(label)


def check_near(label, got, want, slack=1500):
    if abs(got - want) <= slack:
        print(f"  ok    {label}  ({got:,})")
    else:
        print(f"  FAIL  {label}\n          got  {got:,}\n          want ~{want:,}")
        FAILURES.append(label)


class Sandbox:
    """A throwaway CLAUDE_CONFIG_DIR with a settings.json we control."""

    def __init__(self, settings=None, env=None):
        self.settings = settings or {}
        self.env = env or {}

    def __enter__(self):
        self.dir = tempfile.mkdtemp(prefix="cch-test-")
        with open(os.path.join(self.dir, "settings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(self.settings, fh)
        self.saved_env = {}
        for k in ("DISABLE_COMPACT", "DISABLE_AUTO_COMPACT",
                  "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                  "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                  "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
                  "CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE"):
            self.saved_env[k] = os.environ.pop(k, None)
        os.environ.update({k: str(v) for k, v in self.env.items()})

        import session_weight as sw
        self.sw = sw
        self.saved_dir = sw.CLAUDE_DIR
        # Every path in the module is derived from CLAUDE_DIR at call time, so
        # redirecting it here also redirects the runtime cache. That is the
        # point of not binding paths as default arguments: a real cached window
        # on the developer's machine cannot leak into these assertions.
        sw.CLAUDE_DIR = self.dir
        return sw

    def __exit__(self, *exc):
        self.sw.CLAUDE_DIR = self.saved_dir
        for k, v in self.saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

def test_the_clamp():
    """The bug this project exists for: a window setting floored in silence."""
    print("\nthe clamp")
    with Sandbox({"autoCompactWindow": 1000000},
                 {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"}) as sw:
        window, source = sw.auto_compact_window()
        check("asking for 1M against a 200k ceiling resolves to 200k",
              window, 200000)
        check("and says so in the source", "CLAMPED" in source, True)
        check_near("compaction fires at 167,000 -- the observed preTokens",
                   sw.compact_threshold(), 167000)
        check("limit_kind is 'compact'", sw.limit_kind()[0], "compact")


def test_escape_from_the_clamp():
    """DISABLE_COMPACT + CLAUDE_CODE_MAX_CONTEXT_TOKENS raises the ceiling."""
    print("\nescaping the clamp")
    with Sandbox({"autoCompactWindow": 1000000, "autoCompactEnabled": False},
                 {"DISABLE_COMPACT": "1",
                  "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"}) as sw:
        check("compaction is off", sw.compact_enabled()[0], False)
        check("ceiling rises to 1M", sw.model_window()[0], 1000000)
        check("window is no longer clamped",
              "CLAMPED" in sw.auto_compact_window()[1], False)
        check_near("the wall becomes the send-refusal wall at 977,000",
                   sw.compact_threshold(), 977000)
        check("limit_kind is 'blocked'", sw.limit_kind()[0], "blocked")


def test_max_context_tokens_alone_does_nothing():
    """Without DISABLE_COMPACT the ceiling stays put. This is the trap."""
    print("\nCLAUDE_CODE_MAX_CONTEXT_TOKENS on its own")
    with Sandbox({}, {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"}) as sw:
        check("ceiling unchanged", sw.model_window()[0], 200000)
        check_near("wall unchanged", sw.compact_threshold(), 167000)


def test_client_reported_window_wins():
    """What the client says about itself beats every local inference."""
    print("\nclient-reported window")
    with Sandbox({"autoCompactWindow": 1000000}) as sw:
        check_near("before: inferred 200k ceiling", sw.compact_threshold(), 167000)
        sw.remember_window(1000000)
        check("ceiling now sourced from the client",
              sw.model_window(), (1000000, "client-reported"))
        check_near("wall follows it", sw.compact_threshold(), 967000)
        check("a bogus value is ignored", sw.remember_window(0), None)


def test_default_when_nothing_is_configured():
    print("\nunconfigured machine")
    with Sandbox({}) as sw:
        check("compaction on by default", sw.compact_enabled()[0], True)
        check("ceiling falls back to the conservative default",
              sw.model_window(), (200000, "default"))
        check_near("wall is the 200k trigger", sw.compact_threshold(), 167000)


def test_parse_window():
    print("\nwindow parsing")
    with Sandbox({}) as sw:
        cases = [(1000000, 1000000), ("1M", 1000000), ("500k", 500000),
                 ("200", 200000), (200000, 200000),
                 ("auto", None), ("", None), (None, None), ("nonsense", None),
                 (50, None), (99999, None), (2000000, None)]
        for raw, want in cases:
            check(f"  {raw!r} -> {want!r}", sw._parse_window(raw), want)

def write_transcript(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def asst(ts, tokens=None, tools=0):
    row = {"type": "assistant", "timestamp": ts,
           "message": {"model": "test-model", "content": []}}
    for i in range(tools):
        row["message"]["content"].append(
            {"type": "tool_use", "name": "Bash", "id": f"t{i}"})
    if tokens is not None:
        row["message"]["usage"] = {"input_tokens": tokens,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0,
                                   "output_tokens": 0}
    return row


def test_active_time_is_not_span():
    """Trap 1: a session left open overnight is not a session worked overnight."""
    print("\ntrap 1: active time vs span")
    with Sandbox({}) as sw:
        tmp = tempfile.mkdtemp(prefix="cch-t1-")
        try:
            p = os.path.join(tmp, "s.jsonl")
            # Two minutes of work, an eight-hour gap, two more minutes.
            write_transcript(p, [
                asst("2026-01-01T10:00:00Z"),
                asst("2026-01-01T10:01:00Z"),
                asst("2026-01-01T10:02:00Z"),
                asst("2026-01-01T18:02:00Z"),
                asst("2026-01-01T18:03:00Z"),
                asst("2026-01-01T18:04:00Z"),
            ])
            sig = sw.measure(p)
            check("span is 8.07h", round(sig["span_hours"], 2), 8.07)
            check("active time is 0.07h -- the gap is excluded",
                  round(sig["active_hours"], 2), 0.07)
            check("active never exceeds span",
                  sig["active_hours"] <= sig["span_hours"], True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_compactions_come_from_typed_rows():
    """Trap 2: a marker inside tool output is not a compaction."""
    print("\ntrap 2: typed rows only")
    with Sandbox({}) as sw:
        tmp = tempfile.mkdtemp(prefix="cch-t2-")
        try:
            p = os.path.join(tmp, "s.jsonl")
            write_transcript(p, [
                asst("2026-01-01T10:00:00Z", tokens=1000),
                # A real one.
                {"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2026-01-01T10:01:00Z",
                 "compactMetadata": {"trigger": "auto", "preTokens": 167398,
                                     "cumulativeDroppedTokens": 155261}},
                # A tool result that merely CONTAINS the marker text. A grep-based
                # counter reports 3 compactions here; this must report 1.
                {"type": "user", "timestamp": "2026-01-01T10:02:00Z",
                 "message": {"role": "user", "content": [
                     {"type": "tool_result",
                      "content": 'grep found: subtype":"compact_boundary" twice: '
                                 'compact_boundary compact_boundary'}]}},
                asst("2026-01-01T10:03:00Z", tokens=2000),
            ])
            sig = sw.measure(p)
            check("exactly one compaction counted", sig["compactions"], 1)
            check("dropped tokens read from metadata",
                  sig["dropped_tokens"], 155261)
            check("the tool_result is not counted as a user message",
                  sig["user"], 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_the_gate():
    """Proxies must never open the gate on their own."""
    print("\nthe gate")
    with Sandbox({}, {"DISABLE_COMPACT": "1",
                      "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"}) as sw:
        # A heavy session by every proxy measure: long, tool-heavy, already
        # compacted. Only the context fraction varies.
        sig = {"rows": 5000, "assistant": 950, "user": 40, "tool_calls": 700,
               "ctx_tokens": 0, "ctx_max": 0, "compactions": 3,
               "dropped_tokens": 464752, "active_hours": 6.0, "span_hours": 7.4,
               "title": "t", "model": "m", "started": None,
               "last_activity": None, "transcript": "x", "bytes": 0,
               "tool_census": {}}
        wall = sw.compact_threshold()

        pts, _, urgency, extras = sw.score(sig, ctx_override=int(wall * 0.15))
        check("score is high on proxies alone", pts >= 3, True)
        check("...but urgency is capped at 'soon'", urgency, "soon")
        check("...and it says it was gated", extras["gated"], True)

        _, _, urgency, extras = sw.score(sig, ctx_override=int(wall * 0.61))
        check("still gated just under 62%", urgency, "soon")

        _, _, urgency, extras = sw.score(sig, ctx_override=int(wall * 0.63))
        check("opens just over 62%", urgency, "due")
        check("no longer gated", extras["gated"], False)

        _, _, urgency, _ = sw.score(sig, ctx_override=int(wall * 0.86))
        check("due at 85%+", urgency, "due")

        _, _, urgency, _ = sw.score(sig, ctx_override=int(wall * 0.96))
        check("critical at 95%+", urgency, "critical")


def test_score_reports_the_live_wall():
    print("\nextras reported by score()")
    with Sandbox({}, {"DISABLE_COMPACT": "1",
                      "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"}) as sw:
        sig = {"assistant": 10, "tool_calls": 5, "active_hours": 0.5,
               "span_hours": 0.5, "compactions": 0, "dropped_tokens": 0,
               "ctx_tokens": 100000}
        _, _, _, extras = sw.score(sig)
        check("limit_kind is carried in extras", extras["limit_kind"], "blocked")
        check_near("threshold is the send-refusal wall",
                   extras["threshold"], 977000)
        check("headroom is wall minus context",
              extras["headroom"], extras["threshold"] - 100000)


def test_evidence_beats_arithmetic():
    """A real preTokens overrides the computed trigger.

    This is the forward-compatibility guarantee: if a release moves the reserves,
    the arithmetic goes stale but the transcript does not. A session that has
    been compacted knows exactly where its trigger is.
    """
    print("\nevidence overrides arithmetic")
    with Sandbox({}) as sw:
        base = {"assistant": 10, "tool_calls": 5, "active_hours": 0.5,
                "span_hours": 0.5, "compactions": 0, "dropped_tokens": 0,
                "ctx_tokens": 0, "max_pre_tokens": 0}

        # No compaction yet: fall back to arithmetic.
        _, _, _, extras = sw.score(dict(base), ctx_override=1000)
        check_near("no evidence -> computed 167,000", extras["threshold"], 167000)

        # A release moved the reserves and compaction now fires at 150,000.
        moved = dict(base, compactions=1, max_pre_tokens=150000)
        _, _, _, extras = sw.score(moved, ctx_override=1000)
        check("evidence is used instead", extras["threshold"], 150000)
        check("and the source says so",
              "corrected from observed" in extras["window_source"], True)

        # Rounding-level disagreement is not a correction.
        close = dict(base, compactions=1, max_pre_tokens=167400)
        _, _, _, extras = sw.score(close, ctx_override=1000)
        check_near("a 400-token gap is left alone", extras["threshold"], 167000)

        # With compaction OFF the wall is the send-refusal wall, and a historic
        # preTokens from when compaction WAS on must not drag it down.
        os.environ["DISABLE_COMPACT"] = "1"
        os.environ["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = "1000000"
        try:
            _, _, _, extras = sw.score(moved, ctx_override=1000)
            check_near("ignored when compaction is disabled",
                       extras["threshold"], 977000)
        finally:
            os.environ.pop("DISABLE_COMPACT", None)
            os.environ.pop("CLAUDE_CODE_MAX_CONTEXT_TOKENS", None)


def test_observed_trigger_reads_pretokens():
    print("\nmax_pre_tokens is parsed from the transcript")
    with Sandbox({}) as sw:
        tmp = tempfile.mkdtemp(prefix="cch-pre-")
        try:
            p = os.path.join(tmp, "s.jsonl")
            write_transcript(p, [
                asst("2026-01-01T10:00:00Z", tokens=1000),
                {"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2026-01-01T10:01:00Z",
                 "compactMetadata": {"preTokens": 167398,
                                     "cumulativeDroppedTokens": 155261}},
                {"type": "system", "subtype": "compact_boundary",
                 "timestamp": "2026-01-01T11:01:00Z",
                 "compactMetadata": {"preTokens": 167071,
                                     "cumulativeDroppedTokens": 309106}},
            ])
            sig = sw.measure(p)
            check("largest preTokens kept", sig["max_pre_tokens"], 167398)
            check("observed_trigger returns it",
                  sw.observed_trigger(sig), 167398)
            check("returns None with no compactions",
                  sw.observed_trigger({"max_pre_tokens": 0}), None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("session_weight tests")
    for fn in (test_the_clamp, test_escape_from_the_clamp,
               test_max_context_tokens_alone_does_nothing,
               test_client_reported_window_wins,
               test_default_when_nothing_is_configured,
               test_parse_window,
               test_active_time_is_not_span,
               test_compactions_come_from_typed_rows,
               test_the_gate,
               test_score_reports_the_live_wall,
               test_evidence_beats_arithmetic,
               test_observed_trigger_reads_pretokens):
        fn()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
