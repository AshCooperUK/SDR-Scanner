"""Classify direct IPv4 scanner connections using the host's interface prefixes."""

import json
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
import subprocess
from threading import Lock
from time import monotonic


TAILSCALE_RANGE = IPv4Network("100.64.0.0/10")
TAILSCALE_ROUTE_TABLE = "52"
NETWORK_REFRESH_SECONDS = 5.0


def _command_json(*args):
    try:
        output = subprocess.run(args, check=True, capture_output=True, timeout=1)
        return json.loads(output.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _ip_json(*args):
    data = _command_json("ip", "-j", "-4", *args)
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _tailnet_ipv4(*nodes):
    result = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(addresses := node.get("TailscaleIPs"), list):
            continue
        for address in addresses:
            if not isinstance(address, str):
                continue
            try:
                ip = IPv4Address(address)
            except ValueError:
                continue
            if ip in TAILSCALE_RANGE:
                result.add(ip)
    return result


def _tailnet_devices():
    status = _command_json("tailscale", "status", "--json")
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return set(), set()
    peers = status.get("Peer")
    return (_tailnet_ipv4(status.get("Self")),
            _tailnet_ipv4(*peers.values()) if isinstance(peers, dict) else set())


class LocalNetworks:
    def __init__(self):
        self._lock = Lock()
        self._refresh_at = 0.0
        self._subnets = {}
        self._tailnet_peers = {}

    def _refresh(self):
        subnets = {}
        tunnel_addresses = {}
        for interface in _ip_json("addr", "show", "up"):
            name = interface.get("ifname")
            flags = interface.get("flags", ())
            if not name or "NO-CARRIER" in flags:
                continue
            for address in interface.get("addr_info", ()):
                try:
                    entry = IPv4Interface(f"{address['local']}/{address['prefixlen']}")
                except (KeyError, TypeError, ValueError):
                    continue
                if entry.network.prefixlen:
                    subnets[entry.ip] = entry.network
                if (entry.network.prefixlen == 32 and entry.ip in TAILSCALE_RANGE
                        and "POINTOPOINT" in flags and "NOARP" in flags):
                    tunnel_addresses.setdefault(name, set()).add(entry.ip)

        tailnet_peers = {}
        if tunnel_addresses:
            # Tailscale configures a /32 on its tunnel and places peer routes in table 52.
            # A routed subnet can also appear there, so require a known peer IP.
            own_ips, peer_ips = _tailnet_devices()
            for route in _ip_json("route", "show", "table", TAILSCALE_ROUTE_TABLE):
                addresses = tunnel_addresses.get(route.get("dev"), ())
                if not addresses or "gateway" in route or route.get("type", "unicast") != "unicast":
                    continue
                try:
                    peer_route = IPv4Network(route.get("dst", ""))
                except ValueError:
                    continue
                peer = peer_route.network_address
                if peer_route.prefixlen == 32 and peer in peer_ips:
                    for address in addresses & own_ips:
                        tailnet_peers.setdefault(address, set()).add(peer)

        self._subnets = subnets
        self._tailnet_peers = tailnet_peers
        self._refresh_at = monotonic() + NETWORK_REFRESH_SECONDS

    def is_local(self, peer: IPv4Address, destination: IPv4Address) -> bool:
        """Recognise the exact destination's subnet or its known Tailscale peers."""
        if peer.is_loopback or destination.is_loopback:
            return peer.is_loopback and destination.is_loopback
        with self._lock:
            if monotonic() >= self._refresh_at:
                self._refresh()
            subnet = self._subnets.get(destination)
            return (subnet is not None and peer in subnet
                    or peer in self._tailnet_peers.get(destination, ()))
