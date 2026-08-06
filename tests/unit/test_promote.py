"""Promotion and rollback behaviour, including the failure cases reviewers hit.

The happy path is a small part of this file. Most of the tests exist because a
promotion tool that writes the wrong overlay, copies a bootstrap tag into
production, or talks to a cluster would quietly break the GitOps contract the
rest of the repository is built on.

No test here starts a cluster or runs kubectl. Applying a digest is Argo CD's
job after the commit is reviewed.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_promote():
    path = ROOT / "scripts" / "promote.py"
    spec = importlib.util.spec_from_file_location("promote_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROMOTE = load_promote()

DIGEST_A = "sha256:" + ("a" * 64)
DIGEST_B = "sha256:" + ("b" * 64)
DIGEST_C = "sha256:" + ("c" * 64)
SERVICES = ("api", "frontend", "worker", "load-generator")


def digest_for(service: str, generation: str) -> str:
    """Stable, valid sha256 hex derived from the service name and generation."""
    seed = f"{service}:{generation}".encode("utf-8").hex()
    return "sha256:" + (seed * 8)[:64]


class IsolateOverlays(unittest.TestCase):
    """Each test gets its own copy of the environment overlays."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="promote-test-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        source = ROOT / "environments"
        dest = self.tmpdir / "environments"
        shutil.copytree(source, dest)
        self.promote = PROMOTE
        self._original_env = self.promote.ENVIRONMENTS
        self.promote.ENVIRONMENTS = dest
        self.addCleanup(setattr, self.promote, "ENVIRONMENTS", self._original_env)
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = io.StringIO()
        sys.stderr = io.StringIO()
        self.addCleanup(setattr, sys, "stdout", self._stdout)
        self.addCleanup(setattr, sys, "stderr", self._stderr)

    def overlay(self, environment: str) -> Path:
        return self.promote.ENVIRONMENTS / environment / "kustomization.yaml"

    def history(self, environment: str) -> Path:
        return self.promote.ENVIRONMENTS / environment / "promotion-history.json"

    def write_manifest(self, generation: str = "one", *, omit: str | None = None) -> Path:
        path = self.tmpdir / "images.json"
        images = []
        for service in SERVICES:
            if service == omit:
                continue
            images.append(
                {
                    "service": service,
                    "repository": f"localhost:5001/{service}",
                    "digest": digest_for(service, generation),
                    "tag": f"0.0.0-test+{generation}",
                }
            )
        path.write_text(
            json.dumps(
                {
                    "version": f"0.0.0-test+{generation}",
                    "revision": f"deadbeef{generation}",
                    "images": images,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def apply_dev(self, generation: str = "one") -> None:
        manifest = self.write_manifest(generation)
        rc = self.promote.main(
            ["apply", "--environment", "dev", "--from", str(manifest)]
        )
        self.assertEqual(rc, 0)

    def apply_production(self) -> None:
        rc = self.promote.main(
            [
                "apply",
                "--environment",
                "production",
                "--from-environment",
                "dev",
                "--confirm",
                "production",
            ]
        )
        self.assertEqual(rc, 0)

    def pins(self, environment: str) -> dict[str, str]:
        return {pin.service: pin.digest for pin in self.promote.pins_from_overlay(environment)}


class ApplyDevelopment(IsolateOverlays):
    def test_dev_promotion_writes_a_digest_for_every_service(self):
        self.apply_dev("one")
        pins = self.promote.pins_from_overlay("dev")
        self.assertEqual([pin.service for pin in pins], list(SERVICES))
        for pin in pins:
            self.assertTrue(pin.is_digest, pin)
            self.assertEqual(pin.digest, digest_for(pin.service, "one"))
            self.assertTrue(pin.name.startswith("gitops-registry:5000/"))

    def test_dev_promotion_rewrites_localhost_to_the_in_cluster_registry(self):
        self.apply_dev()
        text = self.overlay("dev").read_text(encoding="utf-8")
        self.assertNotIn("localhost:5001", text)
        self.assertNotIn("newTag:", text)
        self.assertIn("digest: sha256:", text)

    def test_dev_promotion_is_idempotent(self):
        self.apply_dev("one")
        first = self.history("dev").read_text(encoding="utf-8")
        self.apply_dev("one")
        second = self.history("dev").read_text(encoding="utf-8")
        self.assertEqual(json.loads(first)["entries"], json.loads(second)["entries"])

    def test_a_second_dev_promotion_records_distinct_history(self):
        self.apply_dev("one")
        self.apply_dev("two")
        entries = json.loads(self.history("dev").read_text(encoding="utf-8"))["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            entries[0]["images"]["api"]["digest"], digest_for("api", "one")
        )
        self.assertEqual(
            entries[1]["images"]["api"]["digest"], digest_for("api", "two")
        )


class ApplyProduction(IsolateOverlays):
    def test_production_copies_the_dev_digests(self):
        self.apply_dev("one")
        self.apply_production()
        self.assertEqual(self.pins("production"), self.pins("dev"))

    def test_production_rejects_a_build_manifest(self):
        manifest = self.write_manifest()
        rc = self.promote.main(
            [
                "apply",
                "--environment",
                "production",
                "--from",
                str(manifest),
                "--confirm",
                "production",
            ]
        )
        self.assertEqual(rc, 1)

    def test_production_rejects_missing_confirmation(self):
        self.apply_dev()
        rc = self.promote.main(
            [
                "apply",
                "--environment",
                "production",
                "--from-environment",
                "dev",
            ]
        )
        self.assertEqual(rc, 1)
        # Overlay must be untouched.
        self.assertTrue(
            all(pin.is_bootstrap for pin in self.promote.pins_from_overlay("production"))
        )

    def test_production_rejects_the_wrong_confirmation_string(self):
        self.apply_dev()
        rc = self.promote.main(
            [
                "apply",
                "--environment",
                "production",
                "--from-environment",
                "dev",
                "--confirm",
                "yes",
            ]
        )
        self.assertEqual(rc, 1)

    def test_production_rejects_copying_bootstrap_tags(self):
        rc = self.promote.main(
            [
                "apply",
                "--environment",
                "production",
                "--from-environment",
                "dev",
                "--confirm",
                "production",
            ]
        )
        self.assertEqual(rc, 1)

    def test_production_rejects_promoting_from_itself(self):
        self.apply_dev()
        rc = self.promote.main(
            [
                "apply",
                "--environment",
                "production",
                "--from-environment",
                "production",
                "--confirm",
                "production",
            ]
        )
        self.assertEqual(rc, 1)


class Rollback(IsolateOverlays):
    def test_rollback_restores_the_previous_digest_set(self):
        self.apply_dev("one")
        self.apply_dev("two")
        rc = self.promote.main(["rollback", "--environment", "dev"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.pins("dev")["api"], digest_for("api", "one"))
        self.assertEqual(self.pins("dev")["frontend"], digest_for("frontend", "one"))

    def test_rollback_fails_when_only_one_promotion_exists(self):
        self.apply_dev("one")
        rc = self.promote.main(["rollback", "--environment", "dev"])
        self.assertEqual(rc, 1)

    def test_rollback_fails_when_history_is_empty(self):
        rc = self.promote.main(["rollback", "--environment", "dev"])
        self.assertEqual(rc, 1)

    def test_production_rollback_requires_confirmation(self):
        self.apply_dev("one")
        self.apply_production()
        self.apply_dev("two")
        self.apply_production()
        rc = self.promote.main(["rollback", "--environment", "production"])
        self.assertEqual(rc, 1)
        rc = self.promote.main(
            ["rollback", "--environment", "production", "--confirm", "production"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.pins("production")["api"], digest_for("api", "one"))


class ManifestRejection(IsolateOverlays):
    def test_missing_manifest(self):
        rc = self.promote.main(
            ["apply", "--environment", "dev", "--from", str(self.tmpdir / "absent.json")]
        )
        self.assertEqual(rc, 1)

    def test_manifest_missing_a_service(self):
        manifest = self.write_manifest(omit="worker")
        rc = self.promote.main(
            ["apply", "--environment", "dev", "--from", str(manifest)]
        )
        self.assertEqual(rc, 1)

    def test_manifest_with_a_tag_but_no_digest(self):
        path = self.tmpdir / "tag-only.json"
        images = [
            {
                "service": service,
                "repository": f"localhost:5001/{service}",
                "tag": "latest",
            }
            for service in SERVICES
        ]
        path.write_text(json.dumps({"images": images}) + "\n", encoding="utf-8")
        rc = self.promote.main(["apply", "--environment", "dev", "--from", str(path)])
        self.assertEqual(rc, 1)

    def test_manifest_with_a_malformed_digest(self):
        path = self.tmpdir / "bad-digest.json"
        images = [
            {
                "service": service,
                "repository": f"localhost:5001/{service}",
                "digest": "sha256:not-a-digest",
            }
            for service in SERVICES
        ]
        path.write_text(json.dumps({"images": images}) + "\n", encoding="utf-8")
        rc = self.promote.main(["apply", "--environment", "dev", "--from", str(path)])
        self.assertEqual(rc, 1)

    def test_unknown_environment(self):
        manifest = self.write_manifest()
        rc = self.promote.main(
            ["apply", "--environment", "staging", "--from", str(manifest)]
        )
        self.assertEqual(rc, 1)

    def test_mixed_digest_and_tag_in_the_overlay_is_rejected(self):
        text = self.overlay("dev").read_text(encoding="utf-8")
        text = text.replace(
            "  - name: gitops-registry:5000/api\n    newTag: 0.0.0-bootstrap",
            f"  - name: gitops-registry:5000/api\n    digest: {DIGEST_A}",
            1,
        )
        self.overlay("dev").write_text(text, encoding="utf-8")
        with self.assertRaises(self.promote.PromoteError):
            self.promote.require_homogeneous(
                self.promote.pins_from_overlay("dev"), context="dev"
            )


class Gate(IsolateOverlays):
    def test_unchanged_production_passes_without_a_trailer(self):
        rc = self.promote.main(
            [
                "gate",
                "--base-overlay",
                str(self.overlay("production")),
                "--head-overlay",
                str(self.overlay("production")),
                "--confirm-text",
                "",
            ]
        )
        self.assertEqual(rc, 0)

    def test_production_digest_change_without_trailer_fails(self):
        base_copy = self.tmpdir / "production-base.yaml"
        base_copy.write_text(
            self.overlay("production").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.apply_dev("one")
        self.apply_production()
        rc = self.promote.main(
            [
                "gate",
                "--base-overlay",
                str(base_copy),
                "--head-overlay",
                str(self.overlay("production")),
                "--confirm-text",
                "Looks good to me.",
            ]
        )
        self.assertEqual(rc, 1)

    def test_production_digest_change_with_trailer_passes(self):
        base_copy = self.tmpdir / "production-base.yaml"
        base_copy.write_text(self.overlay("production").read_text(encoding="utf-8"), encoding="utf-8")
        self.apply_dev("one")
        self.apply_production()
        rc = self.promote.main(
            [
                "gate",
                "--base-overlay",
                str(base_copy),
                "--head-overlay",
                str(self.overlay("production")),
                "--confirm-text",
                "Ready.\n\nPROMOTION-CONFIRM: production\n",
            ]
        )
        self.assertEqual(rc, 0)


class PipelineMustNotDeploy(unittest.TestCase):
    """CI and the promotion tool propose Git changes. They do not apply them."""

    FORBIDDEN = PROMOTE.FORBIDDEN_CLUSTER_VERBS

    def test_promotion_script_does_not_call_the_cluster(self):
        source = (ROOT / "scripts" / "promote.py").read_text(encoding="utf-8")
        for verb in self.FORBIDDEN:
            with self.subTest(verb=verb):
                # The tuple that names the verbs is allowed to mention them.
                occurrences = source.count(verb)
                self.assertEqual(
                    occurrences,
                    1,
                    f"{verb} appears outside the forbidden-verb declaration",
                )

    def test_github_workflows_never_apply_to_a_cluster(self):
        workflow_dir = ROOT / ".github" / "workflows"
        for path in sorted(workflow_dir.glob("*.yml")):
            text = path.read_text(encoding="utf-8").lower()
            for verb in self.FORBIDDEN:
                with self.subTest(workflow=path.name, verb=verb):
                    self.assertNotIn(verb, text)

    def test_makefile_promote_targets_do_not_call_kubectl(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8").lower()
        self.assertNotIn("kubectl apply", makefile)
        self.assertNotIn("kubectl set", makefile)


class OverlayContract(unittest.TestCase):
    def test_committed_overlays_are_bootstrap_or_fully_digested(self):
        for environment in ("dev", "production"):
            pins = PROMOTE.pins_from_overlay(environment)
            PROMOTE.require_complete(pins, context=environment)
            PROMOTE.require_homogeneous(pins, context=environment)

    def test_confirmation_trailer_is_the_documented_token(self):
        self.assertEqual(PROMOTE.CONFIRM_TRAILER, "PROMOTION-CONFIRM: production")


if __name__ == "__main__":
    unittest.main()
