"""Static checks on the delivery manifests, runnable without a cluster.

The failures this file is written against are the ones that cost the most time
to diagnose from inside a cluster, because Argo CD reports them as a generic
sync error minutes after the commit: a kustomization pointing at a file that
was renamed, an AppProject whitelist that omits a kind the overlay renders, an
Application aimed at a cluster nobody registered, an environment overlay that
quietly lost a setting its sibling still has.

Every one of those is a property of the text in this repository, so it is
checked here in under a second rather than discovered later. What this file
deliberately does not do is re-implement Kustomize: it reads the declarations,
not the rendered output. `make bootstrap` followed by `make drift-test` is
what proves the rendered result actually works.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - environment problem
    raise SystemExit(
        "PyYAML is required for the manifest contract tests.\n"
        "Run 'make setup' (or 'pip install -r requirements-dev.txt') and retry."
    ) from exc

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "platform"
ENVIRONMENTS = ROOT / "environments"
BASE = PLATFORM / "workloads" / "base"
ARGOCD_ROOT = PLATFORM / "argocd" / "root"
INSTALL = PLATFORM / "argocd" / "install"

CATALOG = json.loads((ROOT / "apps" / "services.json").read_text(encoding="utf-8"))
SERVICE_NAMES = [service["name"] for service in CATALOG["services"]]
IN_CLUSTER_REGISTRY = CATALOG["registry"]["local_in_cluster"]

OWNER_LABEL = "platform.gitops-reliability.io/owner"
OWNER = "eks-gitops-reliability-platform"
CONFIG_MAP_NAME = "platform-environment"

# Cluster names the bootstrap is able to produce: Argo CD's built-in local
# cluster, and the one scripts/gitops.py registers a Secret for.
KNOWN_CLUSTERS = {"in-cluster", "gitops-prod"}

REMOTE_RESOURCE = re.compile(r"^(?:\w+://|github\.com/|git@)")


def load_gitops_module():
    """Import scripts/gitops.py so its constants are the ones under test.

    Duplicating the placeholder string or the file list here would let the two
    drift apart, which is exactly the failure these tests exist to prevent.
    """
    path = ROOT / "scripts" / "gitops.py"
    spec = importlib.util.spec_from_file_location("gitops_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GITOPS = load_gitops_module()


def documents(path: Path) -> list[dict]:
    """Return the non-empty YAML documents in one file."""
    text = path.read_text(encoding="utf-8")
    return [document for document in yaml.safe_load_all(text) if document]


def manifest_paths() -> list[Path]:
    paths = sorted(PLATFORM.rglob("*.yaml")) + sorted(ENVIRONMENTS.rglob("*.yaml"))
    return paths


def kustomizations() -> list[Path]:
    return [path for path in manifest_paths() if path.name == "kustomization.yaml"]


def workload_documents() -> list[dict]:
    found: list[dict] = []
    for path in sorted(BASE.glob("*.yaml")):
        if path.name in {"kustomization.yaml", "kustomizeconfig.yaml"}:
            continue
        found.extend(documents(path))
    return found


def controllers() -> list[dict]:
    return [
        document
        for document in workload_documents()
        if document.get("kind") in ("Deployment", "Rollout")
    ]


def deployments() -> list[dict]:
    return controllers()


def argocd_documents() -> list[tuple[Path, dict]]:
    found: list[tuple[Path, dict]] = []
    for path in sorted(ARGOCD_ROOT.rglob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        found.extend((path, document) for document in documents(path))
    return found


def overlay_paths() -> list[Path]:
    return sorted(ENVIRONMENTS.glob("*/kustomization.yaml"))


def config_literals(overlay: dict) -> dict[str, str]:
    for generator in overlay.get("configMapGenerator", []):
        if generator.get("name") != CONFIG_MAP_NAME:
            continue
        pairs = {}
        for literal in generator.get("literals", []):
            key, _, value = literal.partition("=")
            pairs[key] = value
        return pairs
    raise AssertionError(f"no {CONFIG_MAP_NAME} configMapGenerator in the overlay")


class ManifestsParse(unittest.TestCase):
    def test_every_manifest_is_valid_yaml(self):
        for path in manifest_paths():
            with self.subTest(path=path.relative_to(ROOT).as_posix()):
                documents(path)

    def test_no_secret_manifest_is_committed(self):
        """Secrets belong in the external secret store, never in Git."""
        for path in manifest_paths():
            for document in documents(path):
                self.assertNotEqual(
                    document.get("kind"),
                    "Secret",
                    f"{path.relative_to(ROOT).as_posix()} commits a Secret",
                )

    def test_kustomizations_reference_files_that_exist(self):
        for path in kustomizations():
            overlay = documents(path)[0]
            referenced = list(overlay.get("resources", []))
            referenced.extend(
                patch["path"] for patch in overlay.get("patches", []) if "path" in patch
            )
            for reference in referenced:
                if REMOTE_RESOURCE.match(reference):
                    continue
                with self.subTest(kustomization=path.name, reference=reference):
                    self.assertTrue(
                        (path.parent / reference).exists(),
                        f"{path.relative_to(ROOT).as_posix()} references "
                        f"{reference}, which does not exist",
                    )

    def test_every_base_manifest_is_reachable_from_the_kustomization(self):
        """A file nobody references is dead weight that looks like desired state."""
        kustomization = documents(BASE / "kustomization.yaml")[0]
        declared = set(kustomization["resources"])
        declared.update(kustomization.get("configurations", []))
        present = {
            path.name for path in BASE.glob("*.yaml") if path.name != "kustomization.yaml"
        }
        self.assertEqual(
            present - declared,
            set(),
            "base manifests exist that the kustomization does not include",
        )


class RepositoryUrlWiring(unittest.TestCase):
    def test_configure_knows_about_every_file_holding_the_placeholder(self):
        holding = {
            path.relative_to(ROOT)
            for path in manifest_paths()
            if GITOPS.PLACEHOLDER in path.read_text(encoding="utf-8")
        }
        self.assertEqual(
            holding,
            set(GITOPS.CONFIGURABLE_FILES),
            "scripts/gitops.py would leave a placeholder behind, or lists a "
            "file that no longer contains one",
        )

    def test_every_configurable_file_exists(self):
        for relative in GITOPS.CONFIGURABLE_FILES:
            self.assertTrue((ROOT / relative).is_file(), f"missing {relative}")

    def test_configure_rejects_values_that_are_not_git_urls(self):
        for candidate in ("", "example.com/repo", "/tmp/repo", "ftp://host/repo"):
            with self.subTest(candidate=candidate):
                self.assertIsNone(GITOPS.REPO_URL_PATTERN.match(candidate))

    def test_configure_accepts_the_usual_git_url_forms(self):
        for candidate in (
            "https://github.com/example/repo.git",
            "git@github.com:example/repo.git",
            "ssh://git@github.com/example/repo.git",
        ):
            with self.subTest(candidate=candidate):
                self.assertIsNotNone(GITOPS.REPO_URL_PATTERN.match(candidate))


class ArgoCdObjects(unittest.TestCase):
    def test_argocd_objects_are_namespaced_and_owned(self):
        for path, document in argocd_documents():
            with self.subTest(path=path.name, kind=document["kind"]):
                metadata = document["metadata"]
                self.assertEqual(metadata.get("namespace"), "argocd")
                self.assertEqual(metadata.get("labels", {}).get(OWNER_LABEL), OWNER)

    def test_the_installation_pins_an_upstream_release(self):
        resources = documents(INSTALL / "kustomization.yaml")[0]["resources"]
        remote = [
            resource for resource in resources if resource.startswith("github.com/")
        ]
        self.assertTrue(remote, "the Argo CD installation is not declared")
        for resource in remote:
            self.assertRegex(
                resource,
                r"\?ref=v\d+\.\d+\.\d+$",
                "the installation must pin an exact release, not a branch",
            )

    def test_applications_target_clusters_the_bootstrap_registers(self):
        for path, document in argocd_documents():
            if document["kind"] == "Application":
                destinations = [document["spec"]["destination"]]
            elif document["kind"] == "ApplicationSet":
                destinations = [document["spec"]["template"]["spec"]["destination"]]
            else:
                continue
            for destination in destinations:
                self.assertNotIn(
                    "server",
                    destination,
                    f"{path.name} addresses a cluster by URL; use the registered "
                    "name so the manifest does not depend on a container IP",
                )
                name = destination["name"]
                # The ApplicationSet template resolves its name from the
                # generator, which is checked separately.
                if "{{" in name:
                    continue
                self.assertIn(name, KNOWN_CLUSTERS)

    def test_the_applicationset_only_generates_registered_clusters(self):
        specification = documents(ARGOCD_ROOT / "workloads-applicationset.yaml")[0]
        elements = specification["spec"]["generators"][0]["list"]["elements"]
        for element in elements:
            with self.subTest(environment=element["environment"]):
                self.assertIn(element["clusterName"], KNOWN_CLUSTERS)

    def test_every_generated_environment_has_an_overlay(self):
        specification = documents(ARGOCD_ROOT / "workloads-applicationset.yaml")[0]
        elements = specification["spec"]["generators"][0]["list"]["elements"]
        generated = {element["environment"] for element in elements}
        on_disk = {path.parent.name for path in overlay_paths()}
        self.assertEqual(generated, on_disk)

    def test_the_applicationset_path_matches_the_overlay_layout(self):
        specification = documents(ARGOCD_ROOT / "workloads-applicationset.yaml")[0]
        path = specification["spec"]["template"]["spec"]["source"]["path"]
        self.assertEqual(path, "environments/{{.environment}}")

    def test_the_root_application_points_at_a_real_kustomization(self):
        specification = documents(ARGOCD_ROOT / "root-application.yaml")[0]
        source = specification["spec"]["source"]
        self.assertTrue((ROOT / source["path"] / "kustomization.yaml").is_file())
        self.assertEqual(source["targetRevision"], "main")

    def test_the_root_application_manages_its_own_definition(self):
        """The root must be inside the path it syncs, or Git stops owning it."""
        specification = documents(ARGOCD_ROOT / "root-application.yaml")[0]
        managed = ROOT / specification["spec"]["source"]["path"]
        kustomization = documents(managed / "kustomization.yaml")[0]
        self.assertIn("root-application.yaml", kustomization["resources"])

    def test_self_heal_and_prune_are_enabled_everywhere(self):
        """The drift test is only meaningful while these stay on."""
        for path, document in argocd_documents():
            if document["kind"] == "Application":
                policy = document["spec"]["syncPolicy"]
            elif document["kind"] == "ApplicationSet":
                policy = document["spec"]["template"]["spec"]["syncPolicy"]
            else:
                continue
            with self.subTest(path=path.name):
                automated = policy.get("automated")
                self.assertIsNotNone(automated, "automated sync is disabled")
                self.assertIs(automated.get("selfHeal"), True)
                self.assertIs(automated.get("prune"), True)

    def test_applications_belong_to_a_project_defined_in_this_repository(self):
        projects = {
            document["metadata"]["name"]
            for _, document in argocd_documents()
            if document["kind"] == "AppProject"
        }
        for path, document in argocd_documents():
            if document["kind"] == "Application":
                project = document["spec"]["project"]
            elif document["kind"] == "ApplicationSet":
                project = document["spec"]["template"]["spec"]["project"]
            else:
                continue
            with self.subTest(path=path.name):
                self.assertIn(project, projects)
                self.assertNotEqual(
                    project, "default", "the permissive default project is not a home"
                )

    def test_resource_tracking_is_annotation_based(self):
        """Label tracking truncates at 63 characters and produces false drift."""
        patch = documents(INSTALL / "patches" / "argocd-cm.yaml")[0]
        self.assertEqual(
            patch["data"]["application.resourceTrackingMethod"], "annotation"
        )

    def test_replica_drift_is_not_ignored(self):
        """An ignoreDifferences rule on replicas would void the drift test."""
        patch = documents(INSTALL / "patches" / "argocd-cm.yaml")[0]
        self.assertNotIn("resource.customizations.ignoreDifferences.all", patch["data"])
        for key, value in patch["data"].items():
            if key.startswith("resource.customizations.ignoreDifferences"):
                self.assertNotIn("replicas", value, f"{key} hides replica drift")


class ProjectGuardrails(unittest.TestCase):
    def workloads_project(self) -> dict:
        return documents(ARGOCD_ROOT / "projects" / "workloads.yaml")[0]["spec"]

    def test_the_workloads_project_permits_every_kind_the_base_renders(self):
        specification = self.workloads_project()
        namespaced = {
            (entry["group"], entry["kind"])
            for entry in specification["namespaceResourceWhitelist"]
        }
        cluster_scoped = {
            (entry["group"], entry["kind"])
            for entry in specification["clusterResourceWhitelist"]
        }
        permitted = namespaced | cluster_scoped
        # The overlays add a generated ConfigMap on top of the base resources.
        rendered = {("", "ConfigMap")}
        for document in workload_documents():
            group, _, _ = document["apiVersion"].partition("/")
            rendered.add(("" if "/" not in document["apiVersion"] else group,
                          document["kind"]))
        missing = rendered - permitted
        self.assertEqual(
            missing,
            set(),
            "the workloads AppProject would refuse to sync these kinds",
        )

    def test_the_workloads_project_forbids_secrets(self):
        specification = self.workloads_project()
        blacklisted = {
            (entry["group"], entry["kind"])
            for entry in specification["namespaceResourceBlacklist"]
        }
        self.assertIn(("", "Secret"), blacklisted)

    def test_the_workloads_project_grants_no_roles(self):
        self.assertEqual(self.workloads_project()["roles"], [])

    def test_project_destinations_cover_the_generated_applications(self):
        specification = self.workloads_project()
        allowed = {
            (destination["name"], destination["namespace"])
            for destination in specification["destinations"]
        }
        applicationset = documents(ARGOCD_ROOT / "workloads-applicationset.yaml")[0]
        template = applicationset["spec"]["template"]["spec"]
        namespace = template["destination"]["namespace"]
        for element in applicationset["spec"]["generators"][0]["list"]["elements"]:
            with self.subTest(environment=element["environment"]):
                self.assertIn((element["clusterName"], namespace), allowed)


class WorkloadHardening(unittest.TestCase):
    def test_the_namespace_enforces_the_restricted_profile(self):
        namespace = documents(BASE / "namespace.yaml")[0]
        labels = namespace["metadata"]["labels"]
        self.assertEqual(labels["pod-security.kubernetes.io/enforce"], "restricted")
        self.assertEqual(labels["pod-security.kubernetes.io/warn"], "restricted")

    def test_every_catalogued_service_has_a_workload_controller(self):
        named = {document["metadata"]["name"] for document in controllers()}
        self.assertEqual(named, set(SERVICE_NAMES))

    def test_the_api_is_a_rollout_not_a_deployment(self):
        """A Deployment cannot abort a canary. The API is the progressive-delivery subject."""
        kinds = {
            document["metadata"]["name"]: document["kind"] for document in controllers()
        }
        self.assertEqual(kinds["api"], "Rollout")
        for name in SERVICE_NAMES:
            if name == "api":
                continue
            self.assertEqual(kinds[name], "Deployment")

    def test_pod_specs_satisfy_the_restricted_profile(self):
        for document in deployments():
            name = document["metadata"]["name"]
            pod = document["spec"]["template"]["spec"]
            with self.subTest(deployment=name):
                self.assertIs(pod["securityContext"]["runAsNonRoot"], True)
                self.assertNotEqual(pod["securityContext"]["runAsUser"], 0)
                self.assertEqual(
                    pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault"
                )
                self.assertIs(pod["automountServiceAccountToken"], False)
                self.assertEqual(pod["serviceAccountName"], name)
                for container in pod["containers"]:
                    security = container["securityContext"]
                    self.assertIs(security["allowPrivilegeEscalation"], False)
                    self.assertIs(security["readOnlyRootFilesystem"], True)
                    self.assertEqual(security["capabilities"]["drop"], ["ALL"])

    def test_every_container_declares_probes_and_bounded_memory(self):
        for document in deployments():
            for container in document["spec"]["template"]["spec"]["containers"]:
                with self.subTest(container=container["name"]):
                    self.assertIn("livenessProbe", container)
                    self.assertIn("readinessProbe", container)
                    resources = container["resources"]
                    self.assertIn("cpu", resources["requests"])
                    self.assertIn("memory", resources["requests"])
                    self.assertIn("memory", resources["limits"])

    def test_a_read_only_root_filesystem_has_somewhere_to_write(self):
        for document in deployments():
            pod = document["spec"]["template"]["spec"]
            mounted = {
                mount["name"]
                for container in pod["containers"]
                for mount in container.get("volumeMounts", [])
            }
            declared = {volume["name"] for volume in pod.get("volumes", [])}
            with self.subTest(deployment=document["metadata"]["name"]):
                self.assertEqual(mounted - declared, set(), "unbacked volumeMount")

    def test_containers_read_their_configuration_from_the_generated_map(self):
        for document in deployments():
            for container in document["spec"]["template"]["spec"]["containers"]:
                sources = [
                    source["configMapRef"]["name"]
                    for source in container.get("envFrom", [])
                    if "configMapRef" in source
                ]
                with self.subTest(container=container["name"]):
                    self.assertIn(
                        CONFIG_MAP_NAME,
                        sources,
                        "environment settings must come from the overlay's "
                        "generated ConfigMap so a change rolls the pods",
                    )

    def test_images_come_from_the_in_cluster_registry(self):
        for document in deployments():
            for container in document["spec"]["template"]["spec"]["containers"]:
                with self.subTest(container=container["name"]):
                    self.assertTrue(
                        container["image"].startswith(f"{IN_CLUSTER_REGISTRY}/"),
                        f"{container['image']} is not served by the local registry",
                    )


class EnvironmentOverlays(unittest.TestCase):
    def overlays(self) -> dict[str, dict]:
        return {path.parent.name: documents(path)[0] for path in overlay_paths()}

    def test_there_is_more_than_one_environment(self):
        self.assertGreater(len(self.overlays()), 1)

    def test_each_overlay_names_itself(self):
        for name, overlay in self.overlays().items():
            with self.subTest(environment=name):
                self.assertEqual(config_literals(overlay)["PLATFORM_ENVIRONMENT"], name)

    def test_overlays_build_on_the_shared_base(self):
        for name, overlay in self.overlays().items():
            with self.subTest(environment=name):
                resolved = [
                    (ENVIRONMENTS / name / resource).resolve()
                    for resource in overlay["resources"]
                ]
                self.assertEqual(resolved, [BASE.resolve()])

    def test_overlays_declare_the_same_settings(self):
        """A key present in one environment and absent in another is a trap."""
        keys = {
            name: set(config_literals(overlay))
            for name, overlay in self.overlays().items()
        }
        shared = set.intersection(*keys.values())
        for name, present in keys.items():
            with self.subTest(environment=name):
                self.assertEqual(
                    present - shared, set(), "setting is missing from another overlay"
                )

    def test_each_overlay_pins_an_image_for_every_service(self):
        for name, overlay in self.overlays().items():
            pinned = {image["name"] for image in overlay["images"]}
            with self.subTest(environment=name):
                self.assertEqual(
                    pinned,
                    {f"{IN_CLUSTER_REGISTRY}/{service}" for service in SERVICE_NAMES},
                )

    def test_each_image_pin_is_either_bootstrap_or_a_digest(self):
        """A promoted release is a digest. A tag is only the pre-promotion placeholder."""
        for name, overlay in self.overlays().items():
            for image in overlay["images"]:
                with self.subTest(environment=name, image=image["name"]):
                    if "digest" in image:
                        self.assertNotIn("newTag", image)
                        self.assertRegex(image["digest"], r"^sha256:[0-9a-f]{64}$")
                    else:
                        self.assertEqual(image.get("newTag"), "0.0.0-bootstrap")

    def test_an_overlay_does_not_mix_bootstrap_tags_and_digests(self):
        for name, overlay in self.overlays().items():
            digested = ["digest" in image for image in overlay["images"]]
            with self.subTest(environment=name):
                self.assertTrue(all(digested) or not any(digested))

    def test_each_overlay_sets_replicas_for_every_workload(self):
        for name, overlay in self.overlays().items():
            counted = {entry["name"] for entry in overlay["replicas"]}
            with self.subTest(environment=name):
                self.assertEqual(counted, set(SERVICE_NAMES))

    def test_production_runs_more_than_one_replica_of_each_service(self):
        """A single replica cannot survive the node drain M10 performs."""
        overlay = self.overlays()["production"]
        counts = {entry["name"]: entry["count"] for entry in overlay["replicas"]}
        for service in SERVICE_NAMES:
            if service == "load-generator":
                # Test equipment: a second replica would double the offered rate.
                continue
            with self.subTest(service=service):
                self.assertGreaterEqual(counts[service], 2)

    def test_production_injects_no_faults(self):
        literals = config_literals(self.overlays()["production"])
        for key in (
            "FAULT_ERROR_RATIO",
            "FAULT_LATENCY_RATIO",
            "LOAD_INVALID_RATIO",
            "CANARY_FAULT_ERROR_RATIO",
        ):
            with self.subTest(setting=key):
                self.assertEqual(float(literals[key]), 0.0)


if __name__ == "__main__":
    unittest.main()
