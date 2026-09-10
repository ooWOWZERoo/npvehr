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
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

DEMO_EMAIL = "admin@newpathvision.example"
DEMO_PASSWORD = "ChangeMe123!"


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


@pytest.fixture(scope="session")
def live_server():
    """Seeds a throwaway SQLite database and runs the app against it for the
    whole test session (one server, many tests -- faster than one per test,
    and this app's own migration/seed steps are already covered by being
    exercised here every run)."""
    with tempfile.TemporaryDirectory(prefix="npvehr-e2e-") as tmp_dir:
        db_path = Path(tmp_dir) / "test_ehr.db"
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "EHR_ENV": "test"}

        subprocess.run([sys.executable, "-m", "ehr.db.seed"], cwd=REPO_ROOT, env=env, check=True)

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
