"""Mapping adapter runs the existing recovery fault injector against real PVC writes."""

import time
import weakref
from collections.abc import MutableMapping
from contextlib import ExitStack
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
        return sum(1 for _ in self)

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

        class FakeAPI:
            def __init__(self):
                self.items = {}

            async def list(self, owner):
                return list(self.items.values())

        self.kube = FakeAPI()
        self.controller = Controller(self.kube, {}, "recovery", warm=0)
        self.app = create_app(
            controller=self.controller, root=Path(self.directory.name), token="host"
        )
        # A managed client owns one portal/event loop for ALL requests; contended
        # controller locks cannot move between per-request event loops.
        lifetime = ExitStack()
        self._cleanup = weakref.finalize(self, lifetime.close)
        self.client = lifetime.enter_context(TestClient(self.app, base_url="https://test"))
        self.prefixes = set()

    def close(self):
        self._cleanup()
        self.directory.cleanup()

    def remote(self, name):
        from daimon_harness.controller import SESSION, TEMPLATE, TOUCHED
        from daimon_harness.pvc_store import ControllerStore

        prefix = "/".join(name.split("/")[:2]) + "/"
        self.prefixes.add(prefix)
        key = name.split("/")[1]
        pod = {
            "metadata": {
                "name": key,
                "uid": key,
                "labels": {SESSION: key, TEMPLATE: self.controller.version},
                "annotations": {TOUCHED: str(time.time())},
            }
        }
        self.kube.items[key] = pod
        token = self.controller.credential(pod, "checkpoint")
        self.controller.capabilities[token] = pod
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
