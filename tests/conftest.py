import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("PYTHONUTF8", "1")
os.environ["VERA_LLM_MODE"] = "off"          # tests are deterministic; LLM paths are tested with fakes
os.environ["LLM_CACHE_PATH"] = ""            # never read/write the real cache file from tests
os.environ["LLM_OUTBOUND"] = "off"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.store import store  # noqa: E402
from tests.dataset import load_all  # noqa: E402


@pytest.fixture(scope="session")
def data():
    return load_all()


@pytest.fixture()
def client():
    store.reset()
    with TestClient(app) as c:
        yield c
    store.reset()


def push(client, scope, cid, payload, version=1):
    return client.post("/v1/context", json={"scope": scope, "context_id": cid, "version": version,
                                            "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


@pytest.fixture()
def loaded(client, data):
    for scope in ("category", "merchant", "customer", "trigger"):
        for cid, p in data[scope].items():
            assert push(client, scope, cid, p).status_code == 200
    return client
