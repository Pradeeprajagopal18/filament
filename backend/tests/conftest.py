import os

os.environ["IGNITE_DATABASE_URL"] = "sqlite://"  # in-memory per test session
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests always run offline
os.environ["IGNITE_AUTH_MODE"] = "none"  # most tests run as the local admin; test_access signs in
os.environ["IGNITE_REPO_SCHEDULER_ENABLED"] = "false"  # tests trigger scans explicitly
os.environ.setdefault("IGNITE_DATA_DIR", __import__("tempfile").mkdtemp(prefix="ignite-test-"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from igniteai.db import Base, get_session, make_engine  # noqa: E402
from igniteai.main import create_app  # noqa: E402


@pytest.fixture
def client():
    engine = make_engine("sqlite://", poolclass=StaticPool)
    from igniteai import models  # noqa: F401

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override():
        s = TestSession()
        try:
            yield s
        finally:
            s.close()

    app = create_app()
    app.dependency_overrides[get_session] = override
    with TestClient(app) as c:
        yield c


@pytest.fixture
def project(client):
    ws = client.post("/api/workspaces", json={"name": "Acme"}).json()
    p = client.post(f"/api/workspaces/{ws['id']}/projects",
                    json={"name": "Checkout v2", "description": "Payments checkout"}).json()
    return ws, p


@pytest.fixture
def ws(client):
    return client.post("/api/workspaces", json={"name": "Acme"}).json()
