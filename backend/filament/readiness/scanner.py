"""Deterministic repo readiness checks (no model needed) plus generated fixes."""

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", "target"}


@dataclass
class Check:
    id: str
    title: str
    weight: int
    passed: bool
    detail: str
    fix: str


@dataclass
class ReadinessResult:
    repo: str
    score: float
    grade: str
    stack: list[str]
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def grade_for(score: float) -> str:
    for threshold, grade in ((90, "A"), (75, "B"), (60, "C"), (40, "D")):
        if score >= threshold:
            return grade
    return "F"


class Repo:
    """Indexed view of a repository's files (bounded walk, ignores vendored dirs)."""

    def __init__(self, root: Path, max_files: int = 20_000):
        self.root = root
        self.files: list[Path] = []
        for path in root.rglob("*"):
            if any(part in IGNORE_DIRS for part in path.relative_to(root).parts):
                continue
            if path.is_file():
                self.files.append(path.relative_to(root))
                if len(self.files) >= max_files:
                    break
        self.names = {p.name.lower() for p in self.files}
        self.rel = {str(p).replace("\\", "/").lower() for p in self.files}

    def exists(self, *candidates: str) -> str | None:
        for c in candidates:
            if c.lower() in self.rel:
                return c
        return None

    def any_name(self, predicate: Callable[[str], bool]) -> list[str]:
        return [str(p) for p in self.files if predicate(p.name.lower())]

    def read(self, rel: str, limit: int = 200_000) -> str:
        try:
            return (self.root / rel).read_text(errors="ignore")[:limit]
        except OSError:
            return ""


def detect_stack(repo: Repo) -> list[str]:
    stack = []
    if repo.exists("pyproject.toml", "requirements.txt", "setup.py"):
        stack.append("python")
    if repo.exists("package.json"):
        stack.append("node")
    if repo.exists("go.mod"):
        stack.append("go")
    if repo.exists("pom.xml", "build.gradle", "build.gradle.kts"):
        stack.append("jvm")
    if repo.exists("cargo.toml"):
        stack.append("rust")
    return stack


