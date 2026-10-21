"""Exercise the real importer against an offline Azure CLI state machine."""

import contextlib
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

CHART = Path(__file__).resolve().parents[1]
PIN = "sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"


class PostgresImportTests(unittest.TestCase):
    def run_import(self, mode, credentials=False, settings=None):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            # Run the real bootstrap/importer with virtual import durations and sleeps.
            python = state / "python3"
            python.write_text(
                "#!"
                + sys.executable
                + "\n"
                + r"""
import os, sys
if not sys.argv[1].endswith('import-postgres.py'):
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
import runpy, subprocess, time, random
from pathlib import Path
from unittest.mock import patch
clock = Path(os.environ['AKS_STATE']) / 'clock'
def now():
    return float(clock.read_text()) if clock.exists() else 0.0
def advance(seconds):
    clock.write_text(str(now() + seconds))
real_popen = subprocess.Popen
class TimedPopen:
    def __init__(self, args, **kwargs):
        self.args = args
        self.first = True
        self.importing = args[:3] == ['az', 'acr', 'import']
        source = args[args.index('--source')+1] if self.importing else ''
        self.stall = self.importing and (
            os.environ['MODE'] == 'both-stall'
            or (os.environ['MODE'] == 'ecr-stall' and source.startswith('public.ecr.aws'))
            or (os.environ['MODE'] == 'command-timeout' and '--password' in args))
        if self.stall:
            with (clock.parent/'calls').open('a') as out:
                import json
                out.write(json.dumps(args[1:]) + '\n')
            # Keep process-group cleanup real, but advance a virtual timeout instantly.
            args = [sys.executable, '-c', 'import time; time.sleep(60)']
        self.process = real_popen(args, **kwargs)
    def __getattr__(self, name):
        return getattr(self.process, name)
    def communicate(self, timeout=None):
        if self.first and self.importing:
            self.first = False
            with (clock.parent/'timeouts').open('a') as out:
                out.write(str(timeout) + '\n')
            if self.stall:
                advance(timeout)
                raise subprocess.TimeoutExpired(self.args, timeout)
            advance(25)
        return self.process.communicate(timeout=timeout)
def sleeping(seconds):
    with (clock.parent/'sleeps').open('a') as out:
        out.write(str(seconds) + '\n')
    advance(seconds)
with patch.object(time, 'monotonic', now), patch.object(time, 'sleep', sleeping), \
     patch.object(random, 'uniform', lambda a,b: 1.0), patch.object(subprocess, 'Popen', TimedPopen):
    runpy.run_path(sys.argv[1], run_name='__main__')
"""
            )
            python.chmod(0o700)
            az = state / "az"
            az.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
p = Path(os.environ['AKS_STATE'])
mode = os.environ['MODE']
with (p/'calls').open('a') as out:
    out.write(json.dumps(args) + '\\n')
count = int((p/'count').read_text()) if (p/'count').exists() else 0
if args[:2] == ['acr', 'run'] and '--no-wait' in args:
    print('WARNING: Queued a run with ID: offline-run', file=sys.stderr)
elif args[:3] == ['acr', 'task', 'show-run']:
    print('Succeeded')
elif args[:2] == ['aks', 'create']:
    assert (p/'postgres-digest').exists(), 'AKS creation preceded verified Postgres'
    sys.exit(73)
elif args[:2] == ['acr', 'import']:
    count += 1
    (p/'count').write_text(str(count))
    source = args[args.index('--source') + 1]
    elapsed = float((p/'clock').read_text())
    if mode in ('timing', 'docker-timing'):
        mirror = source.startswith('public.ecr.aws')
        if (mirror and (elapsed < 120 or mode == 'docker-timing')) or (not mirror and elapsed < 480):
            print('StatusCode: 503' if mirror else 'StatusCode: 429', file=sys.stderr)
            sys.exit(1)
        (p/'success').touch()
    if mode == 'sanitized' and count == 1:
        print('StatusCode: 429 password=hiddenpass token=othertoken Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456 \\n https://user:passwd@example.com/?sig=shortsig ' + os.environ.get('DOCKERHUB_TOKEN', ''), file=sys.stderr)
        sys.exit(1)
    if ((mode in ('429', '503', 'command-timeout') and count == 1)
        or (mode == 'fallback' and source.startswith('public.ecr.aws'))):
        print('StatusCode: ' + ('503' if mode == '503' else '429'), file=sys.stderr)
        sys.exit(1)
elif args[:3] == ['acr', 'repository', 'show']:
    image = args[args.index('--image') + 1]
    if image != 'postgres:18-alpine':
        sys.exit(0)
    if mode in ('timing', 'docker-timing') and not (p/'success').exists():
        sys.exit(1)
    if count == 0 and mode not in ('skip', 'partial', 'old-tag'):
        sys.exit(1)
    print('sha256:' + '0'*64 if mode == 'mismatch' or (mode == 'old-tag' and count == 0) else os.environ['PIN'])
