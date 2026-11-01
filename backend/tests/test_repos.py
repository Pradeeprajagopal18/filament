"""Repo intelligence across repo kinds: profile, kind-aware readiness, API, UI, components."""

from pathlib import Path

import pytest

from filament.repos.analyze import analyze_path


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


FASTAPI = {
    "pyproject.toml": '[project]\nname="orders"\ndependencies=["fastapi","sqlalchemy","redis"]\n',
    "app/main.py": (
        "import os\nfrom fastapi import FastAPI, APIRouter\n"
        "app = FastAPI()\nrouter = APIRouter(prefix='/orders')\n"
        "@router.get('/{order_id}')\ndef get_order(order_id: int):\n    return {}\n"
        "@router.post('')\ndef create_order():\n    return {}\n"
        "app.include_router(router, prefix='/api')\n"
        "PAYMENTS = os.environ['PAYMENTS_SERVICE_URL']\n"
        "STRIPE = 'https://api.stripe.com/v1'\n"
    ),
    "tests/test_main.py": "def test_ok():\n    assert True\n",
    "docker-compose.yml": "services:\n  api:\n    build: .\n    depends_on: [db]\n"
                          "  db:\n    image: postgres:16\n",
}

EXPRESS = {
    "package.json": '{"name":"cart","dependencies":{"express":"4","mongoose":"8"}}',
    "src/server.js": (
        "const express = require('express');\nconst app = express();\n"
        "const r = express.Router();\nr.get('/items', list);\nr.delete('/items/:id', del);\n"
        "app.use('/cart', r);\napp.get('/health', ok);\n"
    ),
}

REACT = {
    "package.json": '{"name":"shop","dependencies":{"react":"19","react-router-dom":"7"}}',
    "src/App.tsx": (
        "import { Routes, Route } from 'react-router-dom';\n"
        "export default function App() {\n  return (\n    <Routes>\n"
        "      <Route path=\"/\" element={<Home />} />\n"
        "      <Route path=\"/checkout\" element={<Checkout />} />\n"
        "    </Routes>\n  );\n}\n"
    ),
    "src/Home.tsx": (
        "export function Home() {\n  return (\n    <main>\n      <h1>Welcome</h1>\n"
        "      <button>Shop now</button>\n    </main>\n  );\n}\n"
    ),
    "src/Checkout.tsx": (
        "export function Checkout() {\n  return (\n    <form>\n      <h2>Checkout</h2>\n"
        "      <input placeholder=\"Card number\" />\n      <button>Pay</button>\n"
        "    </form>\n  );\n}\n"
    ),
}

TERRAFORM = {
    "main.tf": (
        'terraform {\n  required_providers {\n    aws = { source = "hashicorp/aws" }\n  }\n}\n'
        'resource "aws_s3_bucket" "assets" { bucket = "x" }\n'
        'resource "aws_db_instance" "db" { engine = "postgres" }\n'
        'module "vpc" { source = "./modules/vpc" }\n'
    ),
    "modules/vpc/main.tf": 'resource "aws_vpc" "this" {}\n',
}

CONFIG = {
    "envs/dev/app.yaml": "replicas: 1\nlog_level: debug\n",
    "envs/prod/app.yaml": "replicas: 3\nlog_level: info\n",
    "envs/prod/flags.json": '{"new_checkout": false}',
}


def statuses(a: dict) -> dict[str, str]:
    return {c["id"]: c["status"] for c in a["readiness"]["checks"]}


@pytest.fixture
def make(tmp_path):
    def _make(files):
        root = write(tmp_path / "r", files)
        return analyze_path(root, str(root))
    return _make


