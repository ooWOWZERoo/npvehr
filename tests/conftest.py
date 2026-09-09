"""End-to-end test fixtures: launch the real app (uvicorn) against a fresh,
seeded SQLite database for the whole test session, and drive it with a real
browser via Playwright. These are true end-to-end tests against the running
app over HTTP -- not unit tests against internal functions -- so they catch
what those can't: template rendering, routing, auth redirects, form posts.
"""
import os
import socket
import subprocess
import sys
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


def _wait_for_port(port: int, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"Server on 127.0.0.1:{port} did not start within {timeout}s")


@pytest.fixture(scope="session")
def live_server(tmp_path_factory):
    """Seeds a throwaway SQLite database and runs the app against it for the
    whole test session (one server, many tests -- faster than one per test,
    and this app's own migration/seed steps are already covered by being
    exercised here every run)."""
    db_path = tmp_path_factory.mktemp("ehr-e2e") / "test_ehr.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_path}", "EHR_ENV": "test"}

    subprocess.run([sys.executable, "-m", "ehr.db.seed"], cwd=REPO_ROOT, env=env, check=True)

    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ehr.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO_ROOT, env=env,
    )
    try:
        _wait_for_port(port)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def logged_in_page(live_server, page):
    """A page already signed in as the seeded System Administrator demo account."""
    page.goto(f"{live_server}/login")
    page.fill("#email", DEMO_EMAIL)
    page.fill("#password", DEMO_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{live_server}/")
    return page
