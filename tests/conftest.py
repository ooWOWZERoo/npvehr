"""End-to-end test fixtures: launch the real app (uvicorn) against a fresh,
seeded SQLite database for the whole test session, and drive it with a real
browser via Playwright. These are true end-to-end tests against the running
app over HTTP -- not unit tests against internal functions -- so they catch
what those can't: template rendering, routing, auth redirects, form posts.

Artifact cleanup: the test database (and its -wal/-journal files, if any)
lives in a tempfile.TemporaryDirectory that is deleted unconditionally when
the session ends, whether tests passed, failed, or errored during setup --
nothing is left behind in the repo or the OS temp dir. The uvicorn subprocess
is terminated (falling back to kill() if it doesn't exit promptly) in a
finally block for the same reason. Playwright's own browser/context/page
fixtures (session/test scoped respectively) are torn down by
pytest-playwright itself; no extra handling is needed for those here.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DEMO_EMAIL = "admin@newpathvision.example"
DEMO_PASSWORD = "ChangeMe123!"


_LIVE_DB: dict = {}   # path of the live server's throwaway SQLite file, for fixtures that seed reference data


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(proc: subprocess.Popen, port: int, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server process exited early with code {proc.returncode} "
                                f"before it started listening on port {port}")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"Server on 127.0.0.1:{port} did not start within {timeout}s")


RXNAV_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rxnav_captured.json"
RXNAV_OUTAGE_NAME = "Zzoutage Test"      # the stand-in always answers 503 for this name (and for everything while /__fail?on=1)


@pytest.fixture(scope="session")
def rxnav_stub():
    """A local stand-in for NLM RxNav so the suite never touches the network. It replays REAL responses captured from the live
    service (tests/fixtures/rxnav_captured.json, re-captured with tests/fixtures/capture_rxnav.py), keyed by the exact request
    the loader makes; an unknown request is a 404. Failure injection: GET /__fail?on=1 makes every request 503 until on=0.
    Yields the control base URL, e.g. http://127.0.0.1:PORT."""
    captured = json.loads(RXNAV_FIXTURE.read_text())["responses"]
    names = list(captured["/Prescribe/displaynames.json"]["displayTermsList"]["term"]) + [RXNAV_OUTAGE_NAME]
    state = {"fail": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/__fail":
                state["fail"] = urllib.parse.parse_qs(parsed.query).get("on") == ["1"]
                return self._send(200, {"fail": state["fail"]})
            key = parsed.path[len("/REST"):] + ("?" + parsed.query if parsed.query else "")
            if state["fail"] or urllib.parse.unquote_plus(parsed.query).startswith(f"name={RXNAV_OUTAGE_NAME}"):
                return self._send(503, {"error": "stand-in outage"})
            if key == "/Prescribe/displaynames.json":
                return self._send(200, {"displayTermsList": {"term": names}})
            if key in captured:
                return self._send(200, captured[key])
            return self._send(404, {"error": "not captured: " + key})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="session")
def live_server(rxnav_stub):
    """Seeds a throwaway SQLite database and runs the app against it for the
    whole test session (one server, many tests -- faster than one per test,
    and this app's own migration/seed steps are already covered by being
    exercised here every run)."""
    with tempfile.TemporaryDirectory(prefix="npvehr-e2e-") as tmp_dir:
        db_path = Path(tmp_dir) / "test_ehr.db"
        # CRON_SECRET fixed (rather than the random-per-process dev fallback) so
        # tests can exercise the reminder cron endpoint's own auth check.
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "EHR_ENV": "test",
               "CRON_SECRET": "test-cron-secret", "RXNAV_BASE_URL": rxnav_stub + "/REST"}

        subprocess.run([sys.executable, "-m", "ehr.db.seed"], cwd=REPO_ROOT, env=env, check=True)

        _LIVE_DB["path"] = db_path
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "ehr.app:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=REPO_ROOT, env=env,
        )
        try:
            _wait_for_port(proc, port)
            yield f"http://127.0.0.1:{port}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    # tmp_dir (and the SQLite db + any -wal/-journal siblings) is removed here,
    # on the way out of the `with` block, regardless of how the block exited.


@pytest.fixture
def logged_in_page(live_server, page):
    """A page already signed in as the seeded System Administrator demo account."""
    page.goto(f"{live_server}/login")
    page.fill("#email", DEMO_EMAIL)
    page.fill("#password", DEMO_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")
    return page


@pytest.fixture(scope="session")
def drug_catalog_seed(live_server):
    """Clearly-labelled TEST drugs in the live server's catalog (the app seeds no drug data -- the catalog comes from RxNorm
    or an administrator). Returns {name: drug id}. Names are prefixed 'Zz' so they can never be mistaken for real drugs."""
    import sqlite3
    con = sqlite3.connect(_LIVE_DB["path"])
    try:
        ids = {}
        for name, route, dosages in (
            ("Zzdrop Ophthalmic Test", "ophthalmic", ["0.005% ophthalmic solution", "0.01% ophthalmic solution"]),
            ("Zzpill Oral Test", "oral", ["5 mg oral tablet", "10 mg oral tablet"]),
            ("Zznodose Test", "ophthalmic", []),
        ):
            row = con.execute("SELECT id FROM drug_catalog WHERE name = ?", (name,)).fetchone()
            if row:
                ids[name] = row[0]
                continue
            cur = con.execute("INSERT INTO drug_catalog (name, name_lower, default_route, source, is_active, created_at) "
                              "VALUES (?, ?, ?, 'manual', 1, datetime('now'))", (name, name.lower(), route))
            ids[name] = cur.lastrowid
            for label in dosages:
                con.execute("INSERT INTO drug_dosages (drug_id, label) VALUES (?, ?)", (cur.lastrowid, label))
        con.commit()
        return ids
    finally:
        con.close()



@pytest.fixture(scope="session")
def live_db_path(live_server):
    """Path of the live server's throwaway SQLite file, for tests that must read or seed rows the UI can't (e.g. legacy data)."""
    return _LIVE_DB["path"]

