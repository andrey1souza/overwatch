#!/usr/bin/env python3
"""Fit the gaps between a coding agent's edits from Claude Code transcripts and find how long a test rerun may take
before limiting how far the agent runs ahead of its checks slows it down too much."""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

QUIET = 0.4
EDITING_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})
FILE_TOOLS = EDITING_TOOLS - {"Bash"}
TRANSCRIPTS = Path.home() / ".claude" / "projects"
LONGEST_RERUN = 3600.0
SHORTEST_RERUN = 0.1
SHORTEST_GAP = 0.1


class TimingError(ValueError):
    """The transcripts or options cannot give a timing estimate; the message says what to pass instead."""


@dataclass(frozen=True)
class Gaps:
    """Seconds between consecutive edits within one turn of one session, and how many sessions they came from."""
    seconds: tuple[float, ...]
    sessions: int


@dataclass(frozen=True)
class Fit:
    """A lognormal fit of edit gaps: its median and the standard deviation of their logarithms."""
    median: float
    sigma: float


@dataclass(frozen=True)
class Turn:
    """One simulated turn: time spent working, time spent waiting for a result, the mean time from each edit to a
    result that covers it, and the time from the last edit to a result that covers it."""
    working: float
    blocked: float
    latency: float
    tail: float


@dataclass(frozen=True)
class Setup:
    """What a simulation varies: edits per turn, the gap fit, the median rerun time and its spread, the first full
    run's median time or None when the turn starts with a result, and how many edits the agent may run ahead."""
    edits: int
    gaps: Fit
    rerun: float
    rerun_sigma: float
    first: float | None
    ahead: int


def timestamp(text: str) -> float:
    """Seconds since the epoch of a transcript timestamp such as 2026-11-04T06:18:00.123Z."""
    moment = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    assert moment > 0, f"timestamp {text} is before 1970; transcripts record when each step ran"
    assert math.isfinite(moment), f"timestamp {text} is not a finite time"
    return moment


def is_prompt(entry: dict) -> bool:
    """Whether a transcript entry is a person's prompt, which starts a new turn, rather than a tool's result."""
    message = entry.get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    blocks = content if isinstance(content, list) else []
    prompt = entry.get("type") == "user" and not any(isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks)
    assert entry.get("type") == "user" or not prompt, f"a {entry.get('type')} entry counted as a prompt; only user entries start turns"
    assert isinstance(prompt, bool), f"is_prompt gave {prompt!r} for {entry.get('type')}; return a bool"
    return prompt


def edit_tools(entry: dict, tools: frozenset[str]) -> int:
    """How many calls of tools an assistant entry makes, leaving out subagents' entries, which edit on their own."""
    if entry.get("type") != "assistant" or entry.get("isSidechain"):
        return 0
    message = entry.get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    calls = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"] if isinstance(content, list) else []
    count = sum(1 for call in calls if call.get("name") in tools)
    assert 0 <= count <= len(calls), f"counted {count} edits among {len(calls)} tool calls; count only tool_use blocks"
    assert count == 0 or entry.get("type") == "assistant", f"counted edits in a {entry.get('type')} entry; only assistants call tools"
    return count


def session_gaps(lines: Iterable[str], tools: frozenset[str], idle: float) -> list[float]:
    """The gaps between edits in one session's transcript lines, within turns and at most idle seconds long."""
    gaps: list[float] = []
    last: float | None = None
    edits = 0
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or "timestamp" not in entry:
            continue
        if is_prompt(entry):
            last = None
            continue
        for _ in range(edit_tools(entry, tools)):
            edits += 1
            now = timestamp(entry["timestamp"])
            if last is not None and 0 <= now - last <= idle:
                gaps.append(max(now - last, SHORTEST_GAP))
            last = now
    assert all(SHORTEST_GAP <= gap <= max(idle, SHORTEST_GAP) for gap in gaps), f"gaps {gaps} fall outside {SHORTEST_GAP}s to {idle}s"
    assert len(gaps) <= max(edits - 1, 0), f"{len(gaps)} gaps from {edits} edits; a gap lies between two edits"
    return gaps


