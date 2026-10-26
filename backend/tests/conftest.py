import os
import tempfile

_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.setdefault("DEVNEXA_DATABASE_URL", f"sqlite:///{_db}")
os.environ.setdefault("DEVNEXA_SIM_STEP_DELAY", "0")
os.environ.setdefault("DEVNEXA_SEED_DEMO_DATA", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.agents import runner  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def sync_agents():
    """Run agent workflows synchronously so tests are deterministic."""
    orig = runner.submit
    runner.submit = lambda fn, *a, sync=False, **kw: fn(*a, **kw)
    yield
    runner.submit = orig


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c