elif args[:3] == ['acr', 'manifest', 'show']:
    name = args[args.index('-n') + 1]
    if name.endswith('@' + os.environ['PIN']):
        print(json.dumps({'manifests': [{'digest': 'sha256:' + 'a'*64,
                          'platform': {'os': 'linux', 'architecture': 'amd64'}}]}))
    else:
        if (mode == 'partial' and count == 0) or (mode == 'partial-success' and count == 1) or mode == 'incomplete':
            sys.exit(1)
        print(json.dumps({'config': {'digest': 'sha256:' + 'b'*64}, 'layers': []}))
""")
            az.chmod(0o700)
            command = (
                ["bash", str(CHART / "scripts/aks.sh"), "bootstrap"]
                if mode in ("timing", "docker-timing", "ecr-stall", "both-stall")
                else [str(python), str(CHART / "scripts/import-postgres.py")]
            )
            env = dict(
                os.environ,
                PATH=directory + os.pathsep + os.environ["PATH"],
                ACR="offline",
                AKS_STATE=directory,
                MODE=mode,
                PIN=PIN,
                POSTGRES_IMPORT_RETRY_SECONDS="5",
                POSTGRES_IMPORT_BUDGET_SECONDS="900",
                RG="offline",
                LOCATION="offline",
                AKS="offline",
                AOAI="offline",
                TAG="offline",
                DOCKERHUB_USERNAME="example-user" if credentials else "",
                DOCKERHUB_TOKEN="local-private-token" if credentials else "",
            )
            for key in (
                "POSTGRES_IMPORT_BUDGET_SECONDS",
                "POSTGRES_IMPORT_ATTEMPT_SECONDS",
                "POSTGRES_IMPORT_RETRY_SECONDS",
            ):
                env.pop(key, None)
            env.update(settings or {})
            result = subprocess.run(command, env=env, capture_output=True, text=True)
            calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
            if result.returncode in (0, 73):
                self.assertEqual((state / "postgres-digest").read_text().strip(), PIN)
            else:
                self.assertFalse((state / "postgres-digest").exists())
            elapsed = float((state / "clock").read_text()) if (state / "clock").exists() else 0
            timeouts = (
                [float(x) for x in (state / "timeouts").read_text().splitlines()]
                if (state / "timeouts").exists()
                else []
            )
            sleeps = (
                [float(x) for x in (state / "sleeps").read_text().splitlines()]
                if (state / "sleeps").exists()
                else []
            )
            if mode == "both-stall":
                self.assertEqual(timeouts, [180, 180, 180, 180, 105])
            if mode == "docker-timing":
                self.assertEqual(max(sleeps), 60)
            if mode == "ecr-stall":
                self.assertEqual(
                    timeouts[0], float((settings or {}).get("POSTGRES_IMPORT_ATTEMPT_SECONDS", 180))
                )
            return result, calls, elapsed

    def test_stalled_ecr_falls_back_before_budget_and_aks_creation(self):
        for settings in (None, {"POSTGRES_IMPORT_ATTEMPT_SECONDS": "45"}):
            with self.subTest(settings=settings):
                result, calls, elapsed = self.run_import("ecr-stall", settings=settings)
                self.assertEqual(result.returncode, 73, result.stderr)
                self.assertLess(elapsed, 240)
                imports = [c for c in calls if c[:2] == ["acr", "import"]]
                self.assertEqual(len(imports), 2)
                self.assertIn("docker.io/library/postgres@" + PIN, imports[1])
                self.assertIn("timed out", result.stderr)

    def test_both_stalled_sources_exhaust_default_budget_without_aks_creation(self):
        result, calls, elapsed = self.run_import("both-stall")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(elapsed, 900)
        self.assertIn("budget exhausted", result.stderr)
        self.assertFalse(any(c[:2] == ["aks", "create"] for c in calls))

    def test_timeout_kills_cli_and_its_children(self):
        module = runpy.run_path(str(CHART / "scripts/import-postgres.py"))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            az = state / "az"
            az.write_text(
                "#!"
                + sys.executable
                + "\n"
                + """
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
(Path(os.environ['AKS_STATE'])/'pids').write_text(str(os.getpid()) + ' ' + str(child.pid))
time.sleep(60)
"""
            )
            az.chmod(0o700)
            try:
                with patch.dict(
                    os.environ,
                    PATH=directory + os.pathsep + os.environ["PATH"],
                    AKS_STATE=directory,
                    POSTGRES_IMPORT_ATTEMPT_SECONDS="1",
                ):
                    result = module["azure"](
                        "acr",
                        "import",
                        "--password",
                        "fake-cleanup-token",
                        deadline=time.monotonic() + 10,
                    )
                self.assertEqual(result.returncode, 124)
                self.assertNotIn("fake-cleanup-token", result.stderr)
                parent, child = map(int, (state / "pids").read_text().split())
                self.assertFalse(Path(f"/proc/{parent}").exists(), "CLI parent was not reaped")
                child_stat = Path(f"/proc/{child}/stat")
                if child_stat.exists():
                    self.assertEqual(child_stat.read_text().split()[2], "Z", "CLI child still runs")
            finally:
                if (state / "pids").exists():
                    parent = int((state / "pids").read_text().split()[0])
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(parent, signal.SIGKILL)

    def test_defaults_are_pinned(self):
        module = runpy.run_path(str(CHART / "scripts/import-postgres.py"))
        self.assertEqual(module["DEFAULT_BUDGET_SECONDS"], 900)
        self.assertEqual(module["DEFAULT_ATTEMPT_SECONDS"], 180)
        self.assertEqual(module["MAX_BACKOFF_SECONDS"], 60)

    def test_transient_mirror_errors_retry_then_succeed(self):
        for error in ("429", "503"):
            with self.subTest(error=error):
                result, calls, _ = self.run_import(error)
                self.assertEqual(result.returncode, 0, result.stderr)
                imports = [c for c in calls if c[:2] == ["acr", "import"]]
                self.assertEqual(len(imports), 2)
                self.assertIn("public.ecr.aws/docker/library/postgres@" + PIN, imports[0])
                self.assertIn("docker.io/library/postgres@" + PIN, imports[1])
                self.assertTrue(all("--force" in c for c in imports))

    def test_partial_index_missing_child_is_repaired(self):
        for mode, expected in (("partial", 1), ("partial-success", 2)):
            with self.subTest(mode=mode):
                result, calls, _ = self.run_import(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum(c[:2] == ["acr", "import"] for c in calls), expected)
                child_queries = [c for c in calls if "postgres@sha256:" + "a" * 64 in c]
                self.assertGreaterEqual(len(child_queries), 2)

    def test_digest_mismatch_is_refused(self):
        result, calls, _ = self.run_import("mismatch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("digest mismatch", result.stderr)
        self.assertEqual(sum(c[:2] == ["acr", "import"] for c in calls), 1)

    def test_existing_wrong_tag_is_repaired_from_pin(self):
        result, calls, _ = self.run_import("old-tag")
        self.assertEqual(result.returncode, 0, result.stderr)
        imports = [c for c in calls if c[:2] == ["acr", "import"]]
        self.assertEqual(len(imports), 1)
        self.assertIn("--force", imports[0])

    def test_b_timing_replay_and_eight_minute_docker_delay_precede_aks(self):
        for mode, minimum in (("timing", 120), ("docker-timing", 480)):
            with self.subTest(mode=mode):
                result, calls, elapsed = self.run_import(mode)
                self.assertEqual(result.returncode, 73, result.stderr)
                self.assertGreaterEqual(elapsed, minimum)
                self.assertLess(elapsed, 900)
                imports = [c for c in calls if c[:2] == ["acr", "import"]]
                for index, call in enumerate(imports):
                    self.assertIn(
                        (
                            "public.ecr.aws/docker/library/postgres@"
                            if index % 2 == 0
                            else "docker.io/library/postgres@"
                        )
                        + PIN,
                        call,
                    )
                cluster = next(c for c in calls if c[:2] == ["aks", "create"])
                self.assertLess(calls.index(imports[-1]), calls.index(cluster))

    def test_error_excerpt_is_one_line_sanitized_and_credentials_only_go_to_docker(self):
        result, calls, _ = self.run_import("sanitized", credentials=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for secret in (
            "hiddenpass",
            "othertoken",
            "abcdefghijklmnopqrstuvwxyz123456",
            "passwd",
            "shortsig",
            "local-private-token",
            "example-user",
        ):
            self.assertNotIn(secret, result.stderr + result.stdout)
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("public.ecr.aws/docker/library/postgres attempt 1", lines[0])
        self.assertIn("StatusCode: 429", lines[0])
        self.assertIn("[REDACTED]", lines[0])
        imports = [c for c in calls if c[:2] == ["acr", "import"]]
        self.assertNotIn("--password", imports[0])
        self.assertEqual(imports[1][imports[1].index("--password") + 1], "local-private-token")
        self.assertEqual(imports[1][imports[1].index("--username") + 1], "example-user")

    def test_command_timeout_does_not_print_credentials(self):
        result, _, _ = self.run_import("command-timeout", credentials=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("timed out", result.stderr)
        self.assertNotIn("local-private-token", result.stderr + result.stdout)

    def test_existing_complete_pin_skips_import(self):
        result, calls, _ = self.run_import("skip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c[:2] == ["acr", "import"] for c in calls))
        self.assertTrue(any("postgres@sha256:" + "a" * 64 in c for c in calls))

    def test_sources_alternate_after_mirror_failure(self):
        result, calls, _ = self.run_import("fallback")
        self.assertEqual(result.returncode, 0, result.stderr)
        imports = [c for c in calls if c[:2] == ["acr", "import"]]
        self.assertEqual(len(imports), 2)
        self.assertIn("docker.io/library/postgres@" + PIN, imports[-1])

    def test_persistently_missing_platform_exhausts_both_sources(self):
        result, calls, _ = self.run_import("incomplete")
        self.assertEqual(result.returncode, 1)
        self.assertIn("stopping before AKS creation", result.stderr)
        self.assertGreater(sum(c[:2] == ["acr", "import"] for c in calls), 6)


if __name__ == "__main__":
    unittest.main()