def transcript_paths(root: Path, project: str) -> Iterator[Path]:
    """The session transcripts under root whose project folder contains project."""
    if not root.is_dir():
        raise TimingError(f"{root} holds no transcripts; pass --transcripts with Claude Code's projects folder")
    for path in sorted(root.glob("*/*.jsonl")):
        if project in path.parent.name:
            assert path.suffix == ".jsonl", f"{path} is not a transcript; glob only .jsonl files"
            assert path.is_relative_to(root), f"{path} is outside {root}; glob within it"
            yield path


def read_gaps(root: Path, project: str, tools: frozenset[str], idle: float) -> Gaps:
    """The edit gaps of every matching session under root."""
    seconds: list[float] = []
    sessions = 0
    for path in transcript_paths(root, project):
        with path.open(encoding="utf-8", errors="replace") as lines:
            found = session_gaps(lines, tools, idle)
        sessions += bool(found)
        seconds.extend(found)
    gaps = Gaps(tuple(seconds), sessions)
    assert len(gaps.seconds) >= gaps.sessions, f"{gaps.sessions} sessions gave only {len(gaps.seconds)} gaps; count a session only when it has some"
    assert all(gap > 0 for gap in gaps.seconds), "a gap is not positive; session_gaps keeps gaps of at least SHORTEST_GAP"
    return gaps


def fit(seconds: Iterable[float]) -> Fit:
    """The lognormal fit of gaps: the exponential of their logarithms' mean, and those logarithms' spread."""
    logs = [math.log(gap) for gap in seconds]
    if len(logs) < 2:
        raise TimingError(f"{len(logs)} edit gaps are too few to fit; pass --gap-median and --gap-sigma, or more transcripts")
    result = Fit(math.exp(statistics.fmean(logs)), statistics.pstdev(logs))
    assert result.median > 0, f"fitted median {result.median}s is not positive"
    assert result.sigma >= 0, f"fitted sigma {result.sigma} is negative"
    return result


@dataclass
class World:
    """The watcher as spec/Overwatch.tla models it: a run starts once edits go quiet, a newer edit replaces it, and
    edits made during the first full run are rechecked when it ends."""
    rng: random.Random
    setup: Setup
    head: int = 0
    report_at: int | None = None
    running: tuple[float, int, bool] | None = None
    quiet_at: float | None = None
    installed: bool = False
    edited_during_first: bool = False
    covered: dict[int, float] = field(default_factory=dict)

    def start(self, now: float, first: bool) -> None:
        """Starts a run of the code as it is now, the first full run when first."""
        median = self.setup.first if first and self.setup.first is not None else self.setup.rerun
        length = median * math.exp(self.rng.gauss(0, self.setup.rerun_sigma))
        self.running = (now + length, self.head, first)
        assert length > 0, f"a run of median {median}s drew a length of {length}s; draw a positive one"
        assert self.running[0] >= now, f"a run started at {now}s ends earlier, at {self.running[0]}s"

    def ahead(self) -> int:
        """How many edits the agent has made since the code the latest result checked."""
        behind = self.head - (self.report_at if self.report_at is not None else 0)
        assert behind >= 0, f"the agent is {behind} edits ahead; a result never checks code newer than the head"
        assert behind <= self.head, f"{behind} edits ahead of {self.head} made; count from the latest result"
        return behind

    def next_event(self) -> float | None:
        """When the next run starts or ends, or None when nothing is pending."""
        times = [time for time in (self.quiet_at, self.running[0] if self.running else None) if time is not None]
        assert len(times) <= 2, f"{len(times)} pending events; there is at most a quiet period and a run"
        assert all(time >= 0 for time in times), f"pending events {times} are before the turn started"
        return min(times) if times else None

    def step(self) -> float:
        """Handles the next event, a quiet period elapsing or a run ending, and returns when it happened."""
        now = self.next_event()
        assert now is not None, "step called with nothing pending; call it only when next_event gives a time"
        if self.quiet_at is not None and now == self.quiet_at:
            self.quiet_at = None
            self.start(now, False)
            return now
        assert self.running is not None, f"the event at {now}s is neither a quiet period nor a run"
        _, version, first = self.running
        self.running = None
        self.report_at = version
        self.installed = True
        for edit in range(version + 1):
            self.covered.setdefault(edit, now)
        if first and self.edited_during_first:
            self.quiet_at = now + QUIET
        return now

    def advance(self, until: float) -> None:
        """Handles every event up to until."""
        while (now := self.next_event()) is not None and now <= until:
            self.step()
        assert self.next_event() is None or self.next_event() > until, f"an event before {until}s is still pending"
        assert self.report_at is None or self.report_at <= self.head, f"a result checked edit {self.report_at} of {self.head}"

    def edit(self, now: float) -> None:
        """The agent edits at now: the quiet period restarts, or during the first run the edit waits for it to end."""
        self.head += 1
        if self.installed:
            self.quiet_at = now + QUIET
        else:
            self.edited_during_first = True
        assert self.head >= 1, f"head is {self.head} after an edit"
        assert self.installed or self.edited_during_first, "an edit during the first run was not remembered"