def _one_command(repo: Repo) -> str | None:
    if repo.exists("Makefile", "justfile", "Taskfile.yml", "noxfile.py", "tox.ini"):
        return repo.exists("Makefile", "justfile", "Taskfile.yml", "noxfile.py", "tox.ini")
    if repo.exists("package.json"):
        try:
            scripts = json.loads(repo.read("package.json")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if "test" in scripts and "build" in scripts:
            return "package.json scripts (build, test)"
    if repo.exists("pyproject.toml") and "pytest" in repo.read("pyproject.toml"):
        return "pyproject.toml (pytest)"
    if repo.exists("go.mod") or repo.exists("cargo.toml"):
        return "native toolchain (go test / cargo test)"
    if repo.exists("gradlew", "mvnw"):
        return "build wrapper"
    return None


def run_checks(repo: Repo) -> list[Check]:
    checks: list[Check] = []

    def add(id_, title, weight, found, detail_ok, detail_bad, fix):
        checks.append(Check(id_, title, weight, bool(found),
                            detail_ok.format(found=found) if found else detail_bad, fix))

    readme = repo.exists("README.md", "README.rst", "README")
    add("readme", "README explains the project", 10,
        readme if readme and len(repo.read(readme)) >= 300 else None,
        "Found {found}", "README missing or under 300 characters",
        "Add a README covering purpose, setup, how to run and how to test.")

    agent_file = repo.exists("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md",
                             ".cursorrules", ".cursor/rules")
    add("agent_instructions", "Agent instructions file", 15, agent_file,
        "Found {found}", "No AGENTS.md / CLAUDE.md / copilot-instructions.md",
        "Add AGENTS.md with build/test commands, layout, conventions and do-not-touch areas.")

    owners = repo.exists("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
    add("codeowners", "Code owners defined", 8, owners, "Found {found}", "No CODEOWNERS file",
        "Add .github/CODEOWNERS so agents and reviewers know who owns what.")

    test_suffixes = ("_test.go", ".test.ts", ".test.js", ".spec.ts", ".spec.js", "test.java")
    tests = [f for f in repo.rel if "/test" in f"/{f}" or f.split("/")[-1].startswith("test_")
             or f.endswith(test_suffixes)]
    add("tests", "Automated tests present", 15,
        f"{len(tests)} test files" if tests else None, "{found}", "No test files found",
        "Add a test suite; agents need tests to verify their changes.")

    ci = repo.any_name(lambda n: n.endswith((".yml", ".yaml")))
    ci = [c for c in ci if c.replace("\\", "/").startswith(".github/workflows/")]
    ci_found = (ci[0] if ci else None) or repo.exists(".gitlab-ci.yml", "Jenkinsfile",
                                                     ".circleci/config.yml", "azure-pipelines.yml")
    add("ci", "CI pipeline configured", 10, ci_found, "Found {found}", "No CI configuration",
        "Add a CI workflow that builds and runs tests on every pull request.")

    add("one_command", "Build and test in one command", 10, _one_command(repo),
        "Via {found}", "No single build/test entry point detected",
        "Add a Makefile (or package.json scripts) with `build` and `test` targets.")

    lint = repo.exists(".editorconfig", "ruff.toml", ".ruff.toml", ".flake8", ".eslintrc",
                       ".eslintrc.js", ".eslintrc.json", "eslint.config.js", "eslint.config.mjs",
                       "biome.json", ".prettierrc", ".golangci.yml", "rustfmt.toml",
                       "checkstyle.xml")
    if not lint and repo.exists("pyproject.toml") and "[tool.ruff" in repo.read("pyproject.toml"):
        lint = "pyproject.toml [tool.ruff]"
    add("lint", "Lint and format config", 7, lint, "Found {found}", "No lint/format config",
        "Add linter and formatter config so agent output matches team style.")

    specs = repo.any_name(lambda n: n.startswith(("openapi", "swagger", "asyncapi"))
                          and n.endswith((".yaml", ".yml", ".json")))
    specs += repo.any_name(lambda n: n.endswith(".proto") or n.endswith(".graphql"))
    add("api_specs", "API contracts checked in", 5, specs[0] if specs else None,
        "Found {found}", "No OpenAPI/AsyncAPI/proto/GraphQL specs",
        "Check in API specs so agents and simulations can use the contract.")

    dev = repo.exists("Dockerfile", "docker-compose.yml", "compose.yaml",
                      ".devcontainer/devcontainer.json", "devbox.json", ".tool-versions",
                      ".nvmrc", ".python-version", "flake.nix")
    add("dev_env", "Reproducible dev environment", 8, dev, "Found {found}",
        "No container, devcontainer or version pin files",
        "Add a Dockerfile or .devcontainer, or pin tool versions.")

    docs = repo.exists("CONTRIBUTING.md", "docs/README.md", "docs/index.md", "ARCHITECTURE.md")
    if not docs and any(f.startswith("docs/") for f in repo.rel):
        docs = "docs/"
    add("docs", "Contributor and architecture docs", 7, docs, "Found {found}",
        "No CONTRIBUTING.md, ARCHITECTURE.md or docs/",
        "Add CONTRIBUTING.md and a short ARCHITECTURE.md.")

    lock = repo.exists("uv.lock", "poetry.lock", "requirements.txt", "package-lock.json",
                       "pnpm-lock.yaml", "yarn.lock", "go.sum", "cargo.lock", "gradle.lockfile")
    add("lockfile", "Dependencies pinned", 5, lock, "Found {found}", "No lockfile",
        "Commit a lockfile so agent builds are reproducible.")

    env_files = [f for f in repo.rel if f.split("/")[-1] in {".env", ".env.local", ".env.prod"}]
    checks.append(Check("no_secrets_files", "No .env files committed", 5, not env_files,
                        "None found" if not env_files else f"Found {', '.join(env_files[:3])}",
                        "Remove committed .env files and rotate any secrets in them."))
    return checks


@contextmanager
def checkout(repo: str):
    """Yield a local path for a directory or a git URL (shallow clone into a temp dir)."""
    path = Path(repo).expanduser()
    if path.exists():
        yield path.resolve()
        return
    tmp = Path(tempfile.mkdtemp(prefix="filament-ready-"))
    try:
        url = repo if "://" in repo or repo.startswith("git@") else f"https://{repo}"
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, str(tmp / "repo")],
                       check=True, capture_output=True, timeout=180)
        yield tmp / "repo"
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="ignore")[:300]
        raise ValueError(f"Could not clone {repo}: {stderr}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scan(repo: str) -> ReadinessResult:
    with checkout(repo) as root:
        indexed = Repo(root)
        checks = run_checks(indexed)
        stack = detect_stack(indexed)
    total = sum(c.weight for c in checks)
    score = round(100 * sum(c.weight for c in checks if c.passed) / total, 1)
    return ReadinessResult(repo=repo, score=score, grade=grade_for(score), stack=stack,
                           checks=checks)


def fix_files(result: ReadinessResult) -> dict[str, str]:
    """Starter files for failing checks. Humans (or the Readiness Agent) refine them."""
    failing = {c.id for c in result.checks if not c.passed}
    stack = ", ".join(result.stack) or "unknown"
    files: dict[str, str] = {}
    if "agent_instructions" in failing:
        files["AGENTS.md"] = (
            "# Agent instructions\n\n"
            f"Stack: {stack}\n\n"
            "## Build and test\n- Build: `make build` (TODO: confirm)\n- Test: `make test`\n\n"
            "## Layout\n- TODO: describe the main directories\n\n"
            "## Conventions\n- Follow the lint and format config in this repo\n"
            "- Every change ships with tests\n\n"
            "## Do not touch\n- TODO: generated code, migrations, infra owned by other teams\n"
        )
    if "codeowners" in failing:
        files[".github/CODEOWNERS"] = (
            "# Default owners for everything in the repo\n* @your-org/your-team\n"
        )
    if "one_command" in failing:
        files["Makefile"] = (
            ".PHONY: build test\nbuild:\n\t@echo 'TODO: build'\ntest:\n\t@echo 'TODO: test'\n"
        )
    if "docs" in failing:
        files["CONTRIBUTING.md"] = (
            "# Contributing\n\n1. Create a branch\n2. Make the change with tests\n"
            "3. Run `make test`\n4. Open a pull request; CODEOWNERS will review\n"
        )
    return files
