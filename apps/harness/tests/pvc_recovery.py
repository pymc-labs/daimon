"""Mapping adapter runs the existing recovery fault injector against real PVC writes."""

from collections.abc import MutableMapping
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from daimon_harness.pvc_store import PVCStore


class DiskObjects(MutableMapping):
    def __init__(self):
        self.directory = TemporaryDirectory(prefix="l16-recovery-")
        self.store = PVCStore(Path(self.directory.name))

    def __getitem__(self, key):
        output = BytesIO()
        self.store.download(key, output)
        return output.getvalue()

    def __setitem__(self, key, value):
        self.store.upload(key, BytesIO(value))

    def __delitem__(self, key):
        self.store.delete(key)

    def __iter__(self):
        return iter(self.store.list("sessions/"))

    def __len__(self):
        return len(list(self))

    def copy(self):
        return dict(self)


class HTTPObjects(DiskObjects):
    """Run recovery traces through controller HTTP auth/routes and ControllerStore.

    ASGI transport keeps the property deterministic; kind/AKS verify actual TLS.
    Each session prefix gets an independently scoped cached pod capability.
    """

    def __init__(self):
        super().__init__()
        from daimon_harness.controller import Controller, create_app
        from starlette.testclient import TestClient

        class NoAPI:
            async def list(self, owner):
                raise AssertionError("checkpoint HTTP path called Kubernetes")

        self.controller = Controller(NoAPI(), {}, "recovery", warm=0)
        self.app = create_app(
            controller=self.controller, root=Path(self.directory.name), token="host"
        )
        self.client = TestClient(self.app, base_url="https://test")
        self.prefixes = set()

    def remote(self, name):
        from daimon_harness.controller import SESSION
        from daimon_harness.pvc_store import ControllerStore

        prefix = "/".join(name.split("/")[:2]) + "/"
        self.prefixes.add(prefix)
        token = prefix + "capability"
        self.controller.capabilities[token] = {
            "metadata": {"labels": {SESSION: name.split("/")[1]}}
        }
        remote = object.__new__(ControllerStore)
        remote.client = self.client
        self.client.headers["Authorization"] = "Bearer " + token
        return remote

    def __getitem__(self, key):
        output = BytesIO()
        import httpx

        try:
            self.remote(key).download(key, output)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise KeyError(key) from None
            raise
        return output.getvalue()

    def __setitem__(self, key, value):
        self.remote(key).upload(key, BytesIO(value))

    def __delitem__(self, key):
        self.remote(key).delete(key)

    def __iter__(self):
        objects = []
        for prefix in list(self.prefixes):
            objects.extend(self.remote(prefix).list(prefix))
        return iter(sorted(objects))
