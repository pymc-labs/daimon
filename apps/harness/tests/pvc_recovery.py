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
