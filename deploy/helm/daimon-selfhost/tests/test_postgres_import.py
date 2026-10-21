"""Exercise the real importer against an offline Azure CLI state machine."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CHART = Path(__file__).resolve().parents[1]
PIN = "sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"


class PostgresImportTests(unittest.TestCase):
    def run_import(self, mode):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
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
if args[:2] == ['acr', 'import']:
    count += 1
    (p/'count').write_text(str(count))
    source = args[args.index('--source') + 1]
    if ((mode in ('429', '503') and count == 1)
        or (mode == 'fallback' and source.startswith('public.ecr.aws'))):
        print('StatusCode: ' + ('503' if mode == '503' else '429'), file=sys.stderr)
        sys.exit(1)
elif args[:3] == ['acr', 'repository', 'show']:
    if count == 0 and mode not in ('skip', 'partial'):
        sys.exit(1)
    print('sha256:' + '0'*64 if mode == 'mismatch' else os.environ['PIN'])
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
            result = subprocess.run(
                ["python3", str(CHART / "scripts/import-postgres.py")],
                env=dict(
                    os.environ,
                    PATH=directory + os.pathsep + os.environ["PATH"],
                    ACR="offline",
                    AKS_STATE=directory,
                    MODE=mode,
                    PIN=PIN,
                    POSTGRES_IMPORT_RETRY_SECONDS="0",
                ),
                capture_output=True,
                text=True,
            )
            calls = [json.loads(line) for line in (state / "calls").read_text().splitlines()]
            if result.returncode == 0:
                self.assertEqual((state / "postgres-digest").read_text().strip(), PIN)
            else:
                self.assertFalse((state / "postgres-digest").exists())
            return result, calls

    def test_transient_mirror_errors_retry_then_succeed(self):
        for error in ("429", "503"):
            with self.subTest(error=error):
                result, calls = self.run_import(error)
                self.assertEqual(result.returncode, 0, result.stderr)
                imports = [c for c in calls if c[:2] == ["acr", "import"]]
                self.assertEqual(len(imports), 2)
                self.assertTrue(
                    all("public.ecr.aws/docker/library/postgres@" + PIN in c for c in imports)
                )
                self.assertTrue(all("--force" in c for c in imports))

    def test_partial_index_missing_child_is_repaired(self):
        for mode, expected in (("partial", 1), ("partial-success", 2)):
            with self.subTest(mode=mode):
                result, calls = self.run_import(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum(c[:2] == ["acr", "import"] for c in calls), expected)
                child_queries = [c for c in calls if "postgres@sha256:" + "a" * 64 in c]
                self.assertGreaterEqual(len(child_queries), 2)

    def test_digest_mismatch_is_refused(self):
        result, calls = self.run_import("mismatch")
        self.assertEqual(result.returncode, 1)
        self.assertIn("digest mismatch", result.stderr)
        self.assertEqual(sum(c[:2] == ["acr", "import"] for c in calls), 1)

    def test_existing_complete_pin_skips_import(self):
        result, calls = self.run_import("skip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c[:2] == ["acr", "import"] for c in calls))
        self.assertTrue(any("postgres@sha256:" + "a" * 64 in c for c in calls))

    def test_mirror_exhaustion_falls_back_to_dockerhub(self):
        result, calls = self.run_import("fallback")
        self.assertEqual(result.returncode, 0, result.stderr)
        imports = [c for c in calls if c[:2] == ["acr", "import"]]
        self.assertEqual(len(imports), 4)
        self.assertIn("docker.io/library/postgres@" + PIN, imports[-1])

    def test_persistently_missing_platform_exhausts_both_sources(self):
        result, calls = self.run_import("incomplete")
        self.assertEqual(result.returncode, 1)
        self.assertIn("stopping before AKS creation", result.stderr)
        self.assertEqual(sum(c[:2] == ["acr", "import"] for c in calls), 6)


if __name__ == "__main__":
    unittest.main()
