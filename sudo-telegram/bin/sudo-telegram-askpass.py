#!/usr/bin/env python3
"""
sudo-telegram-askpass — askpass client for the sudo-telegram daemon.

Invoked by sudo when SUDO_ASKPASS points here (and -A is given, or there
is no controlling terminal). Connects to the daemon's Unix socket, sends
the sudo invocation context, blocks waiting for the daemon's verdict.

On APPROVED → prints the password to stdout, exits 0.
On DENIED / ERROR / network failure → exits 1, sudo fails closed.
"""

import os
import socket
import sys

SOCKET = os.environ.get("STG_SOCKET", "/run/sudo-telegram/sock")
TIMEOUT = float(os.environ.get("STG_CLIENT_TIMEOUT", "75"))


def get_tty() -> str:
    try:
        if os.isatty(0):
            return os.ttyname(0)
    except OSError:
        pass
    return "notty"


def get_parent_cmdline() -> str:
    # sudo is suid-root, so it sets dumpable=0 — reading /proc/sudo_pid/cmdline
    # from this askpass (running as the original user) usually fails with
    # EACCES via the ptrace check. The daemon (running as root) does the
    # authoritative read; this is just a best-effort fallback.
    try:
        ppid = os.getppid()
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            raw = f.read()
        parts = [p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p]
        return " ".join(parts)
    except OSError:
        return ""


def main() -> int:
    if not os.path.exists(SOCKET):
        print(f"sudo-telegram: socket {SOCKET} not found", file=sys.stderr)
        return 1

    user = os.environ.get("SUDO_USER") or os.environ.get("USER", "unknown")
    sudo_cmd = os.environ.get("SUDO_COMMAND", "")
    parent_cmdline = get_parent_cmdline()
    cmd = sudo_cmd or parent_cmdline or "?"
    cwd = os.environ.get("PWD", "?")
    hostname = os.uname().nodename
    tty = get_tty()
    ppid = os.getppid()

    request = (
        f"VERSION 1\n"
        f"USER {user}\n"
        f"CMD {cmd}\n"
        f"PPID {ppid}\n"
        f"PWD {cwd}\n"
        f"HOSTNAME {hostname}\n"
        f"TTY {tty}\n"
        f"END\n"
    ).encode("utf-8")

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect(SOCKET)
        s.sendall(request)
        s.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
    except (socket.timeout, TimeoutError) as e:
        print(f"sudo-telegram: timeout waiting for daemon: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"sudo-telegram: socket error: {e}", file=sys.stderr)
        return 1
    finally:
        try:
            s.close()
        except OSError:
            pass

    response = b"".join(chunks).decode("utf-8", errors="replace")
    lines = response.splitlines()
    if not lines:
        print("sudo-telegram: empty response", file=sys.stderr)
        return 1

    verdict = lines[0]
    payload = "\n".join(lines[1:])

    if verdict == "APPROVED":
        sys.stdout.write(payload + "\n")
        sys.stdout.flush()
        return 0

    print(f"sudo-telegram: {verdict} — {payload}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
