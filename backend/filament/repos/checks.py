"""Readiness checks chosen by repo kind.

Every check declares which kinds it applies to. Checks that do not apply are reported as
"skip" and excluded from the score, so an infrastructure or config repo is not marked down for
lacking unit tests or a Dockerfile. Checks with a `generate` id can be created inside Filament
from the Launchpad view (see repos/generate.py).
"""

from __future__ import annotations

import re

from filament.readiness.scanner import Check, Repo, _one_command
from filament.repos.profile import Profile

ALL = {"frontend", "mobile", "backend", "api", "infrastructure", "config", "library", "data",
       "docs", "monorepo", "unknown"}
CODE = {"frontend", "mobile", "backend", "api", "library", "data", "unknown"}
RUNNABLE = {"frontend", "mobile", "backend", "api", "data", "unknown"}


def _ci_text(repo: Repo) -> tuple[str | None, str]:
    files = [str(p) for p in repo.files
             if str(p).replace("\\", "/").startswith(".github/workflows/")
             and p.suffix in {".yml", ".yaml"}]
    files += [f for f in (".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml",
                          "azure-pipelines.yml", "bitbucket-pipelines.yml") if repo.exists(f)]
    return (files[0] if files else None), "\n".join(repo.read(f, 100_000) for f in files)


