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
                f"--build-arg BUILDKIT_SYNTAX=docker/dockerfile:1.7.0@sha256:dbbd5e059e8a07ff7ea6233b213b36aa516b4c53c645f1817a4dd18b83cbea56 -t {image} -f {dockerfile} .",
            )
            self.assertIn("DOCKER_BUILDKIT=1", build["env"])
            self.assertEqual(push["push"], [image])
            self.assertNotIn("ignoreErrors", build)
            self.assertNotIn("ignoreErrors", push)
            # ACR defaults to sequential steps; never detach builds or pushes.
            self.assertNotIn("when", build)
            self.assertNotIn("when", push)
            self.assertNotIn("detach", build)

    def run_bootstrap(self, mode, tag="buildtest-14"):
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
    if os.environ['MODE'] == 'failed':
        sys.exit(1)
    if os.environ['MODE'] != 'no-receipt':
        print('WARNING: Queued a run with ID: offline-run', file=sys.stderr)
if args[:3] == ['acr', 'task', 'show-run']:
    mode = os.environ['MODE']
    if mode == 'poll-error':
        sys.exit(1)
    if mode == 'running-then-success':
        counter = Path(os.environ['AKS_STATE']) / 'poll-count'
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        print('Running' if count == 0 else 'Succeeded')
    else:
        print(mode.removeprefix('status-') if mode.startswith('status-') else 'Succeeded')
if args[:3] == ['acr', 'repository', 'show']:
    image = args[args.index('--image') + 1]
    if ((os.environ['MODE'] == 'missing-host' and image.startswith('daimon:'))
        or (os.environ['MODE'] == 'missing-harness' and image.startswith('daimon-harness:'))):
        sys.exit(1)
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
                TAG=tag,
                ACR_BUILD_WAIT_SECONDS="5" if mode == "running-then-success" else "1",
                ACR_BUILD_POLL_SECONDS="1",
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
            if result.returncode == 73:
                build = next(c for c in calls if c[:2] == ["acr", "run"] and "--help" not in c)
                self.assertEqual(
                    (state / "image-tag").read_text().strip(),
                    build[build.index("--set") + 1].removeprefix("tag="),
                )
                self.assertEqual((state / "acr-run-id").read_text().strip(), "offline-run")
            else:
                self.assertFalse((state / "image-tag").exists())
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

    def test_missing_image_after_successful_task_stops_before_import_and_cluster(self):
        for mode, missing_image in (
            ("missing-host", "daimon:buildtest-14"),
            ("missing-harness", "daimon-harness:buildtest-14"),
        ):
            with self.subTest(mode=mode):
                result, calls = self.run_bootstrap(mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn(
                    f"Required ACR image {missing_image} is missing or unreadable", result.stderr
                )
                self.assertTrue(any(c[:2] == ["acr", "run"] and "--help" not in c for c in calls))
                self.assertFalse(
                    any(c[:2] in (["aks", "create"], ["acr", "import"]) for c in calls)
                )
                checks = [c for c in calls if c[:3] == ["acr", "repository", "show"]]
                self.assertEqual(checks[-1][checks[-1].index("--image") + 1], missing_image)

    def test_unsuccessful_run_refuses_existing_tags_and_stops_before_provisioning(self):
        for status in (
            "Failed",
            "Canceled",
            "Error",
            "Timeout",
            "Running",
            "Queued",
            "Started",
            "",
        ):
            with self.subTest(status=status):
                result, calls = self.run_bootstrap("status-" + status)
                self.assertEqual(result.returncode, 1)
                self.assertIn("stopping before provisioning", result.stderr)
                # Stub repository lookups always succeed: old images cannot bypass run status.
                self.assertFalse(any(c[:3] == ["acr", "repository", "show"] for c in calls))
                self.assertFalse(
                    any(c[:2] in (["aks", "create"], ["acr", "import"]) for c in calls)
                )

    def test_missing_queue_receipt_stops_before_poll_or_provisioning(self):
        result, calls = self.run_bootstrap("no-receipt")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no valid run ID", result.stderr)
        self.assertFalse(any(c[:3] == ["acr", "task", "show-run"] for c in calls))
        self.assertFalse(any(c[:2] in (["aks", "create"], ["acr", "import"]) for c in calls))

    def test_failed_status_lookup_stops_before_provisioning(self):
        result, calls = self.run_bootstrap("poll-error")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot verify ACR run", result.stderr)
        self.assertFalse(any(c[:2] in (["aks", "create"], ["acr", "import"]) for c in calls))

    def test_running_then_succeeded_waits_for_terminal_success(self):
        result, calls = self.run_bootstrap("running-then-success")
        self.assertEqual(result.returncode, 73, result.stderr)
        polls = [c for c in calls if c[:3] == ["acr", "task", "show-run"]]
        self.assertEqual(len(polls), 2)

    def test_default_tag_is_unique_and_operator_tag_is_preserved(self):
        generated = []
        for _ in range(2):
            result, calls = self.run_bootstrap("success", tag="")
            self.assertEqual(result.returncode, 73, result.stderr)
            build = next(c for c in calls if c[:2] == ["acr", "run"] and "--help" not in c)
            generated.append(build[build.index("--set") + 1])
        self.assertNotEqual(*generated)
        self.assertTrue(all(value.startswith("tag=") for value in generated))

    def test_successful_remote_push_precedes_cluster_creation(self):
        result, calls = self.run_bootstrap("success")
        self.assertEqual(result.returncode, 73, result.stderr)
        build = next(c for c in calls if c[:2] == ["acr", "run"] and "--help" not in c)
        self.assertEqual(
            build[build.index("-f") + 1], "deploy/helm/daimon-selfhost/scripts/acr-build.yaml"
        )
        self.assertEqual(build[build.index("--set") + 1], "tag=buildtest-14")
        self.assertEqual(build[-1], ".")
        self.assertIn("--no-wait", build)

        self.assertNotIn("--no-logs", build)
        poll = next(c for c in calls if c[:3] == ["acr", "task", "show-run"])
        self.assertEqual(poll[poll.index("--run-id") + 1], "offline-run")
        self.assertEqual(poll[poll.index("--query") + 1], "status")
        cluster = next(c for c in calls if c[:2] == ["aks", "create"])
        imported = next(c for c in calls if c[:2] == ["acr", "import"])
        for image in ("daimon:buildtest-14", "daimon-harness:buildtest-14"):
            check = next(c for c in calls if c[:3] == ["acr", "repository", "show"] and image in c)
            self.assertEqual(check[check.index("-n") + 1], "offline")
            self.assertLess(calls.index(build), calls.index(poll))
            self.assertLess(calls.index(poll), calls.index(check))
            self.assertLess(calls.index(check), calls.index(imported))
        self.assertLess(calls.index(imported), calls.index(cluster))
        self.assertFalse(any(c[:2] == ["acr", "build"] for c in calls))


if __name__ == "__main__":
    unittest.main()
