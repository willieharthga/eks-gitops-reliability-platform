#!/usr/bin/env python3
"""Argo CD bootstrap and inspection for the local platform.

Three ideas shape this script.

**Configuration is a commit, not a flag.** Argo CD reconciles what is in Git.
If the bootstrap injected a repository URL at apply time, the committed
manifests would still hold the placeholder and Argo CD would immediately revert
to it. `configure` therefore edits the tracked files so the operator commits the
real URL; `bootstrap` refuses to run while a placeholder remains.

**Installing the controller is not a GitOps action.** The Argo CD installation
is applied once by an operator and is not reconciled by Argo CD itself, because
an installation that manages its own controller can delete the controller that
would have rolled a bad upgrade back.

**Cluster credentials are generated, never committed.** The production cluster
is registered by reading the local kubeconfig at bootstrap time and creating the
Argo CD cluster Secret in-cluster. Nothing derived from it is written to disk in
the repository.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PLACEHOLDER = "REPLACE_ME_REPO_URL"
CONTROL_PLANE_CLUSTER = "gitops-dev"
MANAGED_CLUSTER = "gitops-prod"
ARGOCD_NAMESPACE = "argocd"
OWNER = "eks-gitops-reliability-platform"

INSTALL_PATH = ROOT / "platform" / "argocd" / "install"
ROLLOUTS_INSTALL = ROOT / "platform" / "rollouts" / "install"
ROOT_PATH = ROOT / "platform" / "argocd" / "root"
PROJECTS_PATH = ROOT_PATH / "projects"
ROOT_APPLICATION = ROOT_PATH / "root-application.yaml"

# Every tracked file that may legitimately contain the repository URL.
CONFIGURABLE_FILES = (
    Path("platform/argocd/root/root-application.yaml"),
    Path("platform/argocd/root/workloads-applicationset.yaml"),
    Path("platform/argocd/root/projects/platform.yaml"),
    Path("platform/argocd/root/projects/workloads.yaml"),
)

ARGOCD_DEPLOYMENTS = (
    "argocd-repo-server",
    "argocd-server",
    "argocd-applicationset-controller",
)
ARGOCD_STATEFULSETS = ("argocd-application-controller",)

REPO_URL_PATTERN = re.compile(r"^(?:https://|git@|ssh://)\S+$")


class GitOpsError(RuntimeError):
    """Raised when an action cannot be completed safely."""


def log(message: str) -> None:
    print(f"[gitops] {message}", flush=True)


def run(
    command: list[str], *, capture: bool = False, check: bool = True, quiet: bool = False
) -> str:
    if not quiet:
        print("+", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise GitOpsError(
            f"command failed ({result.returncode}): {' '.join(command)}"
            + (f"\n{detail}" if detail else "")
        )
    return result.stdout or ""


def require(*tools: str) -> None:
    missing = [tool for tool in tools if not shutil.which(tool)]
    if missing:
        raise GitOpsError(f"required tool(s) not found on PATH: {', '.join(missing)}")


def context_for(cluster: str) -> str:
    return f"kind-{cluster}"


def kubectl(cluster: str, *args: str, capture: bool = False, check: bool = True) -> str:
    return run(
        ["kubectl", "--context", context_for(cluster), *args],
        capture=capture,
        check=check,
    )


# --------------------------------------------------------------------------
# configure
# --------------------------------------------------------------------------


def files_with_placeholder() -> list[Path]:
    return [
        relative
        for relative in CONFIGURABLE_FILES
        if PLACEHOLDER in (ROOT / relative).read_text(encoding="utf-8")
    ]


def current_repo_url() -> str | None:
    """Return the configured URL from the root Application, if it is set.

    The root Application is the single authority: every other manifest is kept
    consistent with it by `configure`, and `verify` fails if they diverge.
    """
    text = ROOT_APPLICATION.read_text(encoding="utf-8")
    match = re.search(r"^\s*repoURL:\s*(\S+)\s*$", text, re.MULTILINE)
    if match is None:
        raise GitOpsError(f"no repoURL found in {ROOT_APPLICATION.name}")
    value = match.group(1)
    return None if value == PLACEHOLDER else value


def configure(repo_url: str) -> int:
    if not REPO_URL_PATTERN.match(repo_url):
        raise GitOpsError(
            f"{repo_url!r} does not look like a Git URL "
            "(expected https://, ssh:// or git@)"
        )

    # Replacing the previous value as well makes re-pointing the repository a
    # supported operation rather than a manual edit of four files.
    previous = current_repo_url()
    targets = [PLACEHOLDER] if previous is None else [PLACEHOLDER, previous]

    changed: list[Path] = []
    for relative in CONFIGURABLE_FILES:
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        updated = text
        for target in targets:
            updated = updated.replace(target, repo_url)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(relative)

    if not changed:
        log(f"repository URL is already {repo_url}")
        return 0

    for relative in changed:
        log(f"updated {relative.as_posix()}")
    log("")
    log("Commit these files before bootstrapping. Argo CD reconciles the")
    log("committed state, so an uncommitted URL would be reverted.")
    return 0


def assert_configured() -> str:
    url = current_repo_url()
    outstanding = files_with_placeholder()
    if url is None or outstanding:
        listing = ", ".join(path.as_posix() for path in outstanding)
        raise GitOpsError(
            "the repository URL is still a placeholder in: "
            f"{listing or ROOT_APPLICATION.name}\n"
            "Run\n"
            "    make configure REPO_URL=https://github.com/<you>/"
            "eks-gitops-reliability-platform.git\n"
            "and commit the result before bootstrapping."
        )
    return url


# --------------------------------------------------------------------------
# bootstrap
# --------------------------------------------------------------------------


def cluster_reachable(cluster: str) -> bool:
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            context_for(cluster),
            "get",
            "--raw=/readyz",
            "--request-timeout=5s",
        ],
        cwd=ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def assert_owned(cluster: str) -> None:
    """Refuse to touch a cluster this project did not create."""
    output = kubectl(
        cluster,
        "get",
        "nodes",
        "--selector",
        f"platform.gitops-reliability.io/owner={OWNER}",
        "--output",
        "name",
        "--request-timeout=5s",
        capture=True,
    )
    if not output.strip():
        raise GitOpsError(
            f"cluster {cluster} exists but lacks this project's ownership label; "
            "refusing to modify it"
        )


def install_rollouts(cluster: str) -> None:
    """Install Argo Rollouts into an environment cluster.

    The controller must run where the Rollout objects live. Installing it only
    on the control plane would leave production canaries unanalysed.
    """
    log(f"installing Argo Rollouts into {cluster}")
    kubectl(cluster, "apply", "--kustomize", str(ROLLOUTS_INSTALL))
    kubectl(
        cluster,
        "rollout",
        "status",
        "deployment/argo-rollouts",
        "--namespace",
        "argo-rollouts",
        "--timeout=180s",
    )


def install_argocd() -> None:
    log(f"installing Argo CD into {CONTROL_PLANE_CLUSTER}")
    kubectl(CONTROL_PLANE_CLUSTER, "apply", "--kustomize", str(INSTALL_PATH))

    for name in ARGOCD_DEPLOYMENTS:
        kubectl(
            CONTROL_PLANE_CLUSTER,
            "rollout",
            "status",
            f"deployment/{name}",
            "--namespace",
            ARGOCD_NAMESPACE,
            "--timeout=300s",
        )
    for name in ARGOCD_STATEFULSETS:
        kubectl(
            CONTROL_PLANE_CLUSTER,
            "rollout",
            "status",
            f"statefulset/{name}",
            "--namespace",
            ARGOCD_NAMESPACE,
            "--timeout=300s",
        )


def internal_kubeconfig(cluster: str) -> dict[str, str]:
    """Read the managed cluster's credentials using its container-network address.

    `kind get kubeconfig --internal` returns the API server address as seen from
    inside the shared Docker network, which is how a pod in the control-plane
    cluster must reach it. The host-facing 127.0.0.1 address would be the
    control-plane container's own loopback.
    """
    raw = run(
        ["kind", "get", "kubeconfig", "--name", cluster, "--internal"], capture=True
    )
    if not raw.strip():
        raise GitOpsError(f"kind returned an empty kubeconfig for {cluster}")

    temporary = ROOT / f".{cluster}.kubeconfig.tmp"
    try:
        temporary.write_text(raw, encoding="utf-8")
        # kubectl parses the YAML so this script does not have to, which keeps
        # the credential in one place and avoids a YAML dependency.
        fields = {
            "server": "{.clusters[0].cluster.server}",
            "ca": "{.clusters[0].cluster.certificate-authority-data}",
            "cert": "{.users[0].user.client-certificate-data}",
            "key": "{.users[0].user.client-key-data}",
        }
        values: dict[str, str] = {}
        for name, expression in fields.items():
            values[name] = run(
                [
                    "kubectl",
                    "--kubeconfig",
                    str(temporary),
                    "config",
                    "view",
                    "--raw",
                    "--output",
                    f"jsonpath={expression}",
                ],
                capture=True,
                quiet=True,
            ).strip()
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise GitOpsError(
                f"could not read {', '.join(missing)} from the {cluster} kubeconfig"
            )
        return values
    finally:
        temporary.unlink(missing_ok=True)


def register_managed_cluster() -> None:
    """Create the Argo CD cluster Secret for the production cluster."""
    log(f"registering {MANAGED_CLUSTER} with the Argo CD control plane")
    credentials = internal_kubeconfig(MANAGED_CLUSTER)

    config = json.dumps(
        {
            "tlsClientConfig": {
                "caData": credentials["ca"],
                "certData": credentials["cert"],
                "keyData": credentials["key"],
            }
        }
    )

    def encode(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    manifest = f"""apiVersion: v1
