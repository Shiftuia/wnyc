"""Network isolation probe: run INSIDE the served container to prove what it can and cannot reach.

    docker exec wnyc .venv/bin/python scripts/isolation_probe.py

Every line is one attempt with the expected outcome; the exit code is 1 if any expectation fails.
Direct connections must all fail (internal network, no host bridge address). Through the egress proxy only the
allowlisted API hosts may be reached; any private, loopback or link-local destination is refused.
"""
from __future__ import annotations

import os
import socket
import sys
import urllib.parse

TIMEOUT = 3.0
PROXY = urllib.parse.urlparse(os.environ.get("HTTPS_PROXY") or "http://wnyc-egress:3128")

# host:port that must NOT be reachable directly from the app container
DIRECT_BLOCKED = [
    ("172.18.0.1", 22, "host sshd via the proxy-bridge gateway"),
    ("172.18.0.1", 80, "host :80 (Traefik) via the proxy-bridge gateway"),
    ("172.17.0.1", 22, "host via docker0 gateway"),
    ("192.168.0.100", 22, "host LAN address, sshd"),
    ("192.168.0.100", 80, "host LAN address, Traefik"),
    ("192.168.0.1", 80, "LAN router"),
    ("169.254.169.254", 80, "cloud metadata"),
    ("1.1.1.1", 443, "internet by IP, bypassing the proxy"),
    ("traefik", 80, "Traefik by name"),
    ("umami", 3000, "another service backend (umami)"),
    ("umami-db", 5432, "another service database"),
    ("host.docker.internal", 22, "host via host-gateway alias"),
    ("example.com", 443, "internet by name, bypassing the proxy"),
]
# CONNECT targets through the egress proxy: (target, expect_allowed, why)
VIA_PROXY = [
    ("api.anthropic.com:443", True, "Claude API (the app's model)"),
    ("nominatim.openstreetmap.org:443", True, "address lookup"),
    ("routing.openstreetmap.de:443", True, "street geometry for trip drawing"),
    ("example.com:443", False, "arbitrary internet host"),
    ("tiles.openfreemap.org:443", False, "not needed: basemap is served offline"),
    ("api.anthropic.com:22", False, "allowed host, wrong port"),
    ("169.254.169.254:80", False, "cloud metadata"),
    ("169.254.169.254:443", False, "cloud metadata on 443"),
    ("192.168.0.100:22", False, "host LAN address"),
    ("192.168.0.1:443", False, "LAN router"),
    ("172.18.0.1:22", False, "host via proxy-bridge gateway"),
    ("127.0.0.1:3128", False, "the proxy itself (loopback)"),
    ("localhost:22", False, "loopback by name"),
    ("umami:3000", False, "another service backend"),
    ("metrics.shiftuia.com:443", False, "own public service (not allowlisted)"),
]


def direct(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return "CONNECTED"
    except socket.gaierror:
        return "no DNS"
    except OSError as e:
        return type(e).__name__ if not e.strerror else e.strerror


def via_proxy(target: str, method: str = "CONNECT") -> str:
    try:
        with socket.create_connection((PROXY.hostname, PROXY.port or 3128), timeout=TIMEOUT * 3) as s:
            if method == "CONNECT":
                s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            else:
                s.sendall(f"GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            line = s.recv(256).split(b"\r\n", 1)[0].decode(errors="replace")
            return line or "empty reply"
    except OSError as e:
        return f"proxy unreachable: {e}"


def main() -> int:
    bad = 0
    routes = open("/proc/net/route").read().splitlines()[1:]
    default = [r for r in routes if r.split()[1] == "00000000"]
    print(f"default route: {'NONE' if not default else default}")
    bad += bool(default)
    print("\n# direct connections (all must fail)")
    for host, port, why in DIRECT_BLOCKED:
        r = direct(host, port)
        ok = r != "CONNECTED"
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {host}:{port:<5} {r:<28} {why}")
    print(f"\n# through the egress proxy {PROXY.hostname}:{PROXY.port}")
    for target, allowed, why in VIA_PROXY:
        r = via_proxy(target)
        got = " 200 " in r + " "
        ok = got == allowed
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  CONNECT {target:<34} {r:<38} expect {'allowed' if allowed else 'denied'}: {why}")
    for target in ("169.254.169.254", "192.168.0.1", "example.com"):
        r = via_proxy(target, method="GET")
        ok = " 200 " not in r + " "
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'}  GET http://{target}/{'':<20} {r:<38} expect denied (plain HTTP)")
    print(f"\n{'ALL PASS' if not bad else f'{bad} FAILED'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
