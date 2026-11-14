"""Classify direct IPv4 scanner connections using the host's interface prefixes."""

import json
from functools import lru_cache
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
import subprocess
from threading import Lock
from time import monotonic


TAILSCALE_RANGE = IPv4Network("100.64.0.0/10")
TAILSCALE_ROUTE_TABLE = "52"
LAN_RANGES = tuple(IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
NETWORK_REFRESH_SECONDS = 5.0
NAME_REFRESH_SECONDS = 60


def _command_output(*args):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return ""


def _command_json(*args):
    try:
        return json.loads(_command_output(*args))
    except ValueError:
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
    if status is None:
        # The standard host-network Docker deployment keeps the CLI inside the container.
        status = _command_json("docker", "exec", "tailscale", "tailscale", "status", "--json")
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return set(), set(), set()
    own_node = status.get("Self")
    peers = status.get("Peer")
    name = own_node.get("DNSName") if isinstance(own_node, dict) else None
    names = set()
    if isinstance(name, str) and (name := name.lower().rstrip(".")):
        names.update((name, name.split(".", 1)[0]))
    return (_tailnet_ipv4(own_node), _tailnet_ipv4(*peers.values()) if isinstance(peers, dict) else set(), names)


@lru_cache(maxsize=32)
def _lan_name(destination, _refresh_bucket):
    # Ask the system resolver for this server address, never for the request's Host.
    address = str(destination)
    for line in _command_output("getent", "hosts", address).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == address:
            name = fields[1].lower().rstrip(".")
            answers = _command_output("getent", "--", "ahostsv4", name)
            if any(answer.split(maxsplit=1)[:1] == [address] for answer in answers.splitlines()):
                return name
    return None


class LocalNetworks:
    def __init__(self):
        self._lock = Lock()
        self._refresh_at = 0.0
        self._subnets = {}
        self._tailnet_peers = {}
        self._tailnet_names = {}

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
        tailnet_names = {}
        if tunnel_addresses:
            # Tailscale configures a /32 on its tunnel and places peer routes in table 52.
            # A routed subnet can also appear there, so require a known peer IP.
            own_ips, peer_ips, own_names = _tailnet_devices()
            tailnet_names = {address: own_names for addresses in tunnel_addresses.values()
                             for address in addresses & own_ips}
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
        self._tailnet_names = tailnet_names
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

    def is_local_name(self, host, destination):
        """Match a hostname announced for the socket's local address."""
        host = host.rstrip(".")
        if destination in TAILSCALE_RANGE:
            with self._lock:
                if monotonic() >= self._refresh_at:
                    self._refresh()
                return host in self._tailnet_names.get(destination, ())
        return (any(destination in network for network in LAN_RANGES)
                and host == _lan_name(destination, monotonic() // NAME_REFRESH_SECONDS))
