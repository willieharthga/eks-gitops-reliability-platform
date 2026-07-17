"""Static lifecycle contract tests that deliberately require no Docker daemon."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOCAL_DIR = REPOSITORY_ROOT / "infrastructure" / "local"
BASH_SCRIPT = REPOSITORY_ROOT / "scripts" / "platform.sh"
POWERSHELL_SCRIPT = REPOSITORY_ROOT / "scripts" / "platform.ps1"

CLUSTERS = ("gitops-dev", "gitops-prod")
NODE_IMAGE = (
    "kindest/node:v1.36.4@"
    "sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed"
)
REGISTRY_IMAGE = (
    "registry:3.1.1@"
    "sha256:1be55279f18a2fe1a74edf2664cac61c1bea305b7b4642dab412e7affdcb3e33"
)
REGISTRY_URL = "http://localhost:5001/v2/"
CONTAINER_REGISTRY_HOST = '[host."http://gitops-registry:5000"]'


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class KindConfigurationContractTests(unittest.TestCase):
    def test_each_environment_has_a_current_kind_config(self) -> None:
        for environment in ("dev", "prod"):
            with self.subTest(environment=environment):
                config = read(LOCAL_DIR / f"kind-{environment}.yaml")
                self.assertIn("kind: Cluster", config)
                self.assertIn("apiVersion: kind.x-k8s.io/v1alpha4", config)
                self.assertIn(
                    '[plugins."io.containerd.grpc.v1.cri".registry]', config
                )
                self.assertIn('config_path = "/etc/containerd/certs.d"', config)
                self.assertNotIn("registry.mirrors", config)
                self.assertIn(
                    "platform.gitops-reliability.io/owner: "
                    "eks-gitops-reliability-platform",
                    config,
                )
                self.assertIn(
                    f"platform.gitops-reliability.io/environment: {environment}",
                    config,
                )

    def test_registry_discovery_uses_the_kind_documented_contract(self) -> None:
        manifest = read(LOCAL_DIR / "local-registry-hosting.yaml")
        self.assertIn("name: local-registry-hosting", manifest)
        self.assertIn("namespace: kube-public", manifest)
        self.assertIn("localRegistryHosting.v1: |", manifest)
        self.assertIn('host: "localhost:5001"', manifest)
        self.assertIn(
            'help: "https://kind.sigs.k8s.io/docs/user/local-registry/"', manifest
        )


class LifecycleScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bash = read(BASH_SCRIPT)
        cls.powershell = read(POWERSHELL_SCRIPT)

    def test_resource_names_and_images_are_pinned_in_both_scripts(self) -> None:
        for script_name, script in (
            ("bash", self.bash),
            ("powershell", self.powershell),
        ):
            with self.subTest(script=script_name):
                for cluster in CLUSTERS:
                    self.assertIn(cluster, script)
                self.assertIn("gitops-registry", script)
                self.assertIn(NODE_IMAGE, script)
                self.assertIn(REGISTRY_IMAGE, script)
                self.assertIn("5001", script)
                self.assertIn("5000", script)

    def test_all_actions_have_cross_platform_implementations(self) -> None:
        for action in ("up", "status", "smoke", "down", "doctor"):
            with self.subTest(action=action):
                self.assertRegex(self.bash, rf"\b{action}\)")
                self.assertIn(f'"{action}"', self.powershell)

    def test_current_kind_registry_host_configuration_is_written_to_every_node(
        self,
    ) -> None:
        for script_name, script in (
            ("bash", self.bash),
            ("powershell", self.powershell),
        ):
            with self.subTest(script=script_name):
                self.assertIn("kind get nodes --name", script)
                self.assertIn("/etc/containerd/certs.d/localhost:5001", script)
                self.assertIn(CONTAINER_REGISTRY_HOST, script)
                self.assertIn("hosts.toml", script)
                self.assertRegex(script, r"docker exec(?: --interactive)?")

    def test_up_waits_for_nodes_and_coredns(self) -> None:
        for script_name, script in (
            ("bash", self.bash),
            ("powershell", self.powershell),
        ):
            with self.subTest(script=script_name):
                self.assertIn("condition=Ready", script)
                self.assertIn("deployment/coredns", script)
                self.assertIn("120s", script)

    def test_smoke_is_read_only_and_checks_registry_and_nodes(self) -> None:
        for script_name, script in (
            ("bash", self.bash),
            ("powershell", self.powershell),
        ):
            with self.subTest(script=script_name):
                self.assertIn(REGISTRY_URL, script)
                self.assertIn("get nodes --no-headers", script)
                self.assertIn("Ready", script)

        bash_smoke = self.bash[
            self.bash.index("smoke_platform()") : self.bash.index("down_platform()")
        ]
        ps_smoke = self.powershell[
            self.powershell.index("function Test-PlatformSmoke") : self.powershell.index(
                "function Stop-Platform"
            )
        ]
        for implementation in (bash_smoke, ps_smoke):
            self.assertNotRegex(
                implementation,
                r"\b(create|delete|apply|run|start|stop|remove|rm)\b",
            )

    def test_down_uses_exact_names_and_has_no_bulk_delete(self) -> None:
        self.assertIn('kind delete cluster --name "${cluster}"', self.bash)
        self.assertIn(
            'docker container rm --force "${REGISTRY_NAME}"', self.bash
        )
        self.assertIn('@("delete", "cluster", "--name", $cluster)', self.powershell)
        self.assertIn(
            '@("container", "rm", "--force", $script:RegistryName)',
            self.powershell,
        )

        unsafe_patterns = (
            r"kind\s+delete\s+clusters",
            r"docker\s+(?:container\s+)?prune",
            r"docker\s+system\s+prune",
            r"docker\s+network\s+rm",
            r"docker\s+(?:container\s+)?rm[^\n]*\$\(",
        )
        for script_name, script in (
            ("bash", self.bash),
            ("powershell", self.powershell),
        ):
            for pattern in unsafe_patterns:
                with self.subTest(script=script_name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, script, re.IGNORECASE))

    def test_registry_ownership_is_checked_before_removal(self) -> None:
        bash_down = self.bash[self.bash.index("down_platform()") :]
        ps_down = self.powershell[
            self.powershell.index("function Stop-Platform") :
        ]
        self.assertLess(
            bash_down.index("assert_registry_owned"),
            bash_down.index("docker container rm"),
        )
        self.assertLess(
            ps_down.index("Assert-RegistryOwned"),
            ps_down.index('"container", "rm"'),
        )


if __name__ == "__main__":
    unittest.main()
