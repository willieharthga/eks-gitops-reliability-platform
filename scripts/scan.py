#!/usr/bin/env python3
"""Vulnerability gate for the built service images.

Policy, stated explicitly because a scanner without a documented policy is
theatre:

* **Fail** on HIGH or CRITICAL findings that have a fix available. These are
  actionable: rebuild on a patched base and they disappear.
* **Report but do not fail** on findings with no upstream fix. Blocking on them
  cannot be resolved by any action this repository can take, and a gate that
  cannot be satisfied is a gate that gets disabled.
* **Fail** if the scanner itself cannot run, rather than passing silently. A
  scan that did not happen must never look like a scan that found nothing.

The unfixed findings are still written to the report so that they are visible
and can be reviewed, rather than being invisible because they were ignored.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "artifacts" / "images.json"
DEFAULT_REPORT = ROOT / "artifacts" / "vulnerability-report.json"
BLOCKING_SEVERITIES = ("HIGH", "CRITICAL")


class ScanError(RuntimeError):
    """Raised when the scan cannot be performed."""


def load_images(manifest: Path) -> list[dict[str, Any]]:
    if not manifest.is_file():
        raise ScanError(
            f"{manifest.relative_to(ROOT)} not found; run 'make images' first"
        )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    images = data.get("images") or []
    if not images:
        raise ScanError("the image manifest lists no images")
    return images


def scan_image(reference: str, *, ignore_unfixed: bool) -> dict[str, Any]:
    command = [
        "trivy",
        "image",
        "--quiet",
        "--format",
        "json",
        "--severity",
        ",".join(BLOCKING_SEVERITIES),
        "--scanners",
        "vuln",
    ]
    if ignore_unfixed:
        command.append("--ignore-unfixed")
    command.append(reference)

    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE)
    if result.returncode != 0:
        raise ScanError(f"trivy failed for {reference} (exit {result.returncode})")
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ScanError(f"trivy returned output that is not JSON: {exc}") from exc


def collect_findings(report: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for target in report.get("Results") or []:
        for vulnerability in target.get("Vulnerabilities") or []:
            findings.append(
                {
                    "target": str(target.get("Target", "")),
                    "id": str(vulnerability.get("VulnerabilityID", "")),
                    "package": str(vulnerability.get("PkgName", "")),
                    "installed": str(vulnerability.get("InstalledVersion", "")),
                    "fixed": str(vulnerability.get("FixedVersion", "")),
                    "severity": str(vulnerability.get("Severity", "")),
                }
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--include-unfixed",
        action="store_true",
        help="Also fail on findings that have no upstream fix.",
    )
    args = parser.parse_args(argv)

    try:
        if not shutil.which("trivy"):
            raise ScanError(
                "trivy is not installed; install it or run the scan in CI. "
                "The gate fails closed so a missing scanner cannot be mistaken "
                "for a clean result."
            )

        images = load_images(args.manifest)
        blocking: list[dict[str, str]] = []
        informational: list[dict[str, str]] = []

        for image in images:
            reference = image.get("pinned_reference") or image["reference"]
            blocking_report = scan_image(reference, ignore_unfixed=not args.include_unfixed)
            for finding in collect_findings(blocking_report):
                finding["image"] = image["service"]
                blocking.append(finding)

            if not args.include_unfixed:
                full_report = scan_image(reference, ignore_unfixed=False)
                known = {finding["id"] for finding in blocking}
                for finding in collect_findings(full_report):
                    if finding["id"] not in known:
                        finding["image"] = image["service"]
                        informational.append(finding)

        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {
                    "policy": {
                        "blocking_severities": list(BLOCKING_SEVERITIES),
                        "unfixed_are_blocking": bool(args.include_unfixed),
                    },
                    "images": [image["service"] for image in images],
                    "blocking": blocking,
                    "informational_unfixed": informational,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        print(f"\nreport written to {args.report.relative_to(ROOT)}")
        print(f"unfixed findings recorded for review: {len(informational)}")

        if blocking:
            print(
                f"\nBLOCKED: {len(blocking)} fixable "
                f"{'/'.join(BLOCKING_SEVERITIES)} finding(s):",
                file=sys.stderr,
            )
            for finding in blocking:
                print(
                    f"- [{finding['severity']}] {finding['image']}: {finding['id']} "
                    f"{finding['package']} {finding['installed']} "
                    f"-> fixed in {finding['fixed']}",
                    file=sys.stderr,
                )
            return 1

        print("no fixable HIGH or CRITICAL vulnerabilities found")
    except ScanError as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
