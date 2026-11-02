"""Measure retained RSS after collection and execution in fresh processes.

Install pytest and psutil into the interpreter running this script. Compare two
source trees (with generated _version.py files) using, for example::

    python bench/report_memory.py run --sources ../baseline/src src --output results

Each combination runs three times, in a shuffled order. Plugin autoloading is
disabled. Terminal output goes to the null device, not a retained string buffer.
GC runs at each phase boundary. Counts are taken only after the memory samples.
The JSON records RSS with tracing both off and on, traced Python memory, and
tracemalloc's own metadata separately. These are phase-end values, not peak RSS.
"""

from __future__ import annotations

import argparse
import gc
import itertools
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import sys
import tempfile
import time


def worker(args):
    sys.path.insert(0, str(Path(args.source).resolve()))
    import tracemalloc

    import psutil

    from _pytest.reports import TestReport
    import pytest

    process = psutil.Process()
    process.memory_info()
    points = {}
    if args.trace:
        tracemalloc.start(1)

    def sample(label):
        gc.collect()
        point = {"rss": process.memory_info().rss}
        if args.trace:
            point.update(
                zip(("traced", "peak"), tracemalloc.get_traced_memory(), strict=True)
            )
            point["tracer"] = tracemalloc.get_tracemalloc_memory()
        points[label] = point

    class Probe:
        def pytest_sessionstart(self, session):
            sample("start")

        def pytest_collection_finish(self, session):
            sample("collected")

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtestloop(self, session):
            result = yield
            sample("ran")
            # Enumerate after measuring, without retaining reports in the probe.
            tr = session.config.pluginmanager.getplugin("terminalreporter")
            self.stats = {} if tr is None else {k: len(v) for k, v in tr.stats.items()}
            self.reports = sum(isinstance(obj, TestReport) for obj in gc.get_objects())
            return result

    with tempfile.TemporaryDirectory(prefix="pytest-memory-") as directory:
        work = Path(directory)
        (work / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (work / "test_memory.py").write_text(
            "import pytest\n"
            f'@pytest.mark.parametrize("value", range({args.count}))\n'
            "def test_empty(value):\n    pass\n",
            encoding="utf-8",
        )
        options = ["-c", str(work / "pytest.ini"), "--rootdir", str(work), str(work)]
        if args.mode == "no-terminal":
            options += ["-p", "no:terminal"]
        elif args.mode == "collect":
            options += ["--collect-only"]
        probe = Probe()
        start = time.perf_counter()
        code = pytest.main(options, plugins=[probe])
        elapsed = time.perf_counter() - start
        assert code == 0, code
    Path(args.output).write_text(
        json.dumps(
            dict(
                count=args.count,
                mode=args.mode,
                trace=args.trace,
                source=args.source,
                python=sys.version,
                platform=platform.platform(),
                pytest=pytest.__version__,
                pytest_source=str(Path(pytest.__file__).resolve()),
                psutil=psutil.__version__,
                points=points,
                stats=probe.stats,
                reports=probe.reports,
                elapsed=elapsed,
            )
        ),
        encoding="utf-8",
    )


def parent(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    jobs = list(
        itertools.product(
            args.sources, args.counts, args.modes, args.tracing, range(args.repeats)
        )
    )
    random.Random(10605).shuffle(jobs)
    results = []
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONHASHSEED="0")
    env.pop("PYTEST_ADDOPTS", None)
    for i, (source, count, mode, trace, repeat) in enumerate(jobs):
        destination = output / f"{i:03}.json"
        command = [
            sys.executable,
            __file__,
            "worker",
            "--source",
            source,
            "--count",
            str(count),
            "--mode",
            mode,
            "--output",
            str(destination),
        ]
        if trace:
            command.append("--trace")
        with open(os.devnull, "w") as sink:
            subprocess.run(command, env=env, stdout=sink, check=True)
        data = json.loads(destination.read_text(encoding="utf-8"))
        data["repeat"] = repeat
        results.append(data)
        print(
            f"{i + 1}/{len(jobs)} {Path(source).parent.name} {count} {mode} trace={trace}",
            flush=True,
        )
        (output / "all.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    child = sub.add_parser("worker")
    child.add_argument("--source", required=True)
    child.add_argument("--count", type=int, required=True)
    child.add_argument(
        "--mode", choices=["normal", "collect", "no-terminal"], required=True
    )
    child.add_argument("--trace", action="store_true")
    child.add_argument("--output", required=True)
    run = sub.add_parser("run")
    run.add_argument("--sources", nargs="+", required=True)
    run.add_argument("--counts", type=int, nargs="+", default=[1000, 5000, 10000])
    run.add_argument("--modes", nargs="+", default=["normal", "collect", "no-terminal"])
    run.add_argument("--tracing", type=int, choices=[0, 1], nargs="+", default=[0, 1])
    run.add_argument("--repeats", type=int, default=3)
    run.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "worker":
        worker(args)
    else:
        parent(args)
