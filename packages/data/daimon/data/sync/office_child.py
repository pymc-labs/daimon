# pyright: basic
"""The child process of `office_sandbox`.

`python -I -S office_child.py <mime> <memory> < file > text`.

A bare interpreter (about 20 ms) loads only `office.py` by path, which needs the standard
library alone; the app's own imports (a second or so) are not paid for each file.
"""

import contextlib
import importlib.util
import os
import resource
import sys


def _office_text():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "office.py")
    spec = importlib.util.spec_from_file_location("office", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.office_text


def main() -> int:
    mime, memory = sys.argv[1], int(sys.argv[2])
    office_text = _office_text()
    with open("/proc/self/statm") as f:
        used = int(f.read().split()[0]) * os.sysconf("SC_PAGE_SIZE")
    # A platform without RLIMIT_AS: the parent's wall-clock limit holds.
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_AS, (used + memory, used + memory))
    try:
        text = office_text(mime, sys.stdin.buffer.read())
    except BaseException:  # any failure, MemoryError included, is no text
        return 1
    sys.stdout.buffer.write(text.encode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
