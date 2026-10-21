"""Render the lane-14 chart overlay for offline controller smoke tests."""

import json
import sys

import yaml

for doc in yaml.safe_load_all(sys.stdin):
    if not doc:
        continue
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
    print(yaml.safe_dump(doc), end="---\n")
