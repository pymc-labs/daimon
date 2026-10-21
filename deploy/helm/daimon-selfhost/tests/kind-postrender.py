"""Offline fixtures only: delay PG startup and inject the fake harness pre-install."""

import sys

import yaml

resources = list(yaml.safe_load_all(sys.stdin))
for resource in resources:
    if not resource or resource.get("kind") != "StatefulSet":
        continue
    pod = resource["spec"]["template"]["spec"]
    container = pod["containers"][0]
    if container["name"] == "postgres":
        pod["initContainers"] = [
            dict(
                name="delay-postgres",
                image=container["image"],
                command=["sh", "-ec", "sleep 20"],
                resources=dict(requests=dict(cpu="10m", memory="16Mi")),
            )
        ]
    elif container["name"] == "harness":
        container["args"] = [
            container["args"][0].replace("python -m daimon_harness", "python /validation/fake.py")
        ]
        container["volumeMounts"].append(
            dict(name="offline", mountPath="/validation", readOnly=True)
        )
        pod.setdefault("volumes", []).append(
            dict(name="offline", configMap=dict(name="offline-harness", defaultMode=0o400))
        )
yaml.safe_dump_all(resources, sys.stdout, sort_keys=False)
