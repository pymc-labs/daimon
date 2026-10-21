"""Import the reviewed Postgres index and verify its AKS platform before provisioning."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DIGEST = "sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"
TAG = "18-alpine"
SOURCES = ("public.ecr.aws/docker/library/postgres", "docker.io/library/postgres")


def azure(*args):
    return subprocess.run(["az", *args], capture_output=True, text=True, check=False)


def complete(acr, repository):
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
    )
    if metadata.returncode:
        return False
    actual = metadata.stdout.strip()
    if actual != DIGEST:
        raise ValueError(
            f"Postgres digest mismatch: expected {DIGEST}, got {actual!r}; refusing provisioning"
        )
    index = azure(
        "acr", "manifest", "show", "-r", acr, "-n", f"{repository}@{DIGEST}", "-o", "json"
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
                "acr", "manifest", "show", "-r", acr, "-n", f"{repository}@{child}", "-o", "json"
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
    if complete(acr, repository):
        print(f"Pinned Postgres {repository}:{TAG} is already complete; skipping import")
    else:
        for source in SOURCES:
            for attempt in range(1, 4):
                print(f"Importing pinned Postgres from {source} (attempt {attempt}/3)", flush=True)
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
                )
                if result.returncode == 0:
                    if complete(acr, repository):
                        break
                    print(
                        "Postgres index or linux/amd64 manifest is missing; retrying with --force",
                        file=sys.stderr,
                    )
                else:
                    # Do not echo raw CLI output or optional operator credentials.
                    transient = re.search(
                        r"\b(?:429|5\d\d)\b|TooManyRequests|Rate.?exceed",
                        result.stderr,
                        re.IGNORECASE,
                    )
                    if not transient:
                        print(
                            f"Postgres import from {source} failed; trying the next source",
                            file=sys.stderr,
                        )
                        break
                    print(f"Postgres source {source} returned a transient error", file=sys.stderr)
                if attempt < 3:
                    time.sleep(delay * 2 ** (attempt - 1))
            else:
                continue
            if result.returncode == 0 and complete(acr, repository):
                break
        else:
            raise ValueError(
                "Postgres import exhausted both sources or linux/amd64 remains missing; "
                "stopping before AKS creation"
            )
    (state / "postgres-digest").write_text(DIGEST + "\n")
    print(f"Verified Postgres {repository}:{TAG}: pinned index and linux/amd64 manifest present")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
