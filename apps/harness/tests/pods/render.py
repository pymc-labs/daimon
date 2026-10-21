"""Render the lane-14 chart overlay for offline controller smoke tests."""

import json
import sys

import yaml

for doc in yaml.safe_load_all(sys.stdin):
    if not doc:
        continue
    if doc["kind"] == "Deployment" and doc["metadata"]["name"].endswith("-mcp"):
        spec = doc["spec"]["template"]["spec"]
        spec.pop("initContainers", None)
        spec["volumes"] = [{"name": "tls", "secret": {"secretName": "smoke-daimon-mcp-tls"}}]
        spec["containers"] = [
            {
                "name": "mcp",
                "image": __import__("os").environ["HARNESS_SMOKE_IMAGE"],
                "imagePullPolicy": "Never",
                "command": ["python", "/opt/l16-mcp.py"],
                "volumeMounts": [{"name": "tls", "mountPath": "/tls", "readOnly": True}],
                "readinessProbe": {"tcpSocket": {"port": 8765}},
            }
        ]
    if doc["kind"] == "ConfigMap" and "pod.json" in doc.get("data", {}):
        pod = json.loads(doc["data"]["pod.json"])
        pod["spec"]["containers"][0]["args"] = [
            "chmod 600 /dev/termination-log; exec python /opt/l16-smoke.py"
        ]
        if "--kind" in sys.argv:
            # Nested kind exposes host DMI files as extra bind mounts. Mask these
            # public identifiers so the production external-mount guard stays intact.
            pod["spec"]["volumes"].append({"name": "dmi", "emptyDir": {}})
            pod["spec"]["containers"][0]["volumeMounts"].append(
                {"name": "dmi", "mountPath": "/sys/devices/virtual/dmi/id", "readOnly": True}
            )
            pod["spec"]["initContainers"] = [
                {
                    "name": "kind-dmi",
                    "image": pod["spec"]["containers"][0]["image"],
                    "command": [
                        "sh",
                        "-ec",
                        "touch /dmi/product_name /dmi/product_uuid; chmod 700 /dmi",
                    ],
                    "volumeMounts": [{"name": "dmi", "mountPath": "/dmi"}],
                }
            ]
        doc["data"]["pod.json"] = json.dumps(pod)
    if doc["kind"] not in {"Namespace", "PriorityClass"}:
        doc["metadata"].setdefault("namespace", "daimon-pods")
    print(yaml.safe_dump(doc), end="---\n")
