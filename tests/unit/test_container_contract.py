"""Static contract for the container image and the service catalogue.

These assertions run without Docker. They are the part of the M8 security
posture that can be proved before a cluster exists: if the image runs as root
or resolves a mutable base tag, no admission policy written later can undo it.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import _bootstrap
from _bootstrap import APPS, CATALOG, REPOSITORY_ROOT, SERVICES

DOCKERFILE = APPS / "Dockerfile"
BUILD_SCRIPT = REPOSITORY_ROOT / "scripts" / "build.py"

DIGEST_PINNED_FROM = re.compile(
    r"^FROM\s+\S+@sha256:[0-9a-f]{64}(\s+AS\s+\w+)?\s*$", re.MULTILINE
)
ANY_FROM = re.compile(r"^FROM\s+", re.MULTILINE)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class CatalogueTests(unittest.TestCase):
    def test_the_catalogue_lists_the_four_documented_services(self) -> None:
        self.assertEqual(
            sorted(service["name"] for service in SERVICES),
            ["api", "frontend", "load-generator", "worker"],
        )

    def test_every_catalogue_entry_points_at_real_source(self) -> None:
        for service in SERVICES:
            with self.subTest(service=service["name"]):
                package = APPS / service["directory"] / service["package"]
                self.assertTrue(package.is_dir(), f"missing package: {package}")
                self.assertTrue((package / "main.py").is_file())
                self.assertTrue((package / "__init__.py").is_file())

    def test_package_names_are_unique_so_tests_can_import_them_together(self) -> None:
        packages = [service["package"] for service in SERVICES]
        self.assertEqual(len(packages), len(set(packages)))

    def test_every_service_exposes_a_build_application_entry_point(self) -> None:
        for service in SERVICES:
            with self.subTest(service=service["name"]):
                source = read(APPS / service["directory"] / service["package"] / "main.py")
                self.assertIn("def build_application(", source)
                self.assertIn("def main()", source)

    def test_the_local_registry_matches_the_lifecycle_scripts(self) -> None:
        registry = CATALOG["registry"]
        self.assertEqual(registry["local"], "localhost:5001")
        self.assertEqual(registry["local_in_cluster"], "gitops-registry:5000")
        lifecycle = read(REPOSITORY_ROOT / "scripts" / "platform.sh")
        self.assertIn(registry["local_in_cluster"], lifecycle)


class ImageHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dockerfile = read(DOCKERFILE)

    def test_every_base_image_is_pinned_by_digest(self) -> None:
        # A mutable tag means the bytes that passed the vulnerability gate are
        # not necessarily the bytes that get deployed.
        self.assertEqual(
            len(ANY_FROM.findall(self.dockerfile)),
            len(DIGEST_PINNED_FROM.findall(self.dockerfile)),
        )

    def test_the_image_runs_as_a_fixed_non_root_uid(self) -> None:
        self.assertIn("USER 10001:10001", self.dockerfile)
        self.assertNotIn("USER root", self.dockerfile)

    def test_the_user_is_the_last_privilege_change_before_the_entrypoint(self) -> None:
        self.assertLess(
            self.dockerfile.index("USER 10001:10001"),
            self.dockerfile.index("ENTRYPOINT"),
        )

    def test_bytecode_writing_is_disabled_for_a_read_only_root_filesystem(self) -> None:
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.dockerfile)

    def test_logs_are_unbuffered_so_they_are_not_lost_on_termination(self) -> None:
        self.assertIn("PYTHONUNBUFFERED=1", self.dockerfile)

    def test_the_entrypoint_is_exec_form_so_the_process_receives_sigterm(self) -> None:
        # Shell form would leave /bin/sh as PID 1 and swallow SIGTERM, turning
        # every rollout into a 30-second forced kill.
        self.assertIn('ENTRYPOINT ["python", "-m", "service.main"]', self.dockerfile)

    def test_no_package_manager_runs_in_the_runtime_stage(self) -> None:
        runtime_stage = self.dockerfile[self.dockerfile.rindex("FROM ") :]
        for forbidden in ("apt-get install", "pip install", "curl ", "wget "):
            with self.subTest(command=forbidden):
                self.assertNotIn(forbidden, runtime_stage)

    def test_the_image_carries_oci_provenance_labels(self) -> None:
        for label in (
            "org.opencontainers.image.version",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.source",
            "org.opencontainers.image.licenses",
        ):
            with self.subTest(label=label):
                self.assertIn(label, self.dockerfile)

    def test_the_service_package_lands_at_a_fixed_path(self) -> None:
        # The run command must not need a shell to expand a variable.
        self.assertIn("./service", self.dockerfile)

    def test_no_secret_is_baked_into_the_image(self) -> None:
        for forbidden in ("AWS_SECRET", "PASSWORD=", "TOKEN=", "api_key"):
            with self.subTest(value=forbidden):
                self.assertNotIn(forbidden, self.dockerfile)


class BuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read(BUILD_SCRIPT)

    def test_the_build_records_an_immutable_digest_reference(self) -> None:
        self.assertIn("pinned_reference", self.source)
        self.assertIn("containerimage.digest", self.source)

    def test_sbom_and_provenance_attestations_are_requested(self) -> None:
        self.assertIn("--provenance=mode=max", self.source)
        self.assertIn("--sbom=true", self.source)

    def test_the_build_does_not_write_environment_configuration(self) -> None:
        # Building an artifact and promoting one are separate actions with
        # different approvals; see ADR 0003.
        for path in ("environments/", "kubectl", "argocd"):
            with self.subTest(path=path):
                self.assertNotIn(path, self.source)

    def test_an_unclean_working_tree_is_reported(self) -> None:
        self.assertIn("working_tree_is_clean", self.source)

    def test_the_build_reads_the_shared_catalogue(self) -> None:
        self.assertIn("services.json", self.source)


class BuildScriptBehaviourTests(unittest.TestCase):
    """Exercises the pure helpers without invoking Docker."""

    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location("build_script", BUILD_SCRIPT)
        assert spec is not None and spec.loader is not None
        cls.module = importlib.util.module_from_spec(spec)
        # @dataclass resolves annotations through sys.modules[cls.__module__],
        # so the module must be registered before it is executed.
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def test_services_load_from_the_catalogue(self) -> None:
        names = {service.name for service in self.module.load_services()}
        self.assertEqual(names, {"api", "frontend", "worker", "load-generator"})

    def test_an_explicit_version_is_used_verbatim(self) -> None:
        self.assertEqual(self.module.resolve_version("1.4.2", "abc"), "1.4.2")

    def test_a_derived_version_embeds_the_revision(self) -> None:
        derived = self.module.resolve_version(None, "0123456789abcdef")
        self.assertTrue(derived.startswith("0.0.0-"))
        self.assertIn("0123456789ab", derived)

    def test_the_default_registry_is_the_local_one(self) -> None:
        self.assertEqual(self.module.load_registry("local"), "localhost:5001")

    def test_building_without_docker_fails_with_an_actionable_message(self) -> None:
        import shutil

        original = shutil.which
        shutil.which = lambda name: None  # type: ignore[assignment]
        try:
            with self.assertRaises(self.module.BuildError) as caught:
                self.module.require_tools(push=False)
        finally:
            shutil.which = original  # type: ignore[assignment]
        self.assertIn("docker is required", str(caught.exception))


class CatalogueFormattingTests(unittest.TestCase):
    def test_the_catalogue_is_valid_json_with_a_trailing_newline(self) -> None:
        raw = read(_bootstrap.CATALOG_PATH)
        json.loads(raw)
        self.assertTrue(raw.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
