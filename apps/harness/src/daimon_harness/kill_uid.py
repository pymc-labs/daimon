"""Run as the tool UID to kill descendants without relying on procfs."""

import os
import signal
import time

# Some kernels return success for kill(-1) even when only inaccessible UIDs
# remain. Bound retries as well as the parent's helper wait; never enumerate
# procfs, whose hidepid policy deliberately hides these processes from service.
deadline = time.monotonic() + 0.2
while True:
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        break
    if time.monotonic() >= deadline:
        break
    time.sleep(0.01)
