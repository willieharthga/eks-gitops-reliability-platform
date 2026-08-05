"""Static canary contract: a bad canary cannot be specified to take all traffic.

Runtime abort is `make canary-test`. This file asserts the properties of the
declared Rollout that make that abort possible: analysis runs before every
weight increase, no step sets 100%, and the analysis reads the API SLI.
"""

from __future__ import annotations

import unittest
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit(
        "PyYAML is required for the canary contract tests.\n"
        "Run 'make setup' (or 'pip install -r requirements-dev.txt') and retry."
    ) from exc

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "platform" / "workloads" / "base" / "api.yaml"
ANALYSIS = ROOT / "platform" / "workloads" / "base" / "api-analysis.yaml"
INSTALL = ROOT / "platform" / "rollouts" / "install" / "kustomization.yaml"


def documents(path: Path) -> list[dict]:
    return [document for document in yaml.safe_load_all(path.read_text(encoding="utf-8")) if document]


class CanarySpecification(unittest.TestCase):
    def rollout(self) -> dict:
        for document in documents(API):
            if document.get("kind") == "Rollout":
                return document
        raise AssertionError("api.yaml does not declare a Rollout")

    def steps(self) -> list[dict]:
        return self.rollout()["spec"]["strategy"]["canary"]["steps"]

    def test_the_installation_pins_an_exact_release(self):
        resources = documents(INSTALL)[0]["resources"]
        remote = [item for item in resources if str(item).startswith("github.com/")]
        self.assertTrue(remote)
        for item in remote:
            self.assertRegex(item, r"\?ref=v\d+\.\d+\.\d+$")

    def test_no_step_hands_the_canary_full_traffic(self):
        weights = [step["setWeight"] for step in self.steps() if "setWeight" in step]
        self.assertTrue(weights)
        self.assertLess(max(weights), 100)

    def test_every_weight_increase_is_followed_by_analysis(self):
        steps = self.steps()
        for index, step in enumerate(steps):
            if "setWeight" not in step:
                continue
            self.assertLess(
                index + 1,
                len(steps),
                "a setWeight step has no following analysis; the canary could promote unchecked",
            )
            self.assertIn("analysis", steps[index + 1])

    def test_analysis_uses_the_named_template(self):
        names = set()
        for step in self.steps():
            analysis = step.get("analysis")
            if not analysis:
                continue
            for template in analysis["templates"]:
                names.add(template["templateName"])
        self.assertEqual(names, {"api-canary-sli"})

    def test_the_analysis_template_exists_and_fails_closed_on_errors(self):
        template = documents(ANALYSIS)[0]
        self.assertEqual(template["kind"], "AnalysisTemplate")
        self.assertEqual(template["metadata"]["name"], "api-canary-sli")
        metrics = {metric["name"]: metric for metric in template["spec"]["metrics"]}
        self.assertIn("error-ratio", metrics)
        self.assertIn("p95-latency", metrics)
        error = metrics["error-ratio"]
        self.assertIn("result < 0.1", error["successCondition"])
        self.assertGreaterEqual(error["failureLimit"], 1)
        self.assertEqual(error["provider"]["web"]["jsonPath"], "{$.error_ratio}")

    def test_canary_pods_are_labelled_for_fault_isolation(self):
        canary = self.rollout()["spec"]["strategy"]["canary"]
        self.assertEqual(canary["canaryMetadata"]["labels"]["role"], "canary")
        self.assertEqual(canary["stableMetadata"]["labels"]["role"], "stable")

    def test_the_api_reads_pod_role_from_the_downward_api(self):
        containers = self.rollout()["spec"]["template"]["spec"]["containers"]
        env = {item["name"]: item for item in containers[0]["env"]}
        self.assertIn("POD_ROLE", env)
        self.assertEqual(
            env["POD_ROLE"]["valueFrom"]["fieldRef"]["fieldPath"],
            "metadata.labels['role']",
        )


if __name__ == "__main__":
    unittest.main()
