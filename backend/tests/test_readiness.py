from pathlib import Path

from filament.readiness import fix_files, grade_for, scan


def test_empty_repo_fails_and_gets_fixes(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('hi')\n")
    result = scan(str(tmp_path))
    assert result.grade == "F"
    failing = {c.id for c in result.checks if not c.passed}
    assert {"agent_instructions", "tests", "ci", "codeowners"} <= failing
    files = fix_files(result)
    assert "AGENTS.md" in files and ".github/CODEOWNERS" in files


def test_well_structured_repo_scores_a(tmp_path: Path):
    files = {
        "README.md": "# Svc\n" + "Explains setup, running and testing. " * 20,
        "AGENTS.md": "Build with make build, test with make test.",
        ".github/CODEOWNERS": "* @acme/payments",
        ".github/workflows/ci.yml": "on: [pull_request]",
        "Makefile": "test:\n\tpytest",
        "tests/test_app.py": "def test_ok():\n    assert True",
        "pyproject.toml": "[tool.ruff]\nline-length = 100\n[tool.pytest.ini_options]\n",
        "openapi.yaml": "openapi: 3.1.0",
        "Dockerfile": "FROM python:3.12",
        "CONTRIBUTING.md": "How to contribute",
        "uv.lock": "",
    }
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    result = scan(str(tmp_path))
    assert result.grade == "A", [c for c in result.checks if not c.passed]
    assert "python" in result.stack


def test_committed_env_file_flagged(tmp_path: Path):
    (tmp_path / ".env").write_text("SECRET=1")
    result = scan(str(tmp_path))
    check = next(c for c in result.checks if c.id == "no_secrets_files")
    assert not check.passed


def test_grades():
    assert [grade_for(s) for s in (95, 80, 65, 45, 10)] == ["A", "B", "C", "D", "F"]


def test_scan_endpoint(client, tmp_path: Path):
    (tmp_path / "README.md").write_text("x")
    r = client.post("/api/readiness/scan", json={"repo": str(tmp_path)})
    assert r.status_code == 201
    body = r.json()
    assert body["grade"] in "ABCDF" and len(body["checks"]) == 12
    assert client.get("/api/readiness").json()[0]["id"] == body["id"]
