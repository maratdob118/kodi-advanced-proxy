# -*- coding: utf-8 -*-
"""TCP port helpers (Kodi-free).

Used to detect whether the configured proxy port is already taken - e.g. by a
previously installed sing-box / xray / shadowsocks / other proxy - and to pick
the next free port, so the addon always ends up with a working local listener
instead of crash-looping against an occupied port.
"""
import socket

DEFAULT_HOST = "127.0.0.1"
MAX_ATTEMPTS = 100


def port_in_use(port, host=DEFAULT_HOST):
    """True when something already listens on host:port.

    A bind() probe is used: binding the loopback address fails if ANY process
    holds the port (on 0.0.0.0 or on 127.0.0.1), which covers both a local
    engine and a LAN-exposed one.
    """
    if not (0 < port < 65536):
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def find_free_port(preferred, host=DEFAULT_HOST, max_attempts=MAX_ATTEMPTS):
    """Return the first free port >= preferred within max_attempts.

    Scans [preferred, preferred+max_attempts) and stops at the first free one.
    Falls back to `preferred` when nothing in the range is free (the engine
    will fail loudly in that case rather than silently using a wrong port).
    """
    top = min(preferred + max_attempts, 65536)
    for port in range(preferred, top):
        if not port_in_use(port, host):
            return port
    return preferred
