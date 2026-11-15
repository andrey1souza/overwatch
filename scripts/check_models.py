#!/usr/bin/env python3
"""Run TLC on the configurations that CI can afford and check what each must say.

A configuration passes or it does not. A refinement check shows that Overwatch with TcrMode off
and the system model as it was before TCR describe the same behaviors. A probe asks TLC for a
trace of something that must be possible, or for none of something that must be impossible, so
a property of TCR cannot hold only because its scenario is unreachable.

Set TLA2TOOLS_JAR or pass --jar. IntegrationContract.cfg takes about ten minutes and a large
heap, and OverwatchTcrNotices.cfg explores 300 million states, so they run only with --full.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "spec"
PROBE_BOUNDS = "    Names = {\"a\"}\n    Timeout = 1\n    TcrMode = TRUE\n    SaveReverted = TRUE\n    StartQueued = FALSE\n"
TLC_HEAP = "-Xmx4g"
TLC_TIMEOUT = 3600


@dataclass(frozen=True)
class Run:
    name: str
    module: str
    config: str
    expected: str
    heavy: bool = False


def probe(name: str, edits: int, expected: str) -> Run:
    assert edits >= 2, f"a probe with {edits} workspace edits cannot reach a revert"
    assert expected in ("pass", "violation"), f"probe {name} expects {expected}, which TLC never says"
    config = f"SPECIFICATION Spec\nCONSTANTS\n{PROBE_BOUNDS}    MaxEdits = {edits}\nPROPERTIES\n    {name}\n"
    return Run(f"probe {name} with {edits} edits", "Probes", config, expected)


def checked(config: str, module: str, heavy: bool = False) -> Run:
    assert (SPEC / f"{config}.cfg").is_file(), f"spec/{config}.cfg does not exist"
    assert (SPEC / f"{module}.tla").is_file(), f"spec/{module}.tla does not exist"
    return Run(f"configuration {config}", module, (SPEC / f"{config}.cfg").read_text(), "pass", heavy)


def refinement(module: str) -> Run:
    assert (SPEC / "refinement" / f"{module}.cfg").is_file(), f"spec/refinement/{module}.cfg does not exist"
    assert (SPEC / "refinement" / f"{module}.tla").is_file(), f"spec/refinement/{module}.tla does not exist"
    return Run(f"refinement {module}", module, (SPEC / "refinement" / f"{module}.cfg").read_text(), "pass")


REACHABLE = ("NeverReverts", "NeverCheckpoints", "NeverCheckpointFails", "NeverHolds", "NeverObserves", "NeverPersonCommits",
             "NeverTellsRevert", "NeverTellsHold", "NeverPulls", "NeverHoldsATimeout", "NeverPullsWhileHeld", "NeverUnavailable", "NeverResumes", "NeverDirtyStart")
PROBE_NAMES = (*REACHABLE, "NoRevertOfUnseenWork")


def runs() -> list[Run]:
    """Every run, cheapest first, with what TLC must report."""
    found = [checked("Overwatch", "Overwatch"), checked("OverwatchTcr", "Overwatch"), checked("OverwatchTcrReverts", "Overwatch"), checked("OverwatchTcrQueues", "Overwatch"),
             checked("OverwatchTcrNotices", "Overwatch", heavy=True), checked("IntegrationLifecycle", "IntegrationLifecycle"),
             checked("IntegrationNotices", "IntegrationContract"), checked("IntegrationContract", "IntegrationContract", heavy=True),
             refinement("NewImplementsOld"), refinement("OldImplementsNew")]
    found += [probe(name, 2, "violation") for name in REACHABLE]
    found += [probe("NoRevertOfUnseenWork", 2, "pass"), probe("NoRevertOfUnseenWork", 3, "violation")]
    assert len({run.name for run in found}) == len(found), f"two of the {len(found)} runs share a name: {[run.name for run in found]}"
    assert all(run.expected in ("pass", "violation") for run in found), f"a run of {[run.name for run in found]} expects something other than pass or violation"
    return found


def verdict(output: str) -> str:
    """pass, violation, or error when TLC could not decide, as its output says."""
    assert output, f"TLC printed {len(output)} characters; it did not run"
    found = "pass" if "No error has been found" in output else (
        "violation" if re.search(r"Error: .*(violated|is not satisfied)|Temporal properties were violated", output) else "error")
    assert found in ("pass", "violation", "error"), f"verdict {found} is none of pass, violation, error"
    return found


def execute(run: Run, jar: Path, java: str) -> tuple[str, str]:
    """TLC's verdict on run and its state counts, computed in a copy of the modules so no state files reach the repository."""
    assert jar.is_file(), f"the TLA tools jar {jar} does not exist"
    assert run.config.strip(), f"run {run.name} has no configuration"
    with tempfile.TemporaryDirectory(prefix="overwatch-tlc-") as directory:
        work = Path(directory)
        for source in (*SPEC.glob("*.tla"), *(SPEC / "refinement").glob("*.tla"), *(SPEC / "probes").glob("*.tla")):
            shutil.copyfile(source, work / source.name)
        (work / "Run.cfg").write_text(run.config)
        result = subprocess.run([java, "-XX:+UseParallelGC", TLC_HEAP, "-cp", str(jar), "tlc2.TLC", "-config", "Run.cfg", "-workers", "auto",
                                 "-metadir", str(work / "states"), f"{run.module}.tla"], cwd=work, capture_output=True, text=True, timeout=TLC_TIMEOUT)
    output = result.stdout + result.stderr
    counts = re.findall(r"(\d+) states generated, (\d+) distinct", output)
    return verdict(output), (f"{counts[-1][0]} generated, {counts[-1][1]} distinct" if counts else "no state counts")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jar", type=Path, default=os.environ.get("TLA2TOOLS_JAR", ""))
    parser.add_argument("--java", default="java")
    parser.add_argument("--only", default="", help="run only the runs whose name contains this text")
    parser.add_argument("--full", action="store_true", help="include IntegrationContract.cfg")
    args = parser.parse_args()
    if not str(args.jar) or not args.jar.is_file():
        sys.exit(f"TLA tools jar missing: pass --jar or set TLA2TOOLS_JAR (got {args.jar!r})")
    if shutil.which(args.java) is None:
        sys.exit(f"Java not found: put {args.java!r} on PATH or pass --java with the path of a java executable")
    selected = [run for run in runs() if args.only in run.name and (args.full or not run.heavy)]
    assert selected, f"no run contains {args.only!r}; see runs() for the names"
    assert all(run.expected in ("pass", "violation") for run in selected), f"a selected run expects something TLC never says: {selected}"
    failures = []
    for run in selected:
        got, counts = execute(run, args.jar.resolve(), args.java)
        print(f"{'ok  ' if got == run.expected else 'FAIL'} {run.name}: expected {run.expected}, TLC said {got} ({counts})", flush=True)
        if got != run.expected:
            failures.append(run.name)
    if failures:
        sys.exit(f"{len(failures)} of {len(selected)} runs did not say what they must: {failures}")
    print(f"{len(selected)} TLC runs said what they must")


if __name__ == "__main__":
    main()
