"""Check how scripts/timing.py reads edit gaps from transcripts and how its simulation responds to rerun time."""
import json
import random
import unittest
from dataclasses import dataclass

from paths import ROOT
import timing


@dataclass(frozen=True)
class Shape:
    """What a transcript line holds besides its kind and time: the tools an assistant calls, whether a user entry is
    a tool's result, and whether a subagent wrote it."""
    tools: tuple[str, ...] = ()
    result: bool = False
    sidechain: bool = False



def entry(kind: str, second: int, shape: Shape) -> str:
    """One transcript line: an assistant calling tools, or a user entry that is a prompt or a tool's result."""
    tools, result, sidechain = shape.tools, shape.result, shape.sidechain
    stamp = f"2026-11-04T06:{second // 60:02d}:{second % 60:02d}Z"
    if kind == "assistant":
        content = [{"type": "tool_use", "name": name, "input": {}} for name in tools]
    else:
        content = [{"type": "tool_result", "content": "ok"}] if result else "please continue"
    line = json.dumps({"type": kind, "timestamp": stamp, "isSidechain": sidechain, "message": {"content": content}})
    assert json.loads(line)["type"] == kind, f"entry {line} lost its type {kind}"
    assert ROOT.is_dir(), f"{ROOT} is not the repository; tests/paths.py should find it"
    return line


TRANSCRIPT = (
    entry("user", 0, Shape()),
    entry("assistant", 10, Shape(tools=("Edit",))),
    entry("user", 11, Shape(result=True)),
    entry("assistant", 15, Shape(tools=("Bash",))),
    entry("assistant", 25, Shape(tools=("Read",))),
    entry("assistant", 40, Shape(tools=("Write",), sidechain=True)),
    entry("assistant", 45, Shape(tools=("Edit",))),
    entry("user", 50, Shape()),
    entry("assistant", 55, Shape(tools=("Edit",))),
    entry("assistant", 59 * 60, Shape(tools=("Edit",))),
)


class GapTests(unittest.TestCase):
    def test_gaps_lie_between_edits_of_one_turn(self) -> None:
        self.assertEqual(timing.session_gaps(TRANSCRIPT, timing.EDITING_TOOLS, 600), [5.0, 30.0])

    def test_files_only_leaves_out_bash(self) -> None:
        self.assertEqual(timing.session_gaps(TRANSCRIPT, timing.FILE_TOOLS, 600), [35.0])

    def test_a_long_pause_is_not_a_gap(self) -> None:
        self.assertEqual(timing.session_gaps(TRANSCRIPT, timing.FILE_TOOLS, 3600), [35.0, 59 * 60 - 55.0])

    def test_the_fit_median_is_the_geometric_mean(self) -> None:
        result = timing.fit([1.0, 100.0])
        self.assertAlmostEqual(result.median, 10.0)
        self.assertAlmostEqual(result.sigma, timing.math.log(10.0))

    def test_one_gap_cannot_be_fitted(self) -> None:
        with self.assertRaises(timing.TimingError):
            timing.fit([3.0])


class SimulationTests(unittest.TestCase):
    def setup(self, rerun: float, ahead: int) -> timing.Setup:
        """A turn of ten edits ten seconds apart, give or take, with reruns of the given median time."""
        assert isinstance(self, SimulationTests), f"setup was called on {type(self).__name__}; call it from a simulation test"
        assert rerun >= 0 and ahead > 0, f"rerun {rerun}s and ahead {ahead} must be a time of at least zero and a positive count"
        result = timing.Setup(10, timing.Fit(10.0, 0.5), rerun, 0.3, None, ahead)
        assert result.rerun == rerun and result.ahead == ahead, f"setup {result} lost rerun {rerun} or ahead {ahead}"
        return result

    def test_fast_reruns_barely_slow_the_agent(self) -> None:
        share, latency = timing.slowdown(self.setup(0.5, 1), 50, 1)
        self.assertLess(share, 0.05)
        self.assertLess(latency, 3.0)

    def test_slow_reruns_make_the_agent_wait(self) -> None:
        share, _ = timing.slowdown(self.setup(60.0, 1), 50, 1)
        self.assertGreater(share, 1.0)

    def test_running_further_ahead_allows_longer_reruns(self) -> None:
        close = timing.longest_rerun(self.setup(0.0, 1), 0.1, 50, 1)
        far = timing.longest_rerun(self.setup(0.0, 5), 0.1, 50, 1)
        self.assertLess(close, far)

    def test_every_edit_is_covered_by_a_later_result(self) -> None:
        result = timing.turn(timing.Setup(10, timing.Fit(10.0, 0.5), 5.0, 0.3, 30.0, 3), random.Random(2))
        self.assertGreaterEqual(result.latency, 0)
        self.assertGreaterEqual(result.tail, timing.QUIET)


if __name__ == "__main__":
    unittest.main()
