"""Local dashboard: a small JSON API over the run store plus static files from `web/`.

Binds to 127.0.0.1 only and never executes anything from trace content; the browser renders
trace data as text. Route handlers are plain functions so they can be tested without sockets.
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import store
from .compare import compare
from .coverage import run_coverage, trace_gaps
from .diagnose import align, diagnose

WEB = Path(__file__).parent / "web"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _run_summary(r: store.Run) -> dict:
    res = r.results()
    info = r.info
    return {"run_id": r.id, "label": r.label, "created_at": info.get("created_at"), "status": info.get("status"),
            "suite": info.get("suite"), "target": info.get("target"), "source": info.get("source"),
            "git": info.get("git"), "repeats": info.get("repeats"), "cases": info.get("cases") or [],
            "trials": len(res), "passed": sum(x["outcome"] == "pass" for x in res)}


class Api:
    def __init__(self, home: str | Path | None = None):
        self.home = home
        self._cache: dict[tuple, dict] = {}
        self._lock = threading.Lock()

    def _resolve(self, ref: str | None, what: str) -> store.Run:
        if not ref:
            raise ApiError(400, f"missing `{what}` parameter")
        try:
            return store.resolve(ref, self.home)
        except LookupError as exc:
            raise ApiError(404, str(exc)) from None

    @staticmethod
    def _stamp(run: store.Run) -> tuple:
        f = run.dir / "results.json"
        return (run.id, f.stat().st_mtime_ns if f.is_file() else 0)

    def runs(self, q: dict) -> dict:
        runs = store.list_runs(self.home)
        try:
            b, c = store.default_pair(runs)
            default = {"baseline": b.id, "candidate": c.id}
        except LookupError:
            default = None
        return {"home": str(store.home(self.home).resolve()), "runs": [_run_summary(r) for r in runs],
                "default": default}

    def compare(self, q: dict) -> dict:
        runs = store.list_runs(self.home)
        if q.get("baseline") and q.get("candidate"):
            b, c = self._resolve(q["baseline"], "baseline"), self._resolve(q["candidate"], "candidate")
        else:
            try:
                b, c = store.default_pair(runs)
            except LookupError as exc:
                raise ApiError(404, str(exc)) from None
        if b.id == c.id:
            raise ApiError(400, "pick two different runs")
        key = self._stamp(b) + self._stamp(c)
        with self._lock:
            if key not in self._cache:
                self._cache[key] = compare(b, c)
            return self._cache[key]

    def run(self, q: dict) -> dict:
        r = self._resolve(q.get("run"), "run")
        by_case: dict[str, dict] = {}
        for x in r.results():
            e = by_case.setdefault(x["case_id"], {"case_id": x["case_id"], "trials": []})
            e["trials"].append({"trial": x["trial"], "outcome": x["outcome"], "duration_ms": x.get("duration_ms"),
                                "failed": [c["id"] for c in x["checks"] if c["passed"] is False],
                                "unknown": [c["id"] for c in x["checks"] if c["passed"] is None]})
        return {"run": _run_summary(r), "cases": list(by_case.values()), "coverage": self.coverage(q)}

    def trace(self, q: dict) -> dict:
        r = self._resolve(q.get("run"), "run")
        case = q.get("case")
        if not case:
            raise ApiError(400, "missing `case` parameter")
        try:
            trial = int(q.get("trial", "0"))
        except ValueError:
            raise ApiError(400, "`trial` must be an integer") from None
        t = r.trace(case, trial)
        if t is None:
            raise ApiError(404, f"no trace for case {case!r} trial {trial} in run {r.id}")
        result = next((x for x in r.results() if x["case_id"] == case and x["trial"] == trial), None)
        trials = sorted({x["trial"]: x["outcome"] for x in r.results() if x["case_id"] == case}.items())
        return {"run_id": r.id, "label": r.label, "trace": t.to_dict(), "result": result,
                "trials": [{"trial": k, "outcome": v} for k, v in trials],
                "gaps": trace_gaps(t, r.info.get("thresholds"))}

    def case(self, q: dict) -> dict:
        """One case, baseline trial vs candidate trial: both traces, results, aligned steps and a diagnosis."""
        cmp = self.compare(q)
        b = self._resolve(cmp["baseline"]["run_id"], "baseline")
        c = self._resolve(cmp["candidate"]["run_id"], "candidate")
        cid = q.get("case")
        row = next((r for r in cmp["cases"] if r["case_id"] == cid), None)
        if row is None:
            raise ApiError(404, f"case {cid!r} isn't in either run")
        d = row.get("diagnosis") or {}

        def pick(run, key, default):
            try:
                trial = int(q[key]) if q.get(key) not in (None, "") else default
            except ValueError:
                raise ApiError(400, f"`{key}` must be an integer") from None
            results = [x for x in run.results() if x["case_id"] == cid]
            if not results:
                return None
            if trial not in {x["trial"] for x in results}:
                trial = results[0]["trial"]
            t = run.trace(cid, trial)
            result = next(x for x in results if x["trial"] == trial)
            return {"run_id": run.id, "label": run.label, "trial": trial, "trace": t, "result": result,
                    "trials": [{"trial": x["trial"], "outcome": x["outcome"]} for x in results]}

        bs, cs = pick(b, "btrial", d.get("baseline_trial") or 0), pick(c, "ctrial", d.get("trial") or 0)
        diagnosis, alignment = None, None
        if bs and cs and bs["trace"] and cs["trace"]:
            alignment = align(bs["trace"], cs["trace"])
            alignment.pop("mapping")
            if cs["result"]["outcome"] != "pass":
                diagnosis = diagnose(cs["trace"], cs["result"], bs["trace"], bs["result"])
        elif cs and cs["trace"] and cs["result"]["outcome"] != "pass":
            diagnosis = diagnose(cs["trace"], cs["result"])
        for side in (bs, cs):
            if side:
                side["gaps"] = trace_gaps(side["trace"]) if side["trace"] else []
                side["trace"] = side["trace"].to_dict() if side["trace"] else None
        summary = {k: row[k] for k in ("case_id", "change", "baseline", "candidate", "checks", "latency")}
        return {"case": summary, "baseline": bs, "candidate": cs, "alignment": alignment, "diagnosis": diagnosis,
                "pair": {"baseline": b.id, "candidate": c.id}}

    def coverage(self, q: dict) -> dict:
        r = self._resolve(q.get("run"), "run")
        return run_coverage(r.traces(), r.info.get("graph"), set(r.info.get("checked_tools") or []),
                            r.info.get("coverage_expectations"), r.info.get("thresholds"))

    ROUTES = {"/api/runs": "runs", "/api/compare": "compare", "/api/run": "run", "/api/trace": "trace",
              "/api/case": "case", "/api/coverage": "coverage"}

    def handle(self, raw_path: str) -> tuple[int, str, bytes]:
        """(status, content type, body) for a GET request path."""
        url = urlparse(raw_path)
        if url.path in self.ROUTES:
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                body, status = getattr(self, self.ROUTES[url.path])(q), 200
            except ApiError as exc:
                body, status = {"error": str(exc)}, exc.status
            return status, "application/json", json.dumps(body, default=str).encode()
        if url.path.startswith("/api/"):
            return 404, "application/json", json.dumps({"error": f"unknown endpoint {url.path}"}).encode()
        return self.static(url.path)

    @staticmethod
    def static(path: str) -> tuple[int, str, bytes]:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return 404, "text/plain", b"not found"
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        return 200, ctype, f.read_bytes()


def make_server(home: str | Path | None = None, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    api = Api(home)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                status, ctype, body = api.handle(self.path)
            except Exception as exc:  # keep the server up; report the failure to the page
                status, ctype = 500, "application/json"
                body = json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(home: str | Path | None = None, port: int = 8765, open_browser: bool = True) -> None:
    try:
        httpd = make_server(home, port)
    except OSError as exc:
        raise LookupError(f"can't listen on 127.0.0.1:{port} ({exc.strerror}); try --port {port + 1}") from None
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    n = len(store.list_runs(home))
    print(f"AgentPatch dashboard: {url}  ({n} run(s) in {store.home(home).resolve()}; Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
