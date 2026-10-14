"""Local run store: `.agentpatch/runs/<run_id>/{run.json, results.json, traces/*.json}`."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schema import Trace, read_json, write_json

DEFAULT_HOME = ".agentpatch"


def home(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("AGENTPATCH_HOME") or DEFAULT_HOME)


@dataclass
class Run:
    dir: Path
    info: dict

    @property
    def id(self) -> str:
        return self.info["run_id"]

    @property
    def label(self) -> str:
        return self.info.get("label", "")

    def traces(self) -> list[Trace]:
        files = sorted((self.dir / "traces").glob("*.json"))
        traces = [Trace.from_dict(read_json(f)) for f in files]
        return sorted(traces, key=lambda t: (t.case_id, t.trial))

    def trace(self, case_id: str, trial: int) -> Trace | None:
        f = self.dir / "traces" / trace_filename(case_id, trial)
        return Trace.from_dict(read_json(f)) if f.is_file() else None

    def results(self) -> list[dict]:
        f = self.dir / "results.json"
        return read_json(f) if f.is_file() else []


def trace_filename(case_id: str, trial: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id)[:80]
    return f"{safe}__{trial}.json"


def new_run(label: str, info: dict, root: str | Path | None = None) -> Run:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", label):
        raise ValueError("run label must be 1-40 characters of letters, digits, '.', '_' or '-'")
    now = datetime.now(timezone.utc)
    base = f"{now:%Y%m%d-%H%M%S}-{label}"
    runs = home(root) / "runs"
    run_id, n = base, 1
    while (runs / run_id).exists():
        n += 1
        run_id = f"{base}-{n}"
    d = runs / run_id
    (d / "traces").mkdir(parents=True)
    full = {"run_id": run_id, "label": label, "created_at": now.isoformat(), "status": "running", **info}
    write_json(d / "run.json", full)
    return Run(d, full)


def save_trace(run: Run, trace: Trace) -> Path:
    return write_json(run.dir / "traces" / trace_filename(trace.case_id, trace.trial), trace.to_dict())


def finish_run(run: Run, results: list[dict], **extra) -> Run:
    write_json(run.dir / "results.json", results)
    run.info.update(status="complete", **extra)
    write_json(run.dir / "run.json", run.info)
    return run


def list_runs(root: str | Path | None = None) -> list[Run]:
    runs_dir = home(root) / "runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for d in runs_dir.iterdir():
        f = d / "run.json"
        if f.is_file():
            try:
                out.append(Run(d, read_json(f)))
            except ValueError:
                continue
    return sorted(out, key=lambda r: r.info.get("created_at", ""), reverse=True)


def resolve(ref: str, root: str | Path | None = None) -> Run:
    """A run id, an unambiguous id prefix, or a label (meaning the latest run with that label)."""
    runs = list_runs(root)
    for r in runs:
        if r.id == ref:
            return r
    labelled = [r for r in runs if r.label == ref]
    if labelled:
        return labelled[0]
    pref = [r for r in runs if r.id.startswith(ref)]
    if len(pref) == 1:
        return pref[0]
    known = ", ".join(sorted({r.label for r in runs})) or "none yet"
    raise LookupError(f"no run matches {ref!r} (labels: {known}; see `agentpatch runs`)")


def default_pair(runs: list[Run]) -> tuple[Run, Run]:
    """Latest `baseline` vs latest `candidate`; otherwise the newest run vs the newest run with another label."""
    if len(runs) < 2:
        raise LookupError("need two runs to compare; create them with `agentpatch run --label baseline` and "
                          "`agentpatch run --label candidate` (or try `agentpatch demo`)")
    by_label: dict[str, Run] = {}
    for r in runs:
        by_label.setdefault(r.label, r)
    if "baseline" in by_label and "candidate" in by_label:
        return by_label["baseline"], by_label["candidate"]
    newest = runs[0]
    older = next((r for r in runs[1:] if r.label != newest.label), runs[1])
    return older, newest


def delete_run(run: Run) -> None:
    if run.dir.parent.name == "runs" and (run.dir / "run.json").is_file():
        shutil.rmtree(run.dir)
