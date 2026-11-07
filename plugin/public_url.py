"""The address a gadget reached the gateway on, for the Hermes Desktop page's virtual gadget.

The page runs on the computer Hermes Desktop runs on, which need not be the one the gateway runs
on. The gateway's bind address (``0.0.0.0:8765``) says nothing about how another computer reaches
it: through a reverse proxy, a tunnel or a LAN address. A paired device that connected did reach
it, and its upgrade request says how: the ``Host`` header, or a proxy's ``Forwarded`` /
``X-Forwarded-*`` headers, and the request path. The gateway records that URL in ``status.json``
and the dashboard's ``/info`` route offers it to the page.

These headers come from the client. The URL is only a hint for the owner's own page about where
to connect; it grants nothing (a new device still needs its pairing code approved). Every value is
checked to be a plain host name or address and port before it becomes part of a URL.

Nothing here imports Hermes: the hub and the dashboard both use it.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Iterable, Mapping

MAX_URL = 300
# A DNS name (letters, digits, hyphens, dots) or a bracketed IPv6 address, then an optional port.
_HOSTPORT_RE = re.compile(r"^(?P<host>\[[0-9a-fA-F:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}))(?::(?P<port>[0-9]{1,5}))?$")
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
_SECURE = {"https": "wss", "wss": "wss", "http": "ws", "ws": "ws"}
_DEFAULT_PORT = {"ws": 80, "wss": 443}


def _first(value: Any) -> str:
    """The first entry of a header a chain of proxies may have appended to."""
    return str(value or "").split(",", 1)[0].strip()


def _forwarded(value: Any) -> dict[str, str]:
    """``proto`` and ``host`` from the first element of an RFC 7239 ``Forwarded`` header."""
    out: dict[str, str] = {}
    for pair in _first(value).split(";"):
        name, _, raw = pair.partition("=")
        name = name.strip().lower()
        if name in ("proto", "host") and raw:
            out[name] = raw.strip().strip('"').strip()
    return out


def split_host(hostport: str) -> tuple[str, int | None] | None:
    """``("example.com", 8443)`` from ``"example.com:8443"``; None for anything that isn't a plain
    host name or address with an optional port."""
    m = _HOSTPORT_RE.match(hostport.strip())
    if not m:
        return None
    host, port = m.group("host").lower(), m.group("port")
    if host.startswith("["):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            return None
    elif ".." in host or host.endswith("-"):
        return None
    if port is not None:
        if not 0 < int(port) < 65536:
            return None
        return host, int(port)
    return host, None


def is_loopback(host: str) -> bool:
    host = host.strip().strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build(scheme: str, host: str, port: int | None, path: str) -> str | None:
    """A ws:// or wss:// URL, without the scheme's default port; None if a part is not acceptable."""
    if scheme not in _DEFAULT_PORT or not _PATH_RE.match(path or ""):
        return None
    if split_host(host if port is None else f"{host}:{port}") is None:
        return None
    netloc = host if port is None or port == _DEFAULT_PORT[scheme] else f"{host}:{port}"
    url = f"{scheme}://{netloc}{path}"
    return url if len(url) <= MAX_URL else None


def origin(headers: Mapping[str, Any], *, tls: bool = False) -> tuple[str, str, int | None] | None:
    """``(scheme, host, port)`` a client used, from its request's headers: ``ws`` or ``wss``, the host
    name or address, and the port if one was given. A proxy's ``Forwarded`` header wins, then
    ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` / ``X-Forwarded-Port``, then ``Host``. Without a
    scheme from a proxy it is ``wss`` when the request itself came over TLS. None when the host is
    missing or malformed."""
    def get(name: str) -> str:
        try:
            return str(headers.get(name) or "")
        except Exception:
            return ""

    fwd = _forwarded(get("Forwarded"))
    proto = (fwd.get("proto") or _first(get("X-Forwarded-Proto"))).lower()
    hostport = fwd.get("host") or _first(get("X-Forwarded-Host")) or _first(get("Host"))
    scheme = _SECURE.get(proto) or ("wss" if tls else "ws")
    parts = split_host(hostport) if hostport else None
    if parts is None:
        return None
    host, port = parts
    if port is None and not fwd.get("host"):
        fwd_port = _first(get("X-Forwarded-Port"))
        if fwd_port.isdigit() and 0 < int(fwd_port) < 65536:
            port = int(fwd_port)
    return scheme, host, port


def from_headers(headers: Mapping[str, Any], path: str, *, tls: bool = False) -> str | None:
    """The URL a device used, from its upgrade request's headers and path (the query dropped). None
    when the headers don't say, or name a loopback address (another computer can't use that one)."""
    found = origin(headers, tls=tls)
    if found is None or is_loopback(found[1]):
        return None
    return build(*found, (path or "/").split("?", 1)[0])


def valid(url: Any) -> str | None:
    """``url`` if it is a ws:// or wss:// URL this module would build, else None (for values read
    back from a file or the config)."""
    if not isinstance(url, str) or len(url) > MAX_URL:
        return None
    m = re.match(r"^(wss?)://([^/]+)(/.*)$", url.strip())
    if not m:
        return None
    parts = split_host(m.group(2))
    if parts is None:
        return None
    return build(m.group(1), parts[0], parts[1], m.group(3))


def latest(seen: Iterable[Any]) -> str | None:
    """The newest valid URL of ``status.json``'s ``reached`` list (``{"url", "at"}`` records)."""
    best: tuple[float, str] | None = None
    for rec in seen or ():
        if not isinstance(rec, dict):
            continue
        url = valid(rec.get("url"))
        at = rec.get("at")
        try:
            at = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0
        except OverflowError:
            at = 0.0
        if at != at:  # NaN
            at = 0.0
        if url and (best is None or at > best[0]):
            best = (at, url)
    return best[1] if best else None
