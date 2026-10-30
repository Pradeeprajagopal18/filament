"""Parse dependency manifests across ecosystems into one list of (ecosystem, name, file)."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from filament.readiness.scanner import Repo

MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "go.mod", "pom.xml",
             "build.gradle", "build.gradle.kts", "cargo.toml", "pubspec.yaml", "gemfile",
             "composer.json"}


@dataclass(frozen=True)
class Dependency:
    ecosystem: str
    name: str
    file: str
    dev: bool = False


def _rel(p) -> str:
    return str(p).replace("\\", "/")


def manifest_files(repo: Repo) -> list[str]:
    files = [_rel(p) for p in repo.files if p.name.lower() in MANIFESTS
             or p.suffix.lower() == ".csproj"]
    return sorted(files, key=lambda f: (f.count("/"), f))


def _py_name(spec: str) -> str:
    return re.split(r"[\s<>=!~;\[(@]", spec.strip(), maxsplit=1)[0].lower()


def parse(repo: Repo) -> list[Dependency]:
    deps: list[Dependency] = []
    for f in manifest_files(repo):
        name = PurePosixPath(f).name.lower()
        text = repo.read(f)
        try:
            if name == "package.json":
                data = json.loads(text or "{}")
                for section, dev in (("dependencies", False), ("devDependencies", True),
                                     ("peerDependencies", False)):
                    for dep in (data.get(section) or {}):
                        deps.append(Dependency("npm", dep.lower(), f, dev))
            elif name == "pyproject.toml":
                data = tomllib.loads(text)
                for spec in data.get("project", {}).get("dependencies", []) or []:
                    deps.append(Dependency("pypi", _py_name(spec), f))
                for group in (data.get("project", {}).get("optional-dependencies", {}) or {}).values():
                    deps += [Dependency("pypi", _py_name(s), f, True) for s in group]
                poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
                deps += [Dependency("pypi", d.lower(), f) for d in poetry if d != "python"]
            elif name == "requirements.txt":
                for line in text.splitlines():
                    line = line.split("#")[0].strip()
                    if line and not line.startswith(("-", "git+", "http")):
                        deps.append(Dependency("pypi", _py_name(line), f))
            elif name == "go.mod":
                for m in re.finditer(r"^\s*(?:require\s+)?([a-z0-9.\-]+\.[a-z]{2,}/[^\s]+)\s+v",
                                     text, re.M):
                    deps.append(Dependency("go", m.group(1).lower(), f))
            elif name == "pom.xml":
                for m in re.finditer(r"<artifactId>([^<]+)</artifactId>", text):
                    deps.append(Dependency("maven", m.group(1).lower(), f))
            elif name.startswith("build.gradle"):
                for m in re.finditer(r"['\"]([\w.\-]+):([\w.\-]+)(?::[^'\"]*)?['\"]", text):
                    deps.append(Dependency("maven", m.group(2).lower(), f))
            elif name.endswith(".csproj"):
                for m in re.finditer(r'PackageReference\s+Include="([^"]+)"', text):
                    deps.append(Dependency("nuget", m.group(1).lower(), f))
            elif name == "cargo.toml":
                data = tomllib.loads(text)
                deps += [Dependency("cargo", d.lower(), f) for d in data.get("dependencies", {})]
            elif name == "pubspec.yaml":
                for m in re.finditer(r"^\s{2}([a-z_][a-z0-9_]*):", text, re.M):
                    deps.append(Dependency("pub", m.group(1), f))
        except (json.JSONDecodeError, tomllib.TOMLDecodeError, AttributeError):
            continue
    return deps


def names(deps: list[Dependency]) -> set[str]:
    return {d.name for d in deps}
