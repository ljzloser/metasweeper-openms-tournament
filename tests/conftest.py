"""使用独立插件目录测试，宿主来源可由环境变量覆盖。"""
import os
from pathlib import Path
import sys
import pytest
import requests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("METASWEEPER_SRC", ROOT.parents[1] / "Solvable-Minesweeper" / "src"))
for path in (ROOT, SOURCE):
    sys.path.insert(0, str(path))


@pytest.fixture(autouse=True)
def block_live_http(monkeypatch):
    """All tests must use fake HTTP; never send a real replay or login request."""
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not send real HTTP requests; use a fake session.")

    monkeypatch.setattr(requests.sessions.Session, "send", blocked)


@pytest.fixture(autouse=True)
def virtual_request_time(monkeypatch):
    """Exercise client pacing without real sleeps; direct policy tests use their own clocks."""
    from openms_tournament import client
    from openms_tournament.request_policy import RequestPolicy

    def policy():
        state = [10000.0]

        def wait(delay):
            state[0] += delay

        return RequestPolicy(clock=lambda: state[0], wall_clock=lambda: state[0], wait=wait)

    monkeypatch.setattr(client, 'RequestPolicy', policy)
