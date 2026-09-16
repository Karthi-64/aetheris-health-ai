"""Double-fork daemonizer: start a command fully detached from the sandbox's
process group so it survives between tool calls.

Usage: python3 scripts/daemonize.py <logfile> <command> [args...]
"""

from __future__ import annotations

import os
import sys


def daemonize(command: list[str], logfile: str) -> int:
    pid = os.fork()
    if pid == 0:
        # First child: new session, detach from controlling terminal.
        os.setsid()
        pid2 = os.fork()
        if pid2 == 0:
            # Grandchild: reparented to init, survives the parent's death.
            devnull = os.open(os.devnull, os.O_RDONLY)
            out = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(devnull, 0)
            os.dup2(out, 1)
            os.dup2(out, 2)
            os.close(devnull)
            os.close(out)
            os.execvp(command[0], command)
        os._exit(0)
    # Parent: reap the first child, return the grandchild pid (unknown here).
    os.waitpid(pid, 0)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: daemonize.py <logfile> <command> [args...]", file=sys.stderr)
        sys.exit(2)
    sys.exit(daemonize(sys.argv[2:], sys.argv[1]))
