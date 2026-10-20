"""Tiny exec wrapper: apply tool-only resource limits before starting bash."""

import ctypes
import os
import resource
import sys


def main() -> None:
    cpu, memory, processes = map(int, sys.argv[1:4])
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot disable tool privilege escalation")
    os.chdir(os.environ["HOME"])
    os.execv("/bin/bash", ["/bin/bash", "-c", sys.argv[4]])


if __name__ == "__main__":
    main()