kind: Secret
metadata:
  name: cluster-{MANAGED_CLUSTER}
  namespace: {ARGOCD_NAMESPACE}
  labels:
    argocd.argoproj.io/secret-type: cluster
    platform.gitops-reliability.io/owner: {OWNER}
type: Opaque
data:
  name: {encode(MANAGED_CLUSTER)}
  server: {encode(credentials["server"])}
  config: {encode(config)}
"""

    # Piped to kubectl rather than written to a file: the credential never
    # touches the working tree, so it cannot be committed by accident.
    print("+ kubectl apply -f - (cluster registration secret; contents not logged)")
    result = subprocess.run(
        [
            "kubectl",
            "--context",
            context_for(CONTROL_PLANE_CLUSTER),
            "apply",
            "--filename",
            "-",
        ],
        cwd=ROOT,
        input=manifest,
        text=True,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise GitOpsError(f"cluster registration failed: {result.stderr.strip()}")
    log(result.stdout.strip() or "cluster secret applied")


def apply_projects() -> None:
    """Create the AppProjects before the Application that belongs to one.

    Argo CD refuses to sync an Application whose project does not exist, and
    the platform project is defined inside the root Application's own path.
    Applying the projects first breaks that cycle. Argo CD adopts them on the
    first sync, so this is the only time they are applied by hand; afterwards
    an edit to either project is reverted like any other drift.
    """
    log("applying AppProjects")
    kubectl(CONTROL_PLANE_CLUSTER, "apply", "--filename", str(PROJECTS_PATH))


def apply_root_application() -> None:
    log("applying the root Application")
    kubectl(
        CONTROL_PLANE_CLUSTER,
        "apply",
        "--filename",
        str(ROOT_APPLICATION),
    )


def bootstrap(skip_managed: bool) -> int:
    require("kubectl", "kind")
    repo_url = assert_configured()
    log(f"repository: {repo_url}")

    if not cluster_reachable(CONTROL_PLANE_CLUSTER):
        raise GitOpsError(
            f"cluster {CONTROL_PLANE_CLUSTER} is not reachable; run 'make up' first"
        )
    assert_owned(CONTROL_PLANE_CLUSTER)

    install_argocd()
    install_rollouts(CONTROL_PLANE_CLUSTER)

    if skip_managed:
        log(f"skipping registration of {MANAGED_CLUSTER} as requested")
    elif not cluster_reachable(MANAGED_CLUSTER):
        raise GitOpsError(
            f"cluster {MANAGED_CLUSTER} is not reachable; run 'make up', or pass "
            "--skip-managed-cluster to bootstrap the development environment only"
        )
    else:
        assert_owned(MANAGED_CLUSTER)
        register_managed_cluster()
        install_rollouts(MANAGED_CLUSTER)

    apply_projects()
    apply_root_application()

    log("")
    log("bootstrap complete. Argo CD now reconciles the committed state.")
    log("Inspect it with 'make gitops-status', or open the UI with:")
    log("  kubectl --context kind-gitops-dev --namespace argocd "
        "port-forward svc/argocd-server 8080:443")
    return 0


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def status() -> int:
    require("kubectl")
    if not cluster_reachable(CONTROL_PLANE_CLUSTER):
        print(f"control-plane/{CONTROL_PLANE_CLUSTER} unreachable")
        return 1

    unhealthy = 0
    output = kubectl(
        CONTROL_PLANE_CLUSTER,
        "get",
        "applications.argoproj.io",
        "--namespace",
        ARGOCD_NAMESPACE,
        "--output",
        "json",
        "--request-timeout=10s",
        capture=True,
        check=False,
    )
    if not output.strip():
        print("argocd/applications none (is Argo CD installed?)")
        return 1

    try:
        items = json.loads(output).get("items", [])
    except json.JSONDecodeError:
        print("argocd/applications unreadable")
        return 1

    if not items:
        print("argocd/applications none")
        return 1

    for application in sorted(items, key=lambda item: item["metadata"]["name"]):
        name = application["metadata"]["name"]
        state = application.get("status", {})
        sync = state.get("sync", {}).get("status", "Unknown")
        health = state.get("health", {}).get("status", "Unknown")
        print(f"application/{name} sync={sync} health={health}")
        if sync != "Synced" or health != "Healthy":
            unhealthy = 1

    return unhealthy


# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------


def uninstall() -> int:
    """Remove Argo CD from the control-plane cluster only."""
    require("kubectl")
    if not cluster_reachable(CONTROL_PLANE_CLUSTER):
        log(f"cluster {CONTROL_PLANE_CLUSTER} is not reachable; nothing to remove")
        return 0
    assert_owned(CONTROL_PLANE_CLUSTER)

    # Applications carry a resources finalizer; deleting them first lets Argo CD
    # clean up managed workloads instead of stranding them.
    kubectl(
        CONTROL_PLANE_CLUSTER,
        "delete",
        "applications.argoproj.io",
        "--all",
        "--namespace",
        ARGOCD_NAMESPACE,
        "--ignore-not-found",
        "--timeout=120s",
        check=False,
    )
    kubectl(
        CONTROL_PLANE_CLUSTER,
        "delete",
        "--kustomize",
        str(INSTALL_PATH),
        "--ignore-not-found",
        check=False,
    )
    for cluster in (CONTROL_PLANE_CLUSTER, MANAGED_CLUSTER):
        if cluster_reachable(cluster):
            kubectl(
                cluster,
                "delete",
                "--kustomize",
                str(ROLLOUTS_INSTALL),
                "--ignore-not-found",
                check=False,
            )
    log("Argo CD removed from the control-plane cluster; Argo Rollouts removed from both")
    return 0


def wait_for_synced(timeout: float) -> int:
    """Block until every Application reports Synced and Healthy."""
    require("kubectl")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if status() == 0:
            return 0
        time.sleep(5)
    print(f"applications did not converge within {timeout:.0f}s", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    configure_parser = sub.add_parser(
        "configure", help="Write the repository URL into the tracked manifests."
    )
    configure_parser.add_argument("--repo-url", required=True)

    bootstrap_parser = sub.add_parser(
        "bootstrap", help="Install Argo CD and apply the root Application."
    )
    bootstrap_parser.add_argument(
        "--skip-managed-cluster",
        action="store_true",
        help="Do not register the production cluster.",
    )

    sub.add_parser("status", help="Report Application sync and health.")
    sub.add_parser("uninstall", help="Remove Argo CD from the control-plane cluster.")

    wait_parser = sub.add_parser("wait", help="Wait for all Applications to converge.")
    wait_parser.add_argument("--timeout", type=float, default=300.0)

    args = parser.parse_args(argv)

    try:
        if args.action == "configure":
            return configure(args.repo_url)
        if args.action == "bootstrap":
            return bootstrap(args.skip_managed_cluster)
        if args.action == "status":
            return status()
        if args.action == "uninstall":
            return uninstall()
        if args.action == "wait":
            return wait_for_synced(args.timeout)
    except GitOpsError as exc:
        print(f"[gitops] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