def run_checks(repo: Repo, profile: Profile) -> list[Check]:
    kinds = set(profile.kind_names) or {"unknown"}
    checks: list[Check] = []
    ci_file, ci = _ci_text(repo)
    tf_files = [str(p) for p in repo.files if p.suffix == ".tf"]
    tf_text = "\n".join(repo.read(f, 50_000) for f in tf_files[:200])

    def add(id_, title, weight, applies, found, ok, bad, fix, kind="common", generate=None):
        applicable = bool(kinds & applies)
        if not applicable:
            checks.append(Check(id_, title, weight, False, "Not applicable to this repo type",
                                fix, kind=kind, status="skip", generate=None))
            return
        passed = bool(found)
        checks.append(Check(id_, title, weight, passed, ok.format(found=found) if passed else bad,
                            fix, kind=kind, status="pass" if passed else "fail",
                            generate=None if passed else generate))

    # -- common to every repo -----------------------------------------------------------
    readme = repo.exists("README.md", "README.rst", "README")
    add("readme", "README explains the project", 10, ALL,
        readme if readme and len(repo.read(readme)) >= 300 else None,
        "Found {found}", "README missing or under 300 characters",
        "Add a README covering purpose, setup, how to run and how to test.",
        generate="overview")
    agent_file = repo.exists("AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md",
                             ".cursorrules", ".cursor/rules")
    add("agent_instructions", "Agent instructions file", 15, ALL, agent_file, "Found {found}",
        "No AGENTS.md / CLAUDE.md / copilot-instructions.md",
        "Add AGENTS.md with build/test commands, layout, conventions and do-not-touch areas.",
        generate="agents_md")
    owners = repo.exists("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS")
    if owners and not any(line.strip() and not line.lstrip().startswith("#")
                          for line in repo.read(owners).splitlines()):
        owners = None
    add("codeowners", "Code owners defined", 8, ALL, owners, "Found {found}",
        "No CODEOWNERS file with an active ownership rule",
        "Add .github/CODEOWNERS so agents and reviewers know who owns what.",
        generate="codeowners")
    add("ci", "CI pipeline configured", 10, ALL, ci_file, "Found {found}", "No CI configuration",
        "Add a CI workflow that validates every pull request.")
    docs = repo.exists("CONTRIBUTING.md", "docs/README.md", "docs/index.md", "ARCHITECTURE.md")
    if not docs and any(f.startswith("docs/") for f in repo.rel):
        docs = "docs/"
    add("docs", "Contributor and architecture docs", 7, ALL, docs, "Found {found}",
        "No CONTRIBUTING.md, ARCHITECTURE.md or docs/",
        "Add CONTRIBUTING.md and a short ARCHITECTURE.md.", generate="architecture")
    env_files = [f for f in repo.rel if f.split("/")[-1] in {".env", ".env.local", ".env.prod"}]
    add("no_secrets_files", "No .env files committed", 5, ALL, not env_files, "None found",
        f"Found {', '.join(env_files[:3])}",
        "Remove committed .env files and rotate any secrets in them.")

    # -- application code ------------------------------------------------------------------
    test_suffixes = ("_test.go", ".test.ts", ".test.js", ".test.tsx", ".spec.ts", ".spec.js",
                     ".spec.tsx", "test.java", "_test.py", "_spec.rb")
    tests = [f for f in repo.rel if "/test" in f"/{f}" or f.split("/")[-1].startswith("test_")
             or f.endswith(test_suffixes)]
    add("tests", "Automated tests present", 15, CODE,
        f"{len(tests)} test files" if tests else None, "{found}", "No test files found",
        "Add a test suite; agents need tests to verify their changes.", kind="code")
    add("one_command", "Build and test in one command", 10, CODE, _one_command(repo),
        "Via {found}", "No single build/test entry point detected",
        "Add a Makefile (or package.json scripts) with `build` and `test` targets.", kind="code")
    lint = repo.exists_anywhere(".editorconfig", "ruff.toml", ".ruff.toml", ".flake8",
                                ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs",
                                "eslint.config.js", "eslint.config.mjs", "biome.json",
                                ".prettierrc", ".golangci.yml", "rustfmt.toml", "checkstyle.xml",
                                ".yamllint", ".yamllint.yml", ".tflint.hcl")
    if not lint:
        for f in repo.files:
            if f.name == "pyproject.toml" and "[tool.ruff" in repo.read(str(f)):
                lint = f"{f} [tool.ruff]"
                break
    add("lint", "Lint and format config", 7, CODE | {"infrastructure", "config"}, lint,
        "Found {found}", "No lint/format config",
        "Add linter and formatter config so agent output matches team style.")
    add("dev_env", "Reproducible dev environment", 8, RUNNABLE,
        repo.exists_anywhere("Dockerfile", "docker-compose.yml", "compose.yaml",
                             "devcontainer.json", "devbox.json", ".tool-versions", ".nvmrc",
                             ".python-version", "flake.nix"),
        "Found {found}", "No container, devcontainer or version pin files",
        "Add a Dockerfile or .devcontainer, or pin tool versions.", kind="code")
    lock = repo.exists_anywhere("uv.lock", "poetry.lock", "requirements.txt", "package-lock.json",
                                "pnpm-lock.yaml", "yarn.lock", "go.sum", "cargo.lock",
                                "gradle.lockfile", "Gemfile.lock", "composer.lock",
                                ".terraform.lock.hcl")
    add("lockfile", "Dependencies pinned", 5, CODE | ({"infrastructure"} if tf_files else set()),
        lock, "Found {found}", "No lockfile",
        "Commit a lockfile (or .terraform.lock.hcl) so builds are reproducible.")

    # -- API --------------------------------------------------------------------------------
    specs = [str(p) for p in repo.files if re.match(
        r"(openapi|swagger|asyncapi)[\w.-]*\.(ya?ml|json)$", p.name.lower())
        or p.suffix in {".proto", ".graphql"}]
    add("api_contract", "API contract checked in", 10, {"api"}, specs[0] if specs else None,
        "Found {found}", "No OpenAPI / AsyncAPI / proto / GraphQL contract",
        "Check in an API contract; Filament can generate one from the code.", kind="api",
        generate="api_contract")

    # -- frontend ----------------------------------------------------------------------------
    ui_tests = repo.exists_anywhere(".storybook") or next(
        (f for f in repo.rel if f.endswith((".test.tsx", ".test.jsx", ".spec.tsx", ".stories.tsx",
                                            ".stories.jsx", ".test.vue", ".stories.ts"))), None)
    if not ui_tests and any(p.parts and p.parts[0] == ".storybook" for p in repo.files):
        ui_tests = ".storybook/"
    add("ui_component_tests", "Component tests or Storybook", 7, {"frontend", "mobile"},
        ui_tests, "Found {found}", "No component tests or stories",
        "Add component tests (Testing Library) or Storybook stories.", kind="frontend")
    e2e = repo.exists_anywhere("playwright.config.ts", "playwright.config.js", "cypress.config.ts",
                               "cypress.config.js", "cypress.json", "wdio.conf.js")
    add("e2e_tests", "End-to-end tests", 7, {"frontend"}, e2e, "Found {found}",
        "No Playwright or Cypress config", "Add Playwright or Cypress end-to-end tests.",
        kind="frontend")
    pkg_text = "\n".join(repo.read(m) for m in profile.manifests if m.endswith("package.json"))
    a11y = next((d for d in ("eslint-plugin-jsx-a11y", "@axe-core", "eslint-plugin-vuejs-accessibility",
                             "@angular-eslint/template", "axe-playwright", "jest-axe")
                 if d in pkg_text), None)
    add("a11y_lint", "Accessibility linting", 5, {"frontend"}, a11y, "Uses {found}",
        "No accessibility linter or axe checks",
        "Add eslint-plugin-jsx-a11y (or equivalent) and axe checks.", kind="frontend")

    # -- infrastructure ------------------------------------------------------------------------
    validate = re.search(r"terraform\s+(validate|fmt)|helm\s+lint|kubeconform|kubeval|"
                         r"ansible-lint|cfn-lint|pulumi\s+preview|kustomize\s+build", ci)
    add("iac_validate_ci", "Infrastructure validated in CI", 10, {"infrastructure"},
        validate.group(0) if validate else None, "CI runs `{found}`",
        "CI does not validate infrastructure code",
        "Run terraform fmt/validate, helm lint or kubeconform on every pull request.",
        kind="infrastructure")
    policy = re.search(r"tflint|checkov|tfsec|trivy|kics|conftest|terrascan|kube-linter", ci) \
        or (repo.exists_anywhere(".tflint.hcl", ".checkov.yml", ".checkov.yaml", ".trivyignore"))
    add("iac_policy_scan", "Policy and security scanning", 8, {"infrastructure"},
        (policy.group(0) if hasattr(policy, "group") else policy) if policy else None,
        "Uses {found}", "No tflint, checkov, tfsec, trivy or conftest",
        "Add a policy scanner (checkov, tfsec, trivy or conftest) to CI.", kind="infrastructure")
    if tf_files:
        backend = re.search(r'backend\s+"(\w+)"|cloud\s*\{', tf_text)
        add("remote_state", "Remote Terraform state", 8, {"infrastructure"},
            backend.group(0) if backend else None, "Configured: {found}",
            "No backend block: state would live on someone's laptop",
            "Configure a remote backend (S3 + DynamoDB lock, GCS, Terraform Cloud).",
            kind="infrastructure")
        pinned = re.search(r"required_providers\s*\{[^}]*version\s*=", tf_text, re.S)
        add("provider_pins", "Provider versions pinned", 5, {"infrastructure"},
            "required_providers with versions" if pinned else None, "{found}",
            "Providers are not version-pinned",
            "Pin providers in required_providers with version constraints.",
            kind="infrastructure")
        mod_dirs = sorted({str(p.parent) for p in repo.files if p.suffix == ".tf"
                           and "modules" in p.parts})
        undocumented = [d for d in mod_dirs if not repo.exists(f"{d}/README.md")]
        add("modules_documented", "Modules documented", 5, {"infrastructure"},
            not undocumented, f"{len(mod_dirs)} module(s) documented",
            f"No README in {', '.join(undocumented[:3])}",
            "Add a README with inputs and outputs to every module.", kind="infrastructure")
    state = [f for f in repo.rel if f.endswith((".tfstate", ".tfstate.backup"))]
    add("no_state_files", "No state files committed", 8, {"infrastructure"}, not state,
        "None found", f"Committed: {', '.join(state[:3])}",
        "Remove *.tfstate from the repo (it can contain secrets) and use remote state.",
        kind="infrastructure")

    # -- config --------------------------------------------------------------------------------
    schema = next((f for f in repo.rel if f.endswith((".schema.json", ".schema.yaml", ".cue"))
                   or "/schemas/" in f"/{f}"), None)
    add("config_schema", "Config schema defined", 8, {"config"}, schema, "Found {found}",
        "No JSON Schema / CUE schema for the config",
        "Add a schema so changes are validated before they ship.", kind="config")
    env_dirs = sorted({part for p in repo.files for part in p.parts[:-1]
                       if part.lower() in {"dev", "staging", "stage", "qa", "uat", "prod",
                                           "production"}})
    add("env_separation", "Environments separated", 7, {"config"},
        ", ".join(env_dirs) if env_dirs else None, "Environments: {found}",
        "No per-environment structure (dev / staging / prod)",
        "Keep each environment's values in its own folder or file.", kind="config")
    cfg_ci = re.search(r"check-jsonschema|ajv|yamllint|kubeconform|cue\s+vet|validate", ci)
    add("config_validation_ci", "Config validated in CI", 8, {"config"},
        cfg_ci.group(0) if cfg_ci else None, "CI runs `{found}`",
        "CI does not validate the config",
        "Validate every change against the schema in CI.", kind="config")
    leaks = []
    if kinds & {"config", "infrastructure"}:
        secret_line = re.compile(r"""(?i)^\s*["']?[\w.-]*(password|passwd|secret|api[_-]?key|token)"""
                                 r"""["']?\s*[:=]\s*["']?([^"'\s${}<>]{6,})""", re.M)
        for p in repo.files:
            if p.suffix.lower() in {".yaml", ".yml", ".json", ".env", ".properties", ".tfvars",
                                    ".ini", ".toml"} and len(leaks) < 3:
                for m in secret_line.finditer(repo.read(str(p), 100_000)):
                    if not re.match(r"(?i)(changeme|example|dummy|xxx|redacted|true|false)", m.group(2)):
                        leaks.append(f"{p}:{repo.read(str(p)).count(chr(10), 0, m.start()) + 1}")
                        break
    add("no_plaintext_secrets", "No plaintext secrets in config", 10,
        {"config", "infrastructure"}, not leaks, "None found",
        f"Possible secrets at {', '.join(leaks)}",
        "Move secrets to a secret manager and reference them.", kind="config")

    # -- library --------------------------------------------------------------------------------
    add("changelog", "Changelog maintained", 5, {"library"},
        repo.exists("CHANGELOG.md", "CHANGES.md", "HISTORY.md"), "Found {found}",
        "No CHANGELOG.md", "Keep a CHANGELOG so consumers see what changed.", kind="library")
    release = re.search(r"npm publish|twine upload|pypa/gh-action-pypi-publish|goreleaser|"
                        r"semantic-release|changesets|cargo publish|mvn deploy", ci)
    add("release_workflow", "Automated releases", 5, {"library"},
        release.group(0) if release else None, "CI runs `{found}`", "No release workflow",
        "Automate versioned releases from CI.", kind="library")
    return checks


def score(checks: list[Check]) -> float:
    applicable = [c for c in checks if c.status != "skip"]
    total = sum(c.weight for c in applicable) or 1
    return round(100 * sum(c.weight for c in applicable if c.passed) / total, 1)
