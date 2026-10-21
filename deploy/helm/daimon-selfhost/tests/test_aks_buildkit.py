"""Offline bootstrap regression: remote BuildKit and push failure boundaries."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


class AksBuildkitTests(unittest.TestCase):
    def test_task_builds_and_pushes_both_production_images_with_buildkit(self):
        # ACR expands Go template expressions before parsing YAML.
        source = (CHART / "scripts/acr-build.yaml").read_text()
        task = yaml.safe_load(
            source.replace("{{.Run.Registry}}", "offline.azurecr.io").replace(
                "{{.Values.tag}}", "buildtest-14"
            )
        )
        steps = task["steps"]
        self.assertEqual(len(steps), 4)
        for index, (repo, dockerfile) in enumerate(
            (("daimon", "Dockerfile"), ("daimon-harness", "apps/harness/Dockerfile"))
        ):
            image = "offline.azurecr.io/" + repo + ":buildtest-14"
            build, push = steps[index * 2 : index * 2 + 2]
            self.assertEqual(
                build["build"],
                f"--build-arg BUILDKIT_SYNTAX=docker/dockerfile:1.7.0 -t {image} -f {dockerfile} .",
            )
            self.assertIn("DOCKER_BUILDKIT=1", build["env"])
            self.assertEqual(push["push"], [image])
            self.assertNotIn("ignoreErrors", build)
            self.assertNotIn("ignoreErrors", push)
            # ACR defaults to sequential steps; never detach builds or pushes.
            self.assertNotIn("when", build)
            self.assertNotIn("when", push)
            self.assertNotIn("detach", build)

    def run_bootstrap(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            stub = state / "az"
            stub.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['CALLS']).open('a') as out:
    out.write(json.dumps(args) + '\\n')
if args[:2] == ['acr', 'run']:
    if '--help' in args:
        sys.exit(1 if os.environ['MODE'] == 'missing' else 0)
    sys.exit(1 if os.environ['MODE'] == 'failed' else 0)
if args[:2] == ['aks', 'create']:
    sys.exit(73)  # Proof of ordering; never call a live cluster.
""")
            stub.chmod(0o755)
            env = dict(
                os.environ,
                PATH=directory + os.pathsep + os.environ["PATH"],
                RG="offline",
                LOCATION="offline",
                ACR="offline",
                AKS="offline",
                AOAI="offline",
                TAG="buildtest-14",
                AKS_STATE=directory,
                CALLS=str(state / "calls"),
                MODE=mode,
            )
            result = subprocess.run(
                ["bash", str(CHART / "scripts/aks.sh"), "bootstrap"],
                env=env,
                capture_output=True,
                text=True,
            )
            calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
            return result, calls

    def test_missing_acr_tasks_cli_fails_before_resource_creation(self):
        result, calls = self.run_bootstrap("missing")
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires Azure CLI with az acr run", result.stderr)
        self.assertEqual(calls, [["acr", "run", "--help"]])

    def test_failed_build_or_push_stops_before_import_and_cluster(self):
        result, calls = self.run_bootstrap("failed")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ACR Tasks BuildKit build/push failed", result.stderr)
        self.assertFalse(any(c[:2] in (["aks", "create"], ["acr", "import"]) for c in calls))

    def test_successful_remote_push_precedes_cluster_creation(self):
        result, calls = self.run_bootstrap("success")
        self.assertEqual(result.returncode, 73)
        build = next(c for c in calls if c[:2] == ["acr", "run"] and "--help" not in c)
        self.assertEqual(
            build[build.index("-f") + 1], "deploy/helm/daimon-selfhost/scripts/acr-build.yaml"
        )
        self.assertEqual(build[build.index("--set") + 1], "tag=buildtest-14")
        self.assertNotIn("--no-wait", build)
        self.assertNotIn("--no-logs", build)
        cluster = next(c for c in calls if c[:2] == ["aks", "create"])
        self.assertLess(calls.index(build), calls.index(cluster))
        self.assertFalse(any(c[:2] == ["acr", "build"] for c in calls))


if __name__ == "__main__":
    unittest.main()
