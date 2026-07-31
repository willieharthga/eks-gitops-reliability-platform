#!/usr/bin/env python3
"""Prove that out-of-band cluster changes are reverted, not merely reported.

A GitOps claim is easy to make and easy to get wrong. An Argo CD Application
can sit at `Synced` while a controller quietly ignores the field someone
edited, and "we use GitOps" then means "we have a dashboard". This script
makes the claim falsifiable: it performs the two kinds of drift an operator
actually causes, and fails if the cluster does not converge back to Git.

**Mutation drift** scales a Deployment by hand. Self-heal must restore the
replica count from the committed manifest.

**Deletion drift** deletes a managed Service. Self-heal must recreate it.

Both cases also assert that the resource carries Argo CD's tracking
annotation, which is what makes ownership unambiguous; a resource Argo CD does
not consider its own would drift silently regardless of the sync policy.

Timings are recorded to `artifacts/drift-test.json` because "it self-heals" is
a weaker statement than "it self-healed in 12 seconds, measured here".
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"

ARGOCD_NAMESPACE = "argocd"
TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"

DEFAULT_CLUSTER = "gitops-dev"
DEFAULT_APPLICATION = "platform-dev"
DEFAULT_NAMESPACE = "platform"
DEFAULT_DEPLOYMENT = "frontend"
DEFAULT_SERVICE = "frontend"

POLL_SECONDS = 2.0


class DriftError(RuntimeError):
    """Raised when the environment is unfit to run the test."""


def log(message: str) -> None:
    print(f"[drift] {message}", flush=True)


def kubectl(
    context: str, *args: str, check: bool = True, timeout: str = "10s"
) -> subprocess.CompletedProcess[str]:
    command = ["kubectl", "--context", f"kind-{context}", *args]
    if not any(argument.startswith("--request-timeout") for argument in args):
        command.append(f"--request-timeout={timeout}")
    result = subprocess.run(
        command, cwd=ROOT, check=False, text=True, capture_output=True
    )
    if check and result.returncode != 0:
        raise DriftError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result


def json_resource(context: str, namespace: str, reference: str) -> dict | None:
    result = kubectl(
        context,
        "get",
        reference,
        "--namespace",
        namespace,
        "--output",
        "json",
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def application_state(context: str, application: str) -> tuple[str, str]:
    document = json_resource(
        context, ARGOCD_NAMESPACE, f"applications.argoproj.io/{application}"
    )
    if document is None:
        raise DriftError(
            f"Application {application} not found in the {context} cluster; "
            "run 'make bootstrap' first"
        )
    status = document.get("status", {})
    return (
        status.get("sync", {}).get("status", "Unknown"),
        status.get("health", {}).get("status", "Unknown"),
    )


def assert_ready(context: str, application: str) -> None:
    """Refuse to measure self-heal from a starting point that is already broken."""
    sync, health = application_state(context, application)
    if sync != "Synced" or health != "Healthy":
        raise DriftError(
            f"Application {application} is sync={sync} health={health}; "
            "the drift test needs a converged starting state. "
            "Run 'make gitops-wait' and retry."
        )
    log(f"precondition: {application} is Synced and Healthy")


def assert_tracked(context: str, namespace: str, reference: str) -> str:
    document = json_resource(context, namespace, reference)
    if document is None:
        raise DriftError(f"{reference} does not exist in namespace {namespace}")
    annotations = document.get("metadata", {}).get("annotations") or {}
    tracking = annotations.get(TRACKING_ANNOTATION)
    if not tracking:
        raise DriftError(
            f"{reference} carries no {TRACKING_ANNOTATION} annotation, so Argo CD "
            "does not consider it managed and would never heal it"
        )
    return tracking


def wait_until(
    description: str, predicate, timeout: float
) -> tuple[bool, float]:
    """Poll until the predicate holds, returning success and elapsed seconds."""
    started = time.monotonic()
    deadline = started + timeout
    while True:
        if predicate():
            return True, time.monotonic() - started
        if time.monotonic() >= deadline:
            log(f"timed out after {timeout:.0f}s waiting for {description}")
            return False, time.monotonic() - started
        time.sleep(POLL_SECONDS)


def deployment_replicas(context: str, namespace: str, name: str) -> int | None:
    document = json_resource(context, namespace, f"deployment/{name}")
    if document is None:
        return None
    return document.get("spec", {}).get("replicas")


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------


def mutation_case(
    context: str, namespace: str, name: str, timeout: float
) -> dict:
    reference = f"deployment/{name}"
    tracking = assert_tracked(context, namespace, reference)
    desired = deployment_replicas(context, namespace, name)
    if desired is None:
        raise DriftError(f"{reference} has no spec.replicas to drift")

    drifted = desired + 2
    log(f"{reference} is tracked by {tracking}")
    log(f"scaling {reference} from {desired} to {drifted} out of band")
    kubectl(
        context,
        "scale",
        reference,
        "--namespace",
        namespace,
        f"--replicas={drifted}",
    )

    observed = deployment_replicas(context, namespace, name)
    if observed != drifted:
        raise DriftError(
            f"expected {reference} to read {drifted} replicas after scaling, "
            f"got {observed}"
        )

    healed, seconds = wait_until(
        f"{reference} to return to {desired} replicas",
        lambda: deployment_replicas(context, namespace, name) == desired,
        timeout,
    )
    if healed:
        log(f"self-heal restored {desired} replicas after {seconds:.1f}s")
    else:
        # The controller did not revert the change, so this script must, or it
        # leaves the cluster in the state it was only supposed to observe.
        log("reverting the injected change by hand")
        kubectl(
            context,
            "scale",
            reference,
            "--namespace",
            namespace,
            f"--replicas={desired}",
            check=False,
        )

    return {
        "case": "mutation",
        "resource": reference,
        "namespace": namespace,
        "trackingId": tracking,
        "desiredReplicas": desired,
        "driftedReplicas": drifted,
        "healed": healed,
        "healSeconds": round(seconds, 1),
    }


def deletion_case(
    context: str, namespace: str, name: str, timeout: float
) -> dict:
    reference = f"service/{name}"
    tracking = assert_tracked(context, namespace, reference)
    before = json_resource(context, namespace, reference)
    assert before is not None  # assert_tracked already proved it exists
    original_uid = before["metadata"]["uid"]

    log(f"deleting {reference} out of band")
    kubectl(context, "delete", reference, "--namespace", namespace)

    def recreated() -> bool:
        document = json_resource(context, namespace, reference)
        # A new UID, not merely presence: a stale read of the object being
        # deleted would otherwise look like a successful heal.
        return document is not None and document["metadata"]["uid"] != original_uid

    healed, seconds = wait_until(f"{reference} to be recreated", recreated, timeout)
    if healed:
        log(f"self-heal recreated {reference} after {seconds:.1f}s")
    else:
        log(f"{reference} was not recreated; the environment is now degraded")

    return {
        "case": "deletion",
        "resource": reference,
        "namespace": namespace,
        "trackingId": tracking,
        "healed": healed,
        "healSeconds": round(seconds, 1),
    }


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def write_evidence(record: dict) -> Path:
    ARTIFACTS.mkdir(exist_ok=True)
    path = ARTIFACTS / "drift-test.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--application", default=DEFAULT_APPLICATION)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--deployment", default=DEFAULT_DEPLOYMENT)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds to allow for each case to converge.",
    )
    args = parser.parse_args(argv)

    if not shutil.which("kubectl"):
        print("[drift] ERROR: kubectl not found on PATH", file=sys.stderr)
        return 1

    started = datetime.now(timezone.utc)
    try:
        assert_ready(args.cluster, args.application)
        cases = [
            mutation_case(
                args.cluster, args.namespace, args.deployment, args.timeout
            ),
            deletion_case(args.cluster, args.namespace, args.service, args.timeout),
        ]
    except DriftError as exc:
        print(f"[drift] ERROR: {exc}", file=sys.stderr)
        return 1

    sync, health = application_state(args.cluster, args.application)
    passed = all(case["healed"] for case in cases)
    record = {
        "startedAt": started.isoformat().replace("+00:00", "Z"),
        "cluster": args.cluster,
        "application": args.application,
        "timeoutSeconds": args.timeout,
        "finalSyncStatus": sync,
        "finalHealthStatus": health,
        "passed": passed,
        "cases": cases,
    }
    path = write_evidence(record)

    log("")
    for case in cases:
        outcome = "healed" if case["healed"] else "DRIFT PERSISTED"
        log(f"{case['case']:<9} {case['resource']:<20} {outcome} "
            f"({case['healSeconds']:.1f}s)")
    log(f"evidence written to {path.relative_to(ROOT).as_posix()}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
