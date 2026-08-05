#!/usr/bin/env python3
"""Prove a faulty canary aborts before taking full traffic.

This is the M5 acceptance test. It is an operator experiment, like the drift
test, not a pipeline deploy: it pauses auto-sync, raises canary-only faults,
triggers a new revision, and requires the Rollout to Abort with canary weight
below 100% while a stable ReplicaSet remains.

CI does not run this. `make verify` covers the specification; this script
needs a bootstrapped cluster and records `artifacts/canary-test.json`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"

DEFAULT_CLUSTER = "gitops-dev"
DEFAULT_APPLICATION = "platform-dev"
DEFAULT_NAMESPACE = "platform"
DEFAULT_ROLLOUT = "api"
ARGOCD_NAMESPACE = "argocd"


class CanaryError(RuntimeError):
    """Raised when the environment is unfit or the abort did not happen."""


def log(message: str) -> None:
    print(f"[canary] {message}", flush=True)


def kubectl(context: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = [
        "kubectl",
        "--context",
        f"kind-{context}",
        *args,
        "--request-timeout=15s",
    ]
    result = subprocess.run(command, cwd=ROOT, check=False, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise CanaryError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr.strip()}"
        )
    return result


def json_get(context: str, namespace: str, reference: str) -> dict:
    result = kubectl(
        context,
        "get",
        reference,
        "--namespace",
        namespace,
        "--output",
        "json",
    )
    return json.loads(result.stdout)


def pause_autosync(context: str, application: str) -> None:
    log(f"pausing auto-sync on {application} so the experiment is not reverted mid-analysis")
    kubectl(
        context,
        "patch",
        f"application/{application}",
        "--namespace",
        ARGOCD_NAMESPACE,
        "--type",
        "merge",
        "--patch",
        '{"spec":{"syncPolicy":{"automated":null}}}',
    )


def restore_autosync(context: str, application: str) -> None:
    log(f"restoring auto-sync on {application}")
    kubectl(
        context,
        "patch",
        f"application/{application}",
        "--namespace",
        ARGOCD_NAMESPACE,
        "--type",
        "merge",
        "--patch",
        '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}',
        check=False,
    )


def set_canary_fault(context: str, namespace: str, ratio: str) -> None:
    log(f"setting CANARY_FAULT_ERROR_RATIO={ratio}")
    kubectl(
        context,
        "set",
        "env",
        "configmap/platform-environment",
        f"CANARY_FAULT_ERROR_RATIO={ratio}",
        "--namespace",
        namespace,
    )


def bump_rollout(context: str, namespace: str, rollout: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log(f"triggering a new canary revision ({stamp})")
    kubectl(
        context,
        "patch",
        f"rollout/{rollout}",
        "--namespace",
        namespace,
        "--type",
        "merge",
        "--patch",
        json.dumps(
            {
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "platform.gitops-reliability.io/canary-probe": stamp
                            }
                        }
                    }
                }
            }
        ),
    )


def rollout_status(context: str, namespace: str, rollout: str) -> dict:
    resource = json_get(context, namespace, f"rollout/{rollout}")
    status = resource.get("status", {})
    weights = status.get("canary", {})
    return {
        "phase": status.get("phase", "Unknown"),
        "message": status.get("message", ""),
        "available_replicas": status.get("availableReplicas", 0),
        "canary_weight": weights.get("weights", {}).get("canary", {}).get("weight", 0)
        if isinstance(weights.get("weights"), dict)
        else status.get("canary", {}).get("weights", 0) or 0,
        "stable_replicas": status.get("stableRS", ""),
    }


def wait_for_abort(context: str, namespace: str, rollout: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = rollout_status(context, namespace, rollout)
        log(f"phase={last['phase']} weight={last['canary_weight']} {last['message'][:80]}")
        phase = str(last["phase"]).lower()
        if phase in {"degraded", "abort", "aborted"}:
            return last
        if "abort" in str(last["message"]).lower():
            return last
        time.sleep(5)
    raise CanaryError(f"rollout did not abort within {timeout:.0f}s; last status {last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--application", default=DEFAULT_APPLICATION)
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument("--rollout", default=DEFAULT_ROLLOUT)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    started = time.monotonic()
    paused = False
    record: dict = {
        "passed": False,
        "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cluster": args.cluster,
        "rollout": args.rollout,
    }
    try:
        pause_autosync(args.cluster, args.application)
        paused = True
        set_canary_fault(args.cluster, args.namespace, "1")
        bump_rollout(args.cluster, args.namespace, args.rollout)
        status = wait_for_abort(args.cluster, args.namespace, args.rollout, args.timeout)
        weight = int(status.get("canary_weight") or 0)
        if weight >= 100:
            raise CanaryError(f"canary reached full traffic (weight={weight}) before abort")
        if int(status.get("available_replicas") or 0) < 1:
            raise CanaryError("stable version was not available after abort")
        record.update({"passed": True, "status": status, "canary_weight": weight})
        log("canary aborted without taking full traffic")
        return 0
    except CanaryError as exc:
        record["error"] = str(exc)
        print(f"canary test failed: {exc}", file=sys.stderr)
        return 1
    finally:
        set_canary_fault(args.cluster, args.namespace, "0")
        if paused:
            restore_autosync(args.cluster, args.application)
        record["duration_seconds"] = round(time.monotonic() - started, 3)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        path = ARTIFACTS / "canary-test.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        log(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    raise SystemExit(main())
