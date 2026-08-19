"""budget.py — measure what a dispatch cost, and diagnose why.

Nothing in LaneGate reported what an agent dispatch actually spent. The only
limits were wall-clock ones (``idle_timeout``/``stall_timeout``/
``absolute_ceiling`` in :mod:`lanegate.orchestrate.run_report`), and an agent
making steady progress never trips those no matter how much it burns. Measured
on seven claude-sonnet-5 implement runs: 478 turns and 43.9M cumulative
cache-read tokens, worst case a single ticket at 151 turns / 16.7M tokens /
$6.58. Because each turn re-reads the whole conversation, cost grows
quadratically with turn count -- so the expensive tickets are worth finding.

**This module never stops a dispatch.** That was considered and deliberately
rejected. A cap punishes size rather than waste: a genuinely large ticket may
need 150 turns, and killing it destroys real work. Worse, a killed dispatch
that is retried starts a *new* conversation and re-explores the repository to
re-derive everything the dead session already knew, so kill-and-retry costs
strictly more than letting the original run finish. What was actually missing
was visibility, so visibility is all this provides.

The useful question is not "how many turns" but "why". Turn *mix* answers it,
because the two causes leave different fingerprints:

* Turns dominated by reads and searches -- especially repeat reads of files
  already read -- mean the agent is reconstructing structure the prompt failed
  to supply. That is a LaneGate problem, addressed by better context (skeletons
  for the touched files), not by a smaller ticket.
* Turns dominated by writes spread across many distinct files mean the ticket
  genuinely contains several changes. That is a ticket problem, addressed by
  splitting it.
* Turns dominated by repeated test runs mean the change keeps failing its own
  checks -- usually unclear or wrong acceptance criteria.

:meth:`DispatchMeter.diagnose` reports which pattern a run matched so the fix
lands in the right place instead of being guessed at.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

# Steps that dispatch an agent and therefore have a cost worth attributing.
# drift_check is included: it is a full agent dispatch, not a local computation.
METERED_STEPS = ("analyze", "implement", "review", "fix", "drift_check")

# Turn counts above which a dispatch is worth a human's attention. These are
# advisory thresholds for a report, not limits -- exceeding one changes nothing
# about how the run proceeds. Derived from the measured baseline above: seven
# implement runs averaged 68 turns, so ~100 is where a run stops being typical.
ADVISORY_TURN_THRESHOLDS: dict[str, int] = {
    "analyze": 40,
    "implement": 100,
    "review": 50,
    "fix": 50,
    "drift_check": 25,
}

# Share of turns spent re-deriving context (reads + searches) above which the
# prompt, rather than the ticket, is the more likely culprit.
_EXPLORATION_SHARE = 0.5
# Distinct files written above which the ticket is doing more than one thing.
_MULTI_CONCERN_WRITES = 5
# Repeat reads of an already-read file above which context is clearly not
# sticking -- the agent is paging the same material back in.
_REREAD_THRESHOLD = 3

# Executor types whose stdout streams parseable progress events. Kept here
# rather than imported from executor.py so this module has no dependency on the
# dispatch layer it measures.
_STREAMING_TYPES = frozenset({"claude", "claude-process", "codex"})

_EXPLORATION_ACTIVITIES = frozenset({"reading_file", "searching"})


def metering_supported_for(executor_type: str) -> bool:
    """Return whether *executor_type* streams events this meter can count."""
    return executor_type in _STREAMING_TYPES


def advisory_turn_threshold(step: str, cfg: dict | None = None) -> int | None:
    """Return the turn count at which *step* is worth reporting on.

    Projects tune this via ``turn_advisory.<step>`` in ``.lanegate.yml``; a
    value of ``0`` or below disables the advisory for that step.
    """
    configured = (cfg or {}).get("turn_advisory")
    if isinstance(configured, dict):
        value = configured.get(step)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value) if value > 0 else None
    return ADVISORY_TURN_THRESHOLDS.get(step)


@dataclass
class DispatchMeter:
    """Live turn/token/cost counters and turn-mix telemetry for one dispatch.

    Feed every raw stdout line to :meth:`observe`, together with the normalized
    event for that line when one was produced. Both inputs are needed and
    neither is sufficient: normalized events discard usage figures and collapse
    an assistant turn carrying a tool call into a tool event, while the raw line
    carries no activity classification.

    :meth:`observe` is total -- a malformed or unrecognized line is ignored
    rather than raising, because a parse error in telemetry must never be able
    to disturb a healthy agent run.
    """

    step: str = "implement"
    turns: int = 0
    tokens: int = 0
    last_turn_tokens: int = 0
    cost_usd: float = 0.0
    session_id: str | None = None
    activities: Counter = field(default_factory=Counter)
    files_read: Counter = field(default_factory=Counter)
    files_written: set = field(default_factory=set)
    _usage_seen: bool = field(default=False, repr=False)

    def observe(self, raw_line: str, event=None) -> None:
        """Fold one raw stdout line (and its normalized event) into the counters."""
        if event is not None:
            self._observe_event(event)
        if not raw_line or not raw_line.strip():
            return
        try:
            data = json.loads(raw_line.strip())
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return

        event_type = data.get("type")
        # Claude stream-json emits one "assistant" envelope per model turn,
        # whether it carries prose or a tool call. Codex exec --json emits one
        # terminal "turn.completed" event for the whole invocation, so count
        # its per-item completion boundaries instead.
        if event_type in ("assistant", "item.completed"):
            self.turns += 1

        # Capture the session id from the stream rather than from the final
        # result envelope: a dispatch killed by an existing wall-clock watchdog
        # never emits a final envelope, and without the id any continuation has
        # to start a cold conversation and re-explore the repo -- the single
        # most expensive thing that can happen after an interrupted run.
        if self.session_id is None:
            candidate = data.get("session_id")
            if isinstance(candidate, str) and candidate:
                self.session_id = candidate

        usage = data.get("usage")
        if not isinstance(usage, dict):
            message = data.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            self._add_usage(usage)

        cost = data.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            # Claude reports cost cumulatively, so take the max rather than
            # summing -- adding successive cumulative figures would multiply the
            # reported spend of a long run several times over.
            self.cost_usd = max(self.cost_usd, float(cost))

    def _observe_event(self, event) -> None:
        """Record the activity mix from a normalized :class:`ExecutorEvent`."""
        activity = getattr(event, "activity", None)
        if not isinstance(activity, str):
            return
        self.activities[activity] += 1
        path = getattr(event, "path", None)
        if not isinstance(path, str) or not path:
            return
        if activity == "reading_file":
            self.files_read[path] += 1
        elif activity == "writing_file":
            self.files_written.add(path)

    def _add_usage(self, usage: dict) -> None:
        """Accumulate a usage block, counting cache reads as real spend.

        Cache reads are billed and dominate a long run (43.9M of the 44M
        measured), so ignoring them would make the figure blind to exactly the
        runs worth finding.
        """
        total = 0
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total += int(value)
        if total > 0:
            self.tokens += total
            self.last_turn_tokens = total
            self._usage_seen = True

    @property
    def exploration_turns(self) -> int:
        """Turns spent reading or searching rather than changing anything."""
        return sum(self.activities[a] for a in _EXPLORATION_ACTIVITIES)

    @property
    def reread_count(self) -> int:
        """Reads of a file that had already been read at least once."""
        return sum(count - 1 for count in self.files_read.values() if count > 1)

    def diagnose(self, cfg: dict | None = None) -> dict[str, object] | None:
        """Attribute an expensive run to its likely cause, or ``None`` if normal.

        Returns ``{"verdict", "summary", "detail"}`` where ``verdict`` is one of
        ``"context-starved"`` (LaneGate under-supplied the prompt),
        ``"oversized-ticket"`` (the ticket holds several changes),
        ``"test-churn"`` (the change keeps failing its own checks), or
        ``"unattributed"`` (expensive, cause not evident from the turn mix).
        """
        threshold = advisory_turn_threshold(self.step, cfg)
        if threshold is None or self.turns <= threshold:
            return None

        classified = sum(self.activities.values())
        explore_share = (
            self.exploration_turns / classified if classified else 0.0
        )
        written = len(self.files_written)
        rereads = self.reread_count
        tests = self.activities.get("testing", 0)

        if explore_share >= _EXPLORATION_SHARE or rereads >= _REREAD_THRESHOLD:
            return {
                "verdict": "context-starved",
                "summary": (
                    "Most turns went to finding code, not changing it — the "
                    "prompt likely under-supplied context."
                ),
                "detail": (
                    f"{self.exploration_turns} of {classified} classified turns "
                    f"were reads/searches ({explore_share:.0%}), with {rereads} "
                    f"repeat read(s) of already-read files. Richer structural "
                    f"context for the touched files targets this "
                    f"directly; splitting the ticket would not."
                ),
            }
        if written >= _MULTI_CONCERN_WRITES:
            return {
                "verdict": "oversized-ticket",
                "summary": (
                    "Changes were spread across many files — this ticket "
                    "probably holds more than one concern."
                ),
                "detail": (
                    f"{written} distinct files were written across {self.turns} "
                    f"turns. Splitting into smaller tickets would cut cost and "
                    f"make review tractable."
                ),
            }
        if tests and tests >= max(3, self.turns // 5):
            return {
                "verdict": "test-churn",
                "summary": (
                    "A large share of turns were test runs — the change kept "
                    "failing its own checks."
                ),
                "detail": (
                    f"{tests} test invocations across {self.turns} turns, "
                    f"suggesting the acceptance criteria were unclear or the "
                    f"approach needed rework mid-flight."
                ),
            }
        return {
            "verdict": "unattributed",
            "summary": "Turn count was high but the cause is not evident from the turn mix.",
            "detail": (
                f"{self.turns} turns, {written} file(s) written, "
                f"{self.exploration_turns} exploration turn(s). Worth reading the "
                f"run log before assuming either cause."
            ),
        }

    def summary(self) -> dict[str, object]:
        """Return the counters for logging and analytics."""
        out: dict[str, object] = {
            "turns": self.turns,
            "tokens": self.tokens,
            "last_turn_tokens": self.last_turn_tokens,
            "cost_usd": round(self.cost_usd, 4),
            "files_written": len(self.files_written),
            "files_read": len(self.files_read),
            "exploration_turns": self.exploration_turns,
            "reread_count": self.reread_count,
        }
        if self.activities:
            out["activity_mix"] = dict(self.activities)
        return out

    def format_usage(self) -> str:
        """Return a compact one-line usage string for terminal and log output."""
        parts = [f"{self.turns} turns"]
        if self._usage_seen:
            parts.append(f"{self.tokens:,} tok")
        if self.cost_usd > 0:
            parts.append(f"${self.cost_usd:.2f}")
        return ", ".join(parts)
