#!/usr/bin/env python3
"""Credential-free repository contract checks for the M1 bootstrap."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {".git", ".venv", "artifacts", "__pycache__"}
TEXT_NAMES = {
    ".editorconfig",
    ".gitattributes",
    ".gitignore",
    "Dockerfile",
    "Makefile",
}
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
REQUIRED_FILES = (
    "README.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "Makefile",
    ".tool-versions",
    ".pre-commit-config.yaml",
    ".github/workflows/verify.yml",
    ".github/workflows/promote.yml",
    "infrastructure/local/kind-dev.yaml",
    "infrastructure/local/kind-prod.yaml",
    "scripts/platform.sh",
    "scripts/platform.ps1",
    "scripts/build.py",
    "scripts/gitops.py",
    "scripts/drift.py",
    "scripts/promote.py",
    "apps/Dockerfile",
    "apps/services.json",
    "apps/shared/platform_common/__init__.py",
    "apps/shared/platform_common/config.py",
    "apps/shared/platform_common/httpapp.py",
    "apps/shared/platform_common/logging.py",
    "apps/shared/platform_common/metrics.py",
    "apps/shared/platform_common/runtime.py",
    "apps/shared/platform_common/tracing.py",
    "apps/shared/platform_common/version.py",
    "platform/argocd/install/kustomization.yaml",
    "platform/argocd/root/kustomization.yaml",
    "platform/argocd/root/root-application.yaml",
    "platform/argocd/root/workloads-applicationset.yaml",
    "platform/argocd/root/projects/platform.yaml",
    "platform/argocd/root/projects/workloads.yaml",
    "platform/workloads/base/kustomization.yaml",
    "environments/dev/kustomization.yaml",
    "environments/dev/promotion-history.json",
    "environments/production/kustomization.yaml",
    "environments/production/promotion-history.json",
    "docs/promotion.md",
    "docs/runbooks/failed-promotion.md",
    "docs/runbooks/rollback.md",
    "docs/architecture.md",
    "docs/threat-model.md",
    "docs/cost-model.md",
    "docs/testing-strategy.md",
    "docs/demo-script.md",
    "docs/operational-readiness.md",
    "docs/adr/0001-local-kind-before-eks.md",
    "docs/adr/0002-gitops-ownership-boundaries.md",
    "docs/adr/0003-immutable-artifact-promotion.md",
    "docs/adr/0004-standard-library-service-runtime.md",
    "docs/adr/0005-argo-cd-manages-everything-except-itself.md",
    "docs/gitops.md",
    "docs/runbooks/local-bootstrap-failure.md",
    "docs/runbooks/application-out-of-sync.md",
)
SECRET_PATTERNS = {
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh[oprsu]_[A-Za-z0-9_]{36,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
}
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def text_files() -> list[Path]:
    """Return repository text files while excluding generated state."""
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        if path.name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return sorted(files)


def check_required_files(errors: list[str]) -> None:
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            errors.append(f"missing required file: {relative}")


def check_text_hygiene(files: list[Path], errors: list[str]) -> None:
    for path in files:
        relative = path.relative_to(ROOT)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{relative}: is not UTF-8 ({exc})")
            continue
        if text and not text.endswith("\n"):
            errors.append(f"{relative}: missing final newline")
        for number, line in enumerate(text.splitlines(), start=1):
            if line.endswith((" ", "\t")):
                errors.append(f"{relative}:{number}: trailing whitespace")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                errors.append(f"{relative}: possible {label}")


def check_markdown_links(errors: list[str]) -> None:
    for path in sorted(ROOT.rglob("*.md")):
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().strip("<>")
            if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target = unquote(target.split("#", maxsplit=1)[0].split("?", maxsplit=1)[0])
            resolved = (path.parent / target).resolve()
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                errors.append(f"{path.relative_to(ROOT)}: link escapes repository: {raw_target}")
                continue
            if not resolved.exists():
                errors.append(f"{path.relative_to(ROOT)}: broken link: {raw_target}")


def run_unit_tests() -> int:
    command = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        str(ROOT / "tests" / "unit"),
        "-p",
        "test_*.py",
        "-v",
    ]
    print("+", " ".join(command))
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def main() -> int:
    errors: list[str] = []
    files = text_files()
    check_required_files(errors)
    check_text_hygiene(files, errors)
    check_markdown_links(errors)

    if errors:
        print("Repository checks failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if run_unit_tests() != 0:
        return 1

    print(f"Repository contract passed ({len(files)} text files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
