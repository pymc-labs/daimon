"""Import the reviewed Postgres index and verify its AKS platform before provisioning."""

import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

DIGEST = "sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"
TAG = "18-alpine"
SOURCES = ("public.ecr.aws/docker/library/postgres", "docker.io/library/postgres")


def azure(*args, deadline=None):
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise TimeoutError("Postgres import budget exhausted; stopping before AKS creation")
    try:
        return subprocess.run(
            ["az", *args], capture_output=True, text=True, check=False, timeout=remaining
        )
    except subprocess.TimeoutExpired:
        # TimeoutExpired includes argv, which can contain the Docker Hub token.
        raise TimeoutError(
            "Postgres import command timed out; stopping before AKS creation"
        ) from None


def sanitized_excerpt(message):
    for name in ("DOCKERHUB_USERNAME", "DOCKERHUB_TOKEN"):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[REDACTED]")
            message = message.replace(json.dumps(secret)[1:-1], "[REDACTED]")
    message = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", message)
    message = re.sub(r"(?i)\b(?:Bearer|Basic)\s+\S+", "[REDACTED]", message)
    message = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[REDACTED]@", message)
    message = re.sub(
        r"(?i)(?:--)?\b(?:[a-z0-9_-]*[_-])?"
        r"(?:password|passwd|token|secret|api[_-]?key|sig|authorization|username)"
        r"[\"']?\s*(?:[:=]\s*|\s+)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        "[REDACTED]",
        message,
    )
    message = re.sub(r"[A-Za-z0-9_+/.=-]{24,}", "[REDACTED]", message)
    return " ".join(message.split())[:240] or "no error detail returned"


def complete(acr, repository, deadline=None, repair_mismatch=False):
    metadata = azure(
        "acr",
        "repository",
        "show",
        "-n",
        acr,
        "--image",
        f"{repository}:{TAG}",
        "--query",
        "digest",
        "-o",
        "tsv",
        deadline=deadline,
    )
    if metadata.returncode:
        return False
    actual = metadata.stdout.strip()
    if actual != DIGEST:
        if repair_mismatch:
            print("Existing Postgres tag has another digest; repairing from the pinned source")
            return False
        raise ValueError("Postgres digest mismatch after import; refusing provisioning")
    index = azure(
        "acr",
        "manifest",
        "show",
        "-r",
        acr,
        "-n",
        f"{repository}@{DIGEST}",
        "-o",
        "json",
        deadline=deadline,
    )
    if index.returncode:
        return False
    try:
        candidates = [
            item["digest"]
            for item in json.loads(index.stdout)["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        ]
        if not candidates:
            return False
        for child in candidates:
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", child):
                return False
            manifest = azure(
                "acr",
                "manifest",
                "show",
                "-r",
                acr,
                "-n",
                f"{repository}@{child}",
                "-o",
                "json",
                deadline=deadline,
            )
            if manifest.returncode:
                return False
            content = json.loads(manifest.stdout)
            if not content.get("config", {}).get("digest") or not isinstance(
                content.get("layers"), list
            ):
                return False
    except (ValueError, TypeError, KeyError, AttributeError):
        return False
    return True


def main():
    acr = os.environ["ACR"]
    state = Path(os.environ["AKS_STATE"])
    repository = os.environ.get("POSTGRES_IMPORT_REPOSITORY", "postgres")
    if not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", repository):
        raise ValueError("Invalid POSTGRES_IMPORT_REPOSITORY")
    delay = int(os.environ.get("POSTGRES_IMPORT_RETRY_SECONDS", "5"))
    if not 0 <= delay <= 60:
        raise ValueError("POSTGRES_IMPORT_RETRY_SECONDS must be between 0 and 60")
    budget = float(os.environ.get("POSTGRES_IMPORT_BUDGET_SECONDS", "900"))
    if not 0 < budget <= 86400:
        raise ValueError("POSTGRES_IMPORT_BUDGET_SECONDS must be between 0 and 86400")
    username, token = os.environ.get("DOCKERHUB_USERNAME"), os.environ.get("DOCKERHUB_TOKEN")
    if bool(username) != bool(token):
        raise ValueError("Set both DOCKERHUB_USERNAME and DOCKERHUB_TOKEN, or neither")
    deadline = time.monotonic() + budget
    if complete(acr, repository, deadline, repair_mismatch=True):
        print(f"Pinned Postgres {repository}:{TAG} is already complete; skipping import")
    else:
        attempt = 0
        backoff = delay
        while time.monotonic() < deadline:
            source = SOURCES[attempt % len(SOURCES)]
            attempt += 1
            print(f"Importing pinned Postgres from {source} (attempt {attempt})", flush=True)
            credentials = []
            if source.startswith("docker.io/") and username and token:
                credentials = ["--username", username, "--password", token]
            try:
                result = azure(
                    "acr",
                    "import",
                    "-n",
                    acr,
                    "--source",
                    f"{source}@{DIGEST}",
                    "--image",
                    f"{repository}:{TAG}",
                    "--force",
                    "-o",
                    "none",
                    *credentials,
                    deadline=deadline,
                )
            except TimeoutError as error:
                raise TimeoutError(f"Postgres import {source} attempt {attempt}: {error}") from None
            if result.returncode == 0:
                if complete(acr, repository, deadline):
                    break
                excerpt = (
                    "index or linux/amd64 manifest missing after import; will repair with --force"
                )
            else:
                excerpt = sanitized_excerpt(result.stderr or result.stdout)
            print(f"Postgres import {source} attempt {attempt}: {excerpt}", file=sys.stderr)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Postgres import budget exhausted; stopping before AKS creation")
            # Alternate sources throughout the shared budget. Jitter prevents synchronized retries.
            time.sleep(min(remaining, 60, random.uniform(0.8, 1.2) * backoff))
            backoff = min(60, backoff * 2)
        else:
            raise TimeoutError("Postgres import budget exhausted; stopping before AKS creation")
    (state / "postgres-digest").write_text(DIGEST + "\n")
    print(f"Verified Postgres {repository}:{TAG}: pinned index and linux/amd64 manifest present")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, TimeoutError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