def test_fastapi_backend(make):
    a = make(FASTAPI)
    assert {"backend", "api"} <= set(a["profile"]["kind_names"])
    eps = {(e["method"], e["path"]) for e in a["api"]["endpoints"]}
    assert ("GET", "/api/orders/{order_id}") in eps
    assert ("POST", "/api/orders") in eps
    assert a["api"]["source"] == "inferred"
    assert a["api"]["openapi"]["openapi"].startswith("3.")
    st = statuses(a)
    assert st["api_contract"] == "fail"  # inferred only: no committed contract
    assert st["iac_policy_scan"] == "skip" and st["e2e_tests"] == "skip"
    labels = {n["label"] for n in a["components"]["nodes"]}
    assert any("PostgreSQL" in x for x in labels) and any("Stripe" in x for x in labels)
    assert any("Redis" in x for x in labels)
    assert "flowchart" in a["components"]["mermaid"]


def test_express_api(make):
    a = make(EXPRESS)
    eps = {(e["method"], e["path"]) for e in a["api"]["endpoints"]}
    assert {("GET", "/cart/items"), ("DELETE", "/cart/items/{id}"), ("GET", "/health")} <= eps
    assert any("Mongo" in n["label"] for n in a["components"]["nodes"])


def test_react_frontend_pages_and_mock(make):
    a = make(REACT)
    assert "frontend" in a["profile"]["kind_names"]
    pages = {p["path"]: p for p in a["ui"]["pages"]}
    assert {"/", "/checkout"} <= set(pages)
    kinds = {e["type"] for e in pages["/checkout"]["elements"]}
    assert {"form", "input", "button"} <= kinds
    assert "Pay" in pages["/checkout"]["mock_html"]
    st = statuses(a)
    assert st["e2e_tests"] == "fail" and st["iac_validate_ci"] == "skip"


def test_terraform_infrastructure(make):
    a = make(TERRAFORM)
    assert a["profile"]["kind_names"][0] == "infrastructure"
    st = statuses(a)
    assert st["tests"] == "skip" and st["api_contract"] == "skip"
    assert st["remote_state"] == "fail" and st["provider_pins"] == "fail"
    assert a["api"]["endpoints"] == [] and a["ui"]["pages"] == []
    labels = " ".join(n["label"] for n in a["components"]["nodes"])
    assert "aws_s3_bucket" in labels or "assets" in labels


def test_config_repo(make):
    a = make(CONFIG)
    assert "config" in a["profile"]["kind_names"]
    st = statuses(a)
    assert st["env_separation"] == "pass"
    assert st["config_schema"] == "fail"
    assert st["tests"] == "skip" and st["one_command"] == "skip"


def test_onboard_and_generate_in_platform(client, ws, tmp_path):
    root = write(tmp_path / "svc", FASTAPI)
    r = client.post(f"/api/workspaces/{ws['id']}/repos", json={"repo": str(root)})
    assert r.status_code == 201, r.text
    repo = r.json()
    assert repo["project_id"] and "api" in repo["kinds"]
    # empty items -> every generator behind a failing check
    g = client.post(f"/api/repos/{repo['id']}/generate", json={}).json()
    assert {"agents_md", "api_contract"} <= set(g["generated"])
    art = client.get(f"/api/artifacts/{g['generated']['api_contract']}").json()
    assert art["status"] == "draft" and art["author"] == "agent:repo-onboarding"
    assert art["data"]["in_repo"] is False and art["data"]["openapi"]["paths"]
    # regenerating bumps the version instead of duplicating
    g2 = client.post(f"/api/repos/{repo['id']}/generate", json={"items": ["api_contract"]}).json()
    assert g2["generated"]["api_contract"] == g["generated"]["api_contract"]
    assert client.get(f"/api/artifacts/{g['generated']['api_contract']}").json()["version"] == 2
    mem = client.get(f"/api/workspaces/{ws['id']}/memory").json()
    assert mem and all(m["status"] == "proposed" for m in mem)
    assert "paths:" in client.get(f"/api/repos/{repo['id']}/openapi").text
    assert "openapi.yaml" in client.get(f"/api/repos/{repo['id']}/files").json()
    bad = client.post(f"/api/repos/{repo['id']}/generate", json={"items": ["nope"]})
    assert bad.status_code == 422
