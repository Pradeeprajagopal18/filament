"""Deterministic repo readiness checks (no model needed) plus generated fixes."""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", "target",
               "dist-demo", ".next", ".nuxt", "out", "coverage", "vendor", ".terraform",
               ".pytest_cache", ".ruff_cache", ".mypy_cache", "bower_components"}


@dataclass
class Check:
    id: str
    title: str
    weight: int
    passed: bool
    detail: str
    fix: str
    kind: str = "common"  # which repo kind the check belongs to
    status: str = ""  # pass | fail | skip (skip = not applicable, excluded from the score)
    generate: str | None = None  # generator Filament can run to create the missing item

    def __post_init__(self):
        if not self.status:
            self.status = "pass" if self.passed else "fail"


@dataclass
class ReadinessResult:
    repo: str
    score: float
    grade: str
    stack: list[str]
    checks: list[Check] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)
    profile: dict = field(default_factory=dict)

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
            if path.name.endswith((".min.js", ".min.css", ".map")):
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

    def exists_anywhere(self, *names: str) -> str | None:
        """First file at any depth whose name matches (monorepo-friendly)."""
        wanted = {n.lower() for n in names}
        for p in self.files:
            if p.name.lower() in wanted:
                return str(p).replace("\\", "/")
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
    """Kind-aware readiness checks (see filament.repos.checks)."""
    from filament.repos.checks import run_checks as kind_checks
    from filament.repos.profile import detect

    return kind_checks(repo, detect(repo))


@contextmanager
def checkout(repo: str):
    """Yield a local path for a directory or a git URL (shallow clone into a temp dir)."""
    path = Path(repo).expanduser()
    if path.exists():
        yield path.resolve()
        return
    tmp = Path(tempfile.mkdtemp(prefix="filament-ready-"))
    token = None
    try:
        url = repo if "://" in repo or repo.startswith("git@") else f"https://{repo}"
        # Private repos: a token from the environment (never stored by Filament).
        token = os.environ.get("FILAMENT_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token and url.startswith("https://github.com/"):
            url = url.replace("https://", f"https://x-access-token:{token}@", 1)
        subprocess.run(["git", "clone", "--depth", "1", "--quiet", url, str(tmp / "repo")],
                       check=True, capture_output=True, timeout=180)
        yield tmp / "repo"
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="ignore")[:300]
        if token:
            stderr = stderr.replace(token, "***")
        raise ValueError(f"Could not clone {repo}: {stderr}") from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def scan(repo: str) -> ReadinessResult:
    from filament.repos.checks import run_checks as kind_checks
    from filament.repos.checks import score as score_checks
    from filament.repos.profile import detect

    with checkout(repo) as root:
        indexed = Repo(root)
        profile = detect(indexed)
        checks = kind_checks(indexed, profile)
        stack = detect_stack(indexed)
    score = score_checks(checks)
    return ReadinessResult(repo=repo, score=score, grade=grade_for(score), stack=stack,
                           checks=checks, kinds=profile.kind_names, profile=profile.to_dict())


def fix_files(result: ReadinessResult) -> dict[str, str]:
    """Starter files for failing checks. Humans (or the Readiness Agent) refine them."""
    failing = {c.id for c in result.checks if c.status == "fail"}
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
