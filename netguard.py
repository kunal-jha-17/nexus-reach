"""
netguard.py -- stop the server being used to reach things it shouldn't.

The app fetches web pages named in people's lead lists and connects to mail
servers people type into Settings. On a shared server that's dangerous: a
crafted address could point at the host's internal network or cloud metadata
service (169.254.169.254). Everything that connects to an address someone else
chose goes through here first:

  * only http/https, only ordinary web ports
  * the name is resolved and EVERY address must be a public internet address
    (private, loopback, link-local, CGNAT, multicast, reserved are refused)
  * redirects are followed by hand, and each hop is checked again

Set ALLOW_PRIVATE_HOSTS=1 to switch this off (for testing against a mail
server on your own network). Not recommended on a shared server.

Known limit: the name is checked, then connected to by the HTTP library, so an
attacker who controls a DNS server could answer differently the second time
("DNS rebinding"). The fetched text is only mined for emails/phones and never
shown to the user, which limits what that could achieve.
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

import config

WEB_PORTS = {80, 443, 8080, 8443}
MAIL_PORTS = {25, 465, 587, 2525, 143, 993}


class Blocked(ValueError):
    """The address isn't allowed."""


def _allow_private():
    return config._server_env("ALLOW_PRIVATE_HOSTS", "").lower() in ("1", "true", "yes")


def resolve_public(host, port):
    """Resolve `host` and make sure every address it points to is public. Returns the addresses."""
    if not host:
        raise Blocked("No host name.")
    if _allow_private():
        return [host]
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise Blocked(f"Couldn't find '{host}'.")
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        # an IPv6 address that wraps an IPv4 one (::ffff:10.0.0.1) is judged by the IPv4 part
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise Blocked(f"'{host}' points to a private or internal address, which isn't allowed.")
        addrs.append(str(ip))
    if not addrs:
        raise Blocked(f"Couldn't find '{host}'.")
    return addrs


def check_url(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise Blocked("Only http and https addresses can be fetched.")
    port = p.port or (443 if p.scheme == "https" else 80)
    if port not in WEB_PORTS and not _allow_private():
        raise Blocked(f"Port {port} isn't allowed.")
    resolve_public(p.hostname, port)


def check_mail_host(host, port):
    if int(port) not in MAIL_PORTS and not _allow_private():
        raise Blocked(f"Port {port} isn't a mail port (use 587, 465, 993 ...).")
    resolve_public(host, int(port))


def safe_get(url, headers=None, timeout=12, max_redirects=3, max_bytes=400_000):
    """GET a web page safely. Returns the text, or raises Blocked / requests exceptions."""
    current = url
    for _ in range(max_redirects + 1):
        check_url(current)
        r = requests.get(current, headers=headers, timeout=timeout, allow_redirects=False, stream=True)
        try:
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                current = urljoin(current, r.headers["Location"])
                continue
            if r.status_code >= 400:
                return ""
            body = b""
            for chunk in r.iter_content(16384):
                body += chunk
                if len(body) >= max_bytes:
                    break
            return body[:max_bytes].decode(r.encoding or "utf-8", errors="replace")
        finally:
            r.close()
    raise Blocked("Too many redirects.")
