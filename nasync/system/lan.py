"""Is the NAS on the same local network as this machine?

Live sync reacts to every file change, so before it touches the network it has
to answer "am I home?" cheaply and without false positives. Syncing a laptop's
folders to a home NAS from a cafe is at best a long stall on an unreachable
address, and at worst an attempt to speak SMB to whatever else answers on that
address out there.

Two checks, in order:

  1. **On-link route** — the kernel already knows whether an address is
     reachable directly over a link or only through a gateway. A route with no
     gateway means the server is on a network segment this machine is attached
     to, which is exactly what "same LAN" means. This is read from
     ``/proc/net/route`` and needs no packets at all.

  2. **Private + reachable** — for setups where the NAS lives on another subnet
     of the same home network (VLANs, mesh routers, wired/wireless split), fall
     back to a short TCP probe. Restricted to RFC1918 addresses, so a routable
     public address never qualifies however reachable it happens to be.

On a platform without ``/proc/net/route`` (macOS) only the second check runs.
"""

import ipaddress
import socket
import struct
from pathlib import Path

PROC_ROUTE = Path("/proc/net/route")

# Long enough for a NAS that is awake, short enough that live sync doesn't
# stall noticeably when we're away from home.
PROBE_TIMEOUT = 1.0


def resolve(host: str):
    """Resolve `host` to an IPv4 address, or None if it doesn't resolve."""
    if not host:
        return None
    try:
        ipaddress.IPv4Address(host)
        return host                      # already an address; don't hit DNS
    except ipaddress.AddressValueError:
        pass
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def _onlink_networks(route_text: str):
    """Yield the networks this machine can reach without a gateway.

    ``/proc/net/route`` holds addresses as little-endian hex words, which is
    why they go back through ``struct``/``inet_ntoa`` rather than being read as
    plain integers.
    """
    networks = []
    for line in route_text.splitlines()[1:]:        # first line is the header
        fields = line.split()
        if len(fields) < 8:
            continue
        _iface, destination, gateway, _flags, _rc, _use, _metric, mask = fields[:8]
        if int(gateway, 16) != 0:
            continue                                # reached via a router
        try:
            mask_int = int.from_bytes(struct.pack("<L", int(mask, 16)), "big")
            dest_int = int.from_bytes(struct.pack("<L", int(destination, 16)), "big")
        except (ValueError, struct.error):
            continue
        if mask_int == 0:
            # A gateway-less default route would otherwise match the whole
            # internet. Nothing about it says "local".
            continue
        networks.append((dest_int, mask_int))
    return networks


def same_subnet(ip: str, route_text: str = None):
    """True/False if the routing table can answer; None if it isn't readable."""
    if route_text is None:
        try:
            route_text = PROC_ROUTE.read_text()
        except OSError:
            return None                             # not Linux, or no procfs
    try:
        address = int(ipaddress.IPv4Address(ip))
    except ipaddress.AddressValueError:
        return False
    return any(address & mask == network
               for network, mask in _onlink_networks(route_text))


def is_private(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_private
    except ipaddress.AddressValueError:
        return False


def reachable(ip: str, port: int = 445, timeout: float = PROBE_TIMEOUT) -> bool:
    """True if something accepts a TCP connection on `ip:port` right now."""
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def on_lan(host: str, port: int = 445, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when `host` is on a network this machine is directly attached to."""
    ip = resolve(host)
    if ip is None:
        return False
    if same_subnet(ip):
        return True
    # Anything past here is off-link, so it only counts as "home" if it is a
    # private address that answers immediately.
    return is_private(ip) and reachable(ip, port, timeout)


def describe(host: str, port: int = 445) -> str:
    """One line for the UI explaining the current verdict."""
    ip = resolve(host)
    if ip is None:
        return f"'{host}' doesn't resolve to an address."
    if same_subnet(ip):
        return f"On the same network as {ip}."
    if not is_private(ip):
        return (f"{ip} is a public address — live sync only runs on a "
                f"private home network.")
    if reachable(ip, port):
        return f"{ip} is on another subnet of this network, and reachable."
    return f"Not on the same network as {ip} — syncing is paused."
