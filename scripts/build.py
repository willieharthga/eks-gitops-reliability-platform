#!/usr/bin/env python3
"""Build the service images and record their immutable digests.

CI and a workstation run exactly the same code path here. A separate
CI-only build definition is how "it works locally" and "it works in the
pipeline" quietly stop meaning the same thing.

The script never edits environment configuration. Producing an artifact and
promoting an artifact are different actions with different approval
requirements; see docs/adr/0003-immutable-artifact-promotion.md.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "apps" / "services.json"
DOCKERFILE = ROOT / "apps" / "Dockerfile"
DEFAULT_OUTPUT = ROOT / "artifacts" / "images.json"


class BuildError(RuntimeError):
    """Raised when the build cannot proceed or a build step failed."""


@dataclass(frozen=True)
class Service:
    name: str
    directory: str
    package: str
    role: str
    port: int

    @property
    def source_paths(self) -> tuple[Path, ...]:
        return (
            ROOT / "apps" / "shared" / "platform_common",
            ROOT / "apps" / self.directory / self.package,
        )


def load_services() -> list[Service]:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    return [
        Service(
            name=entry["name"],
            directory=entry["directory"],
            package=entry["package"],
            role=entry["role"],
            port=int(entry["port"]),
        )
        for entry in data["services"]
    ]


def load_registry(kind: str) -> str:
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    return str(data["registry"][kind])


def run(command: list[str], *, capture: bool = False) -> str:
    print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command, cwd=ROOT, check=False, text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    if result.returncode != 0:
        raise BuildError(f"command failed with exit code {result.returncode}")
    return result.stdout or ""


def git_revision() -> str:
    if not shutil.which("git"):
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT, check=False, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return result.stdout.strip() or "unknown"


def working_tree_is_clean() -> bool:
    if not shutil.which("git"):
        return False
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT, check=False, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0 and not result.stdout.strip()


def resolve_version(explicit: str | None, revision: str) -> str:
    if explicit:
        return explicit
    stamp = datetime.now(UTC).strftime("%Y.%m.%d")
    short = revision[:12] if revision != "unknown" else "unknown"
    return f"0.0.0-{stamp}+{short}"


def require_tools(push: bool) -> None:
    if not shutil.which("docker"):
        raise BuildError(
            "docker is required to build images; install Docker or run "
            "'make verify' for the credential-free checks"
        )
    probe = subprocess.run(
        ["docker", "buildx", "version"],
        cwd=ROOT, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        raise BuildError("docker buildx is required for SBOM and provenance attestations")
    if push:
        print("note: --push requires the target registry to already be reachable")


def build_service(
    service: Service,
    *,
    registry: str,
    version: str,
    revision: str,
    created: str,
    push: bool,
    attest: bool,
    metadata_dir: Path,
) -> dict[str, Any]:
    reference = f"{registry}/{service.name}:{version}"
    metadata_file = metadata_dir / f"{service.name}.json"

    command = [
        "docker", "buildx", "build",
        "--file", str(DOCKERFILE),
        "--build-arg", f"SERVICE_DIR={service.directory}",
        "--build-arg", f"SERVICE_PACKAGE={service.package}",
        "--build-arg", f"SERVICE_NAME={service.name}",
        "--build-arg", f"SERVICE_VERSION={version}",
        "--build-arg", f"SERVICE_REVISION={revision}",
        "--build-arg", f"BUILD_CREATED={created}",
        "--tag", reference,
        "--metadata-file", str(metadata_file),
    ]
    if attest:
        # mode=max records the full build definition, which is what makes the
        # provenance useful for answering "what produced this digest".
        command += ["--provenance=mode=max", "--sbom=true"]
    command += ["--push"] if push else ["--load"]
    command.append(str(ROOT))

    run(command)

    digest = ""
    if metadata_file.is_file():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        digest = str(metadata.get("containerimage.digest", ""))

    if not digest and not push:
        # buildx --load does not always publish a manifest digest; fall back to
        # the local image id so the record is never silently empty.
        digest = run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            capture=True,
        ).strip()

    if not digest:
        raise BuildError(f"could not determine an image digest for {service.name}")

    return {
        "service": service.name,
        "role": service.role,
        "port": service.port,
        "repository": f"{registry}/{service.name}",
        "tag": version,
        "reference": reference,
        "digest": digest,
        "pinned_reference": f"{registry}/{service.name}@{digest}",
        "attested": attest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        default=None,
        help="Target registry; defaults to the local registry in apps/services.json.",
    )
    parser.add_argument("--version", default=None, help="Explicit artifact version.")
    parser.add_argument(
        "--service", action="append", default=None,
        help="Build only the named service; repeatable.",
    )
    parser.add_argument("--push", action="store_true", help="Push instead of loading locally.")
    parser.add_argument(
        "--no-attest", action="store_true",
        help="Skip SBOM and provenance attestations (they require a registry).",
    )
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help="Where to write the digest manifest.",
    )
    args = parser.parse_args(argv)

    try:
        services = load_services()
        if args.service:
            wanted = set(args.service)
            unknown = wanted - {service.name for service in services}
            if unknown:
                raise BuildError(f"unknown service(s): {sorted(unknown)}")
            services = [service for service in services if service.name in wanted]

        registry = args.registry or load_registry("local")
        revision = git_revision()
        version = resolve_version(args.version, revision)
        created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        clean = working_tree_is_clean()

        require_tools(args.push)

        if not clean:
            print(
                "warning: the working tree has uncommitted changes; this artifact "
                "is not reproducible from a reviewed revision",
                file=sys.stderr,
            )

        args.output.parent.mkdir(parents=True, exist_ok=True)
        metadata_dir = args.output.parent / "buildx"
        metadata_dir.mkdir(parents=True, exist_ok=True)

        records = [
            build_service(
                service,
                registry=registry,
                version=version,
                revision=revision,
                created=created,
                push=args.push,
                attest=not args.no_attest and args.push,
                metadata_dir=metadata_dir,
            )
            for service in services
        ]

        manifest = {
            "version": version,
            "revision": revision,
            "created": created,
            "registry": registry,
            "source_clean": clean,
            "pushed": args.push,
            "images": records,
        }
        args.output.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.output.relative_to(ROOT)}")
        for record in records:
            print(f"  {record['service']:<16} {record['digest']}")
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
