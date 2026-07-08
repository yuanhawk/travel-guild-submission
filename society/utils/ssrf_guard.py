"""ssrf_guard.py — shared SSRF-001 outbound-URL guard.

CONTEXT — a security audit found this guard existed ONLY in budget_agent.py
(as `_validate_merchant_url`, called once at import time) even though
accommodation_agent.py and critic_agent.py read the identical
MERCHANT_MCP_URL env var and POST to it directly, with no equivalent check.
Each of these is a SEPARATE process/container reading its own environment, so
a guard running in budget_agent's process gave zero protection to the other
two — an operator/deployment misconfiguration that pointed MERCHANT_MCP_URL at
a cloud metadata endpoint (169.254.169.254) on the accommodation- or
critic-agent container would sail straight through.

THE FIX: extracted here so all three agents import and call the SAME
function, both at import time (catch a bad startup config immediately) and
immediately before every outbound POST (closes a DNS-rebinding TOCTOU gap —
a hostname that resolves benignly at import time could later be rebound to
169.254.169.254; re-checking right before each request means the window an
attacker would need to win shrinks to "between this check and this request",
not "for the life of the process").

SCOPE: covers BOTH IPv4 and IPv6 link-local/metadata ranges via the stdlib
`ipaddress` module (the original budget_agent.py-only check only compared
`resolved_ip.startswith("169.254.")` against an IPv4-only `socket.gethostbyname`
result, so an IPv6-only or dual-stack metadata endpoint was never checked at
all).

RFC-1918 private ranges (10.x, 172.16-31.x, 192.168.x) and loopback are
DELIBERATELY ALLOWED — the Go merchant service and local dev tooling live on
private/loopback addresses by design. An unresolvable hostname (cluster-
internal DNS not reachable from this box, e.g. in a unit-test environment) is
also allowed — fail-open for "we genuinely can't tell", not fail-closed on a
DNS lookup limitation unrelated to SSRF.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def validate_outbound_url(url: str, *, param_name: str = "URL") -> None:
    """
    Raise ValueError if `url` is unsafe for a server process to fetch:
      - scheme must be http or https
      - if the hostname resolves (IPv4 or IPv6), every resolved address must
        not be link-local (covers 169.254.0.0/16 IMDS and IPv6 fe80::/10)

    NEVER raises for an unresolvable hostname (allowed — see module docstring)
    or for private/loopback addresses (allowed by design).
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"{param_name} scheme must be http or https, got: {parts.scheme!r}")
    hostname = parts.hostname or ""
    try:
        # getaddrinfo (unlike gethostbyname) resolves BOTH IPv4 and IPv6.
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return  # unresolvable cluster-internal hostname -> allow
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
        # An IPv4-mapped IPv6 address (::ffff:169.254.169.254) is NOT itself
        # in fe80::/10, so check the mapped IPv4 address too.
        mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
        if ip.is_link_local or (mapped is not None and mapped.is_link_local):
            raise ValueError(
                f"{param_name} resolves to a link-local/metadata address {ip} — "
                f"SSRF block (cloud IMDS endpoints live in this range, e.g. "
                f"169.254.169.254)"
            )
