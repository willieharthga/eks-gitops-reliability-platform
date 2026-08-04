#!/usr/bin/env python3
"""Promote and roll back immutable image digests through Git.

Producing an artifact and putting it into an environment are different actions
with different approval requirements. This script only edits tracked overlay
files. It never talks to a cluster: Argo CD reconciles the commit after it is
reviewed and merged. See docs/adr/0003-immutable-artifact-promotion.md.

Four rules are enforced here rather than in a later review:

1. A promoted pin is a digest, never a mutable tag.
2. Development may be promoted from a build manifest.
3. Production may only copy pins that already exist in another environment, and
   only when the operator types the environment name as confirmation.
4. Rollback restores a previously recorded digest set; it does not rebuild.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "apps" / "services.json"
ENVIRONMENTS = ROOT / "environments"
DEFAULT_MANIFEST = ROOT / "artifacts" / "images.json"
PRODUCTION = "production"
BOOTSTRAP_TAG = "0.0.0-bootstrap"
CONFIRM_TRAILER = "PROMOTION-CONFIRM: production"
HISTORY_LIMIT = 20
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGES_BLOCK = re.compile(r"^images:\n(?:^[ \t].*\n)*", re.MULTILINE)

# CI and promotion automation must not reach a cluster. The contract tests
# scan this tuple against workflow files and this script.
FORBIDDEN_CLUSTER_VERBS = (
    "kubectl apply",
    "kubectl set",
    "kubectl rollout",
    "kubectl patch",
    "kubectl create",
    "kubectl replace",
    "helm upgrade",
    "helm install",
    "argocd app sync",
    "argocd app set",
)


class PromoteError(RuntimeError):
    """Raised when a promotion or rollback cannot proceed safely."""


@dataclass(frozen=True)
class Pin:
    service: str
    name: str
    digest: str = ""
    tag: str = ""

    @property
    def is_digest(self) -> bool:
        return bool(self.digest)

    @property
    def is_bootstrap(self) -> bool:
        return not self.digest and self.tag == BOOTSTRAP_TAG

    def as_history(self) -> dict[str, str]:
        record = {"name": self.name}
        if self.digest:
            record["digest"] = self.digest
        if self.tag:
            record["tag"] = self.tag
        return record


def load_catalog() -> dict[str, Any]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def service_names() -> list[str]:
    return [entry["name"] for entry in load_catalog()["services"]]


def in_cluster_registry() -> str:
    return str(load_catalog()["registry"]["local_in_cluster"])


def overlay_path(environment: str) -> Path:
    path = ENVIRONMENTS / environment / "kustomization.yaml"
    if not path.is_file():
        raise PromoteError(f"unknown environment {environment!r}: {path} does not exist")
    return path


def history_path(environment: str) -> Path:
    overlay_path(environment)
    return ENVIRONMENTS / environment / "promotion-history.json"


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_images_block(text: str) -> list[dict[str, str]]:
    """Read the overlay's images list without requiring a YAML library."""
    in_images = False
    current: dict[str, str] = {}
    images: list[dict[str, str]] = []
    for line in text.splitlines():
        if line.startswith("images:"):
            in_images = True
            continue
        if not in_images:
            continue
        if line and not line[0].isspace():
            break
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- name:"):
            if current:
                images.append(current)
            current = {"name": stripped.split(":", 1)[1].strip()}
        elif stripped.startswith("digest:"):
            current["digest"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("newTag:"):
            current["newTag"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("newName:"):
            current["newName"] = stripped.split(":", 1)[1].strip()
    if current:
        images.append(current)
    return images


def pins_from_overlay_text(text: str) -> list[Pin]:
    pins = []
    registry = in_cluster_registry()
    known = {f"{registry}/{name}": name for name in service_names()}
    for image in parse_images_block(text):
        name = image.get("name", "")
        service = known.get(name)
        if service is None:
            raise PromoteError(
                f"overlay pins {name or '<missing>'}, which is not a catalogued "
                f"in-cluster image ({', '.join(sorted(known))})"
            )
        pins.append(
            Pin(
                service=service,
                name=name,
                digest=image.get("digest", ""),
                tag=image.get("newTag", ""),
            )
        )
    return pins


def pins_from_overlay(environment: str) -> list[Pin]:
    return pins_from_overlay_text(overlay_path(environment).read_text(encoding="utf-8"))


def require_complete(pins: list[Pin], *, context: str) -> None:
    present = {pin.service for pin in pins}
    missing = [name for name in service_names() if name not in present]
    if missing:
        raise PromoteError(f"{context} is missing pin(s) for: {', '.join(missing)}")
    extra = present - set(service_names())
    if extra:
        raise PromoteError(f"{context} contains unknown service(s): {', '.join(sorted(extra))}")


def require_digests(pins: list[Pin], *, context: str) -> None:
    for pin in pins:
        if not pin.digest:
            raise PromoteError(
                f"{context}: {pin.service} is not digest-pinned "
                f"(tag {pin.tag or '<none>'}). A promoted release must use a digest."
            )
        if not DIGEST_PATTERN.match(pin.digest):
            raise PromoteError(
                f"{context}: {pin.service} digest {pin.digest!r} is not a sha256 digest"
            )


def require_homogeneous(pins: list[Pin], *, context: str) -> None:
    """A mix of bootstrap tags and digests is not a defined environment state."""
    digested = [pin for pin in pins if pin.is_digest]
    tagged = [pin for pin in pins if not pin.is_digest]
    if digested and tagged:
        raise PromoteError(
            f"{context} mixes digest pins ({', '.join(p.service for p in digested)}) "
            f"with tags ({', '.join(p.service for p in tagged)})"
        )


def in_cluster_name(repository: str, service: str) -> str:
    """Rewrite a build-time repository to the in-cluster registry name.

    CI and workstations push to localhost; clusters pull from
    gitops-registry:5000. The digest is the identity; the name is a location.
    """
    trailing = repository.rsplit("/", 1)[-1]
    if trailing != service:
        raise PromoteError(
            f"manifest repository {repository!r} does not end with service {service!r}"
        )
    return f"{in_cluster_registry()}/{service}"


def pins_from_manifest(path: Path) -> tuple[list[Pin], dict[str, Any]]:
    if not path.is_file():
        raise PromoteError(f"digest manifest not found: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromoteError(f"{path} is not valid JSON: {exc}") from exc
    records = manifest.get("images")
    if not isinstance(records, list) or not records:
        raise PromoteError(f"{path} does not contain an images list")
    pins: list[Pin] = []
    seen: set[str] = set()
    for record in records:
        service = str(record.get("service", ""))
        digest = str(record.get("digest", ""))
        repository = str(record.get("repository", ""))
        tag = str(record.get("tag", ""))
        if not service:
            raise PromoteError(f"{path} contains an image record without a service name")
        if service in seen:
            raise PromoteError(f"{path} lists {service} more than once")
        seen.add(service)
        if not digest:
            raise PromoteError(
                f"{path}: {service} has no digest. Refusing to promote a tag-only record."
            )
        pins.append(
            Pin(
                service=service,
                name=in_cluster_name(repository, service),
                digest=digest,
                tag=tag,
            )
        )
    require_complete(pins, context=str(path))
    require_digests(pins, context=str(path))
    return pins, manifest


def render_images_block(pins: list[Pin]) -> str:
    ordered = {pin.service: pin for pin in pins}
    lines = ["images:"]
    for service in service_names():
        pin = ordered[service]
        lines.append(f"  - name: {pin.name}")
        if pin.digest:
            lines.append(f"    digest: {pin.digest}")
        else:
            lines.append(f"    newTag: {pin.tag or BOOTSTRAP_TAG}")
    return "\n".join(lines) + "\n"


def replace_images_block(text: str, pins: list[Pin]) -> str:
    replacement = render_images_block(pins)
    updated, count = IMAGES_BLOCK.subn(replacement, text, count=1)
    if count != 1:
        raise PromoteError("overlay does not contain exactly one top-level images: block")
    return updated


def load_history(environment: str) -> dict[str, Any]:
    path = history_path(environment)
    if not path.is_file():
        return {"environment": environment, "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromoteError(f"{path} is not valid JSON: {exc}") from exc
    if data.get("environment") not in (None, environment):
        raise PromoteError(
            f"{path} belongs to environment {data.get('environment')!r}, not {environment!r}"
        )
    data.setdefault("environment", environment)
    data.setdefault("entries", [])
    if not isinstance(data["entries"], list):
        raise PromoteError(f"{path} entries must be a list")
    return data


def write_history(environment: str, history: dict[str, Any]) -> None:
    history["environment"] = environment
    history["entries"] = history["entries"][-HISTORY_LIMIT:]
    path = history_path(environment)
    path.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def pins_equal(left: list[Pin], right: list[Pin]) -> bool:
    def key(pins: list[Pin]) -> dict[str, tuple[str, str]]:
        return {pin.service: (pin.digest, pin.tag) for pin in pins}

    return key(left) == key(right)


def append_history(
    environment: str,
    pins: list[Pin],
    *,
    action: str,
    source: str,
    source_environment: str | None,
    revision: str,
    created: str,
) -> None:
    history = load_history(environment)
    entries = history["entries"]
    record = {
        "action": action,
        "created": created,
        "source": source,
        "source_environment": source_environment,
        "source_revision": revision,
        "images": {pin.service: pin.as_history() for pin in pins},
    }
    if entries:
        previous = entries[-1].get("images", {})
        if previous == record["images"] and entries[-1].get("action") == action:
            return
    entries.append(record)
    write_history(environment, history)


def write_overlay(environment: str, pins: list[Pin]) -> None:
    path = overlay_path(environment)
    text = path.read_text(encoding="utf-8")
    path.write_text(replace_images_block(text, pins), encoding="utf-8")


def apply_pins(
    environment: str,
    pins: list[Pin],
    *,
    action: str,
    source: str,
    source_environment: str | None,
    revision: str,
) -> str:
    require_complete(pins, context=action)
    require_homogeneous(pins, context=action)
    if action != "bootstrap":
        require_digests(pins, context=action)
    current = pins_from_overlay(environment)
    require_complete(current, context=f"{environment} overlay")
    require_homogeneous(current, context=f"{environment} overlay")
    if pins_equal(current, pins):
        return "unchanged"
    created = utcnow()
    write_overlay(environment, pins)
    append_history(
        environment,
        pins,
        action=action,
        source=source,
        source_environment=source_environment,
        revision=revision,
        created=created,
    )
    return "updated"


def cmd_apply(args: argparse.Namespace) -> None:
    environment = args.environment
    if environment == PRODUCTION:
        if args.from_manifest:
            raise PromoteError(
                "production cannot be promoted from a build manifest. Promote "
                "the same digests already running in another environment with "
                "--from-environment, then type --confirm production."
            )
        if args.confirm != PRODUCTION:
            raise PromoteError(
                "production promotion requires --confirm production. This is "
                "the local stand-in for a required review; CI also requires "
                f"{CONFIRM_TRAILER} in the pull request body."
            )
        if not args.from_environment:
            raise PromoteError("production promotion requires --from-environment")
        if args.from_environment == PRODUCTION:
            raise PromoteError("production cannot be promoted from itself")
        source_pins = pins_from_overlay(args.from_environment)
        require_complete(source_pins, context=args.from_environment)
        require_digests(
            source_pins,
            context=f"{args.from_environment} (production cannot copy bootstrap tags)",
        )
        result = apply_pins(
            environment,
            source_pins,
            action="promote",
            source=f"environment:{args.from_environment}",
            source_environment=args.from_environment,
            revision="",
        )
    else:
        if args.from_environment:
            source_pins = pins_from_overlay(args.from_environment)
            require_complete(source_pins, context=args.from_environment)
            require_digests(source_pins, context=args.from_environment)
            result = apply_pins(
                environment,
                source_pins,
                action="promote",
                source=f"environment:{args.from_environment}",
                source_environment=args.from_environment,
                revision="",
            )
        else:
            manifest_path = Path(args.from_manifest) if args.from_manifest else DEFAULT_MANIFEST
            pins, manifest = pins_from_manifest(manifest_path)
            result = apply_pins(
                environment,
                pins,
                action="promote",
                source=str(manifest_path.as_posix()),
                source_environment=None,
                revision=str(manifest.get("revision", "")),
            )
    print(f"{environment}: {result}")
    for pin in pins_from_overlay(environment):
        identity = pin.digest or pin.tag
        print(f"  {pin.service:<16} {identity}")


def cmd_rollback(args: argparse.Namespace) -> None:
    environment = args.environment
    if environment == PRODUCTION and args.confirm != PRODUCTION:
        raise PromoteError("production rollback requires --confirm production")
    history = load_history(environment)
    entries = history["entries"]
    if len(entries) < 2:
        raise PromoteError(
            f"{environment} has no previous promotion to restore. "
            "Rollback needs at least two history entries."
        )
    previous = entries[-2]
    images = previous.get("images", {})
    pins = []
    for service in service_names():
        record = images.get(service)
        if not isinstance(record, dict):
            raise PromoteError(
                f"{environment} history is missing {service} in the previous entry"
            )
        pins.append(
            Pin(
                service=service,
                name=str(record.get("name", f"{in_cluster_registry()}/{service}")),
                digest=str(record.get("digest", "")),
                tag=str(record.get("tag", "")),
            )
        )
    require_digests(pins, context=f"{environment} rollback target")
    result = apply_pins(
        environment,
        pins,
        action="rollback",
        source=f"history:{previous.get('created', 'unknown')}",
        source_environment=previous.get("source_environment"),
        revision=str(previous.get("source_revision", "")),
    )
    print(f"{environment}: rollback {result}")
    for pin in pins:
        print(f"  {pin.service:<16} {pin.digest}")


def cmd_status(args: argparse.Namespace) -> None:
    environment = args.environment
    pins = pins_from_overlay(environment)
    require_complete(pins, context=environment)
    require_homogeneous(pins, context=environment)
    kind = "digest" if pins[0].is_digest else "bootstrap"
    history = load_history(environment)
    print(f"environment: {environment}")
    print(f"pin_kind:    {kind}")
    print(f"history:     {len(history['entries'])} entr{'y' if len(history['entries']) == 1 else 'ies'}")
    for pin in pins:
        print(f"  {pin.service:<16} {pin.digest or pin.tag}")


def overlay_from_git(environment: str, ref: str) -> str:
    """Return overlay text at ref using git show; empty if the file did not exist."""
    import subprocess

    relative = overlay_path(environment).relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f"{ref}:{relative}"],
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def cmd_gate(args: argparse.Namespace) -> None:
    """Fail if production pins changed without an explicit confirmation trailer.

    This is the CI-enforceable half of the production approval boundary. Branch
    protection still has to require a review; this check stops an unnoticed
    digest swap from merging just because the rest of the PR looked harmless.
    """
    body = args.confirm_text or ""
    if args.base_overlay and args.head_overlay:
        base_text = Path(args.base_overlay).read_text(encoding="utf-8")
        head_text = Path(args.head_overlay).read_text(encoding="utf-8")
    else:
        base_text = overlay_from_git(PRODUCTION, args.base)
        head_text = overlay_from_git(PRODUCTION, args.head) or overlay_path(PRODUCTION).read_text(
            encoding="utf-8"
        )
    if not base_text:
        base_pins: list[Pin] = []
    else:
        base_pins = pins_from_overlay_text(base_text)
    head_pins = pins_from_overlay_text(head_text)
    require_complete(head_pins, context="production (head)")
    require_homogeneous(head_pins, context="production (head)")
    if pins_equal(base_pins, head_pins):
        print("production image pins unchanged")
        return
    if all(pin.is_bootstrap for pin in head_pins) and all(
        pin.is_bootstrap for pin in base_pins
    ):
        print("production image pins unchanged (bootstrap)")
        return
    require_digests(head_pins, context="production (head)")
    if CONFIRM_TRAILER.lower() not in body.lower():
        raise PromoteError(
            "production image pins changed, but the pull request body does not "
            f"contain {CONFIRM_TRAILER!r}. Promotion of production is a reviewed "
            "configuration change, not an implicit side effect."
        )
    print("production promotion confirmation found")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    apply_p = sub.add_parser("apply", help="Write digest pins into an environment overlay.")
    apply_p.add_argument("--environment", required=True, help="Target overlay name.")
    apply_p.add_argument(
        "--from",
        dest="from_manifest",
        default=None,
        help="Digest manifest produced by scripts/build.py.",
    )
    apply_p.add_argument(
        "--from-environment",
        default=None,
        help="Copy pins from another overlay (required for production).",
    )
    apply_p.add_argument(
        "--confirm",
        default="",
        help="Must equal 'production' when targeting production.",
    )
    apply_p.set_defaults(func=cmd_apply)

    rollback_p = sub.add_parser("rollback", help="Restore the previous recorded digest set.")
    rollback_p.add_argument("--environment", required=True)
    rollback_p.add_argument("--confirm", default="")
    rollback_p.set_defaults(func=cmd_rollback)

    status_p = sub.add_parser("status", help="Show the current pins without changing them.")
    status_p.add_argument("--environment", required=True)
    status_p.set_defaults(func=cmd_status)

    gate_p = sub.add_parser(
        "gate",
        help="Require a confirmation trailer when production pins change.",
    )
    gate_p.add_argument("--base", default="origin/main", help="Git ref for the merge base.")
    gate_p.add_argument("--head", default="HEAD", help="Git ref for the proposed change.")
    gate_p.add_argument(
        "--confirm-text",
        default="",
        help="Pull request body that must contain the confirmation trailer.",
    )
    gate_p.add_argument(
        "--base-overlay",
        default="",
        help="Optional file holding the base overlay (used by tests).",
    )
    gate_p.add_argument(
        "--head-overlay",
        default="",
        help="Optional file holding the head overlay (used by tests).",
    )
    gate_p.set_defaults(func=cmd_gate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except PromoteError as exc:
        print(f"promote failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