def turn(setup: Setup, rng: random.Random) -> Turn:
    """Simulates one turn of setup.edits edits, waiting before each edit while the agent is setup.ahead edits ahead."""
    world = World(rng, setup)
    if setup.first is None:
        world.installed = True
        world.report_at = 0
    else:
        world.start(0.0, True)
    edited: dict[int, float] = {}
    now = working = blocked = 0.0
    for _ in range(setup.edits):
        gap = setup.gaps.median * math.exp(rng.gauss(0, setup.gaps.sigma))
        working += gap
        now += gap
        world.advance(now)
        while world.ahead() >= setup.ahead:
            event = world.step()
            blocked += event - now
            now = event
        world.edit(now)
        edited[world.head] = now
    while world.report_at != world.head:
        world.step()
    latency = statistics.fmean(world.covered[version] - at for version, at in edited.items())
    result = Turn(working, blocked, latency, world.covered[world.head] - now)
    assert result.blocked >= 0 and result.working > 0, f"turn {result} has negative waiting or no work"
    assert result.tail >= 0, f"the last edit was covered {result.tail}s before it was made"
    return result


def slowdown(setup: Setup, samples: int, seed: int) -> tuple[float, float]:
    """The time spent waiting as a share of time spent working, and the median feedback latency, over samples turns."""
    rng = random.Random(seed)
    turns = [turn(setup, rng) for _ in range(samples)]
    share = sum(each.blocked for each in turns) / sum(each.working for each in turns)
    latency = statistics.median(each.latency for each in turns)
    assert share >= 0, f"slowdown {share} is negative"
    assert latency >= 0, f"feedback latency {latency}s is negative"
    return share, latency


def longest_rerun(setup: Setup, target: float, samples: int, seed: int) -> float:
    """The longest median rerun time whose slowdown stays within target, by bisection on its logarithm."""
    if slowdown(Setup(**{**setup.__dict__, "rerun": LONGEST_RERUN}), samples, seed)[0] <= target:
        return LONGEST_RERUN
    low, high = math.log(SHORTEST_RERUN), math.log(LONGEST_RERUN)
    for _ in range(20):
        middle = (low + high) / 2
        within = slowdown(Setup(**{**setup.__dict__, "rerun": math.exp(middle)}), samples, seed)[0] <= target
        low, high = (middle, high) if within else (low, middle)
    longest = math.exp(low)
    assert SHORTEST_RERUN <= longest <= LONGEST_RERUN, f"bisection left {longest}s outside its range"
    assert high - low < 1e-3, f"bisection stopped {high - low} apart; run it longer"
    return longest


def budget_text(gaps: Fit, source: str, args: argparse.Namespace) -> str:
    """The longest rerun for each max-ahead within the target slowdown, and the first turn's slowdown when asked."""
    lines = [f"Edit gaps: median {gaps.median:.1f}s, sigma {gaps.sigma:.2f} ({source})",
             f"Turns of {args.edits} edits; rerun times vary by sigma {args.run_sigma}; target slowdown {args.slowdown:.0%}",
             "",
             f"{'max ahead':>9}  {'longest rerun':>13}  {'feedback':>9}  {'edits meanwhile':>15}" + ("  first turn slowdown" if args.first else "")]
    mean_gap = gaps.median * math.exp(gaps.sigma ** 2 / 2)
    for ahead in args.ahead:
        setup = Setup(args.edits, gaps, 0.0, args.run_sigma, None, ahead)
        rerun = longest_rerun(setup, args.slowdown, args.samples, args.seed)
        _, latency = slowdown(Setup(**{**setup.__dict__, "rerun": rerun}), args.samples, args.seed)
        shown = f">{LONGEST_RERUN:.0f}s" if rerun >= LONGEST_RERUN else f"{rerun:.1f}s"
        line = f"{ahead:>9}  {shown:>13}  {latency:>8.1f}s  {latency / mean_gap:>15.1f}"
        if args.first:
            first, _ = slowdown(Setup(**{**setup.__dict__, "rerun": rerun, "first": args.first}), args.samples, args.seed)
            line += f"  {first:>20.0%}"
        lines.append(line)
    text = "\n".join(lines)
    assert text.count("\n") == 3 + len(args.ahead), f"budget text has {text.count(chr(10)) + 1} lines for {len(args.ahead)} rows"
    assert text.startswith("Edit gaps: "), f"budget text opens with {text[:20]!r}; state the fit first"
    return text


