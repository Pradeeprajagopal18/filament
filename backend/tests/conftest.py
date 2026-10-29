import os

os.environ["FILAMENT_DATABASE_URL"] = "sqlite://"  # in-memory per test session
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests always run offline

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from filament.db import Base, get_session, make_engine  # noqa: E402
from filament.main import create_app  # noqa: E402


@pytest.fixture
def client():
    engine = make_engine("sqlite://", poolclass=StaticPool)
    from filament import models  # noqa: F401

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