def gaps_text(gaps: Gaps, result: Fit) -> str:
    """The gaps' count, sessions, percentiles and lognormal fit."""
    deciles = statistics.quantiles(gaps.seconds, n=10)
    text = (f"{len(gaps.seconds)} edit gaps from {gaps.sessions} sessions: median {statistics.median(gaps.seconds):.1f}s, "
            f"p10 {deciles[0]:.1f}s, p90 {deciles[-1]:.1f}s\n"
            f"lognormal fit: --gap-median {result.median:.1f} --gap-sigma {result.sigma:.2f}")
    assert str(len(gaps.seconds)) in text, f"gaps text {text!r} omits how many gaps there are"
    assert "--gap-median" in text, "gaps text omits the options that reuse its fit"
    return text


def main() -> None:
    """Parses the arguments, reads and fits the transcripts' edit gaps unless given, and prints gaps or budget."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("gaps", "Fit the gaps between edits in your transcripts"),
                       ("budget", "Find the longest rerun time within a target slowdown for each max-ahead")):
        command = commands.add_parser(name, help=text, description=text)
        command.add_argument("--transcripts", type=Path, default=TRANSCRIPTS, help="Claude Code's projects folder")
        command.add_argument("--project", default="", help="Only transcripts whose project folder contains this")
        command.add_argument("--files-only", action="store_true", help="Count file edits only, not Bash, which overwatch also counts")
        command.add_argument("--idle", type=float, default=600.0, help="Leave out gaps longer than this many seconds")
    budget = commands.choices["budget"]
    budget.add_argument("--gap-median", type=float, help="Skip the transcripts and use this median gap in seconds")
    budget.add_argument("--gap-sigma", type=float, help="With --gap-median, the spread of the gaps' logarithms")
    budget.add_argument("--slowdown", type=float, default=0.1, help="Waiting allowed as a share of working time")
    budget.add_argument("--ahead", type=int, nargs="+", default=[1, 3, 5], help="Max-ahead values to try")
    budget.add_argument("--edits", type=int, default=20, help="Edits per turn")
    budget.add_argument("--first", type=float, help="Also show the first turn's slowdown with a first full run of this many seconds")
    budget.add_argument("--run-sigma", type=float, default=0.3, help="Spread of the logarithms of rerun times")
    budget.add_argument("--samples", type=int, default=300, help="Turns simulated per estimate")
    budget.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    tools = FILE_TOOLS if args.files_only else EDITING_TOOLS
    try:
        if args.command == "budget" and args.gap_median is not None:
            if args.gap_sigma is None:
                raise TimingError("--gap-median needs --gap-sigma; pass both, or neither to read your transcripts")
            output = budget_text(Fit(args.gap_median, args.gap_sigma), "given", args)
        else:
            gaps = read_gaps(args.transcripts, args.project, tools, args.idle)
            result = fit(gaps.seconds)
            source = f"{len(gaps.seconds)} gaps from {gaps.sessions} sessions"
            output = gaps_text(gaps, result) if args.command == "gaps" else budget_text(result, source, args)
    except TimingError as error:
        parser.error(str(error))
    assert output, f"{args.command} printed nothing; gaps_text and budget_text always produce text"
    assert args.command in ("gaps", "budget"), f"unknown command {args.command}; argparse allows only gaps and budget"
    print(output)


if __name__ == "__main__":
    main()
