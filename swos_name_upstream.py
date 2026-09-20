#!/usr/bin/env python3
"""Automated SwOS Uplink Detection and Upstream Switch Naming Tool.

Discovers the uplink port on MikroTik CSS106-5G-1S switches (via RSTP root port
role and learned host distribution), maps the immediate upstream switch using
Dell SNMP forwarding tables and MikroTik neighbor/bridge topology, and renames
the switch identity to the deterministic pattern: swos-<octet>-<upstream-switch>
(e.g., swos-131-sw-a200).
"""

from __future__ import annotations

import argparse
import datetime
import glob
import os
import re
import socket
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import click
from rich.console import Console
from rich.table import Table

# Add repository root to path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mikrotik_swos.client import SwOSClient, SwOSConnectionError, SwOSError
from mikrotik_swos.codec import decode_hex_str, encode_hex_str

try:
    _term_width = os.get_terminal_size().columns if sys.stdout.isatty() else 200
except OSError:
    _term_width = 200
console = Console(emoji=False, width=max(180, _term_width))

DEFAULT_INVENTORY = Path.home() / "m-swos" / "m-swos-ip-mac"
FALLBACK_INVENTORY = Path("/home/dpavlin/m-swos/m-swos-ip-mac")
DEFAULT_FDB_DIR = Path("/dev/shm/snmp-mac-port")
DEFAULT_NEIGHBORS = Path("/dev/shm/neighbors.tab")
DEFAULT_SW_IP_MAC = Path("/dev/shm/sw-ip-name-mac")
DEFAULT_TRUNK_REGEX = Path("/dev/shm/trunk.regex")

DEFAULT_PORT_NAMES = ["Port1", "Port2", "Port3", "Port4", "Port5", "SFP"]


def normalize_mac(mac: str) -> str:
    """Normalize MAC address to lowercase colon-separated hex."""
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    if len(cleaned) == 12:
        return ":".join(cleaned[i : i + 2] for i in range(0, 12, 2))
    return mac.lower()


def normalize_port_name(port_str: str) -> str:
    """Extract physical port number/designator while ignoring virtual interfaces."""
    p = port_str.split(",")[0].strip()
    if p.lower().startswith(("vlan", "mgmt", "null")):
        return p
    p_leaf = p.split("/")[-1]
    m = re.search(r"(?:Gi|Te|Fa)\d+/\d+/(\d+)", p_leaf, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.match(r"^(?:g|ether|port)(\d+)$", p_leaf, re.IGNORECASE)
    if m:
        return m.group(1)
    if p_leaf.isdigit():
        return p_leaf
    return p_leaf


class TopologyDiscovery:
    """Loads and caches L2 topology, Dell SNMP FDB tables, and MikroTik neighbors."""

    def __init__(
        self,
        fdb_dir: Path = DEFAULT_FDB_DIR,
        neighbors_file: Path = DEFAULT_NEIGHBORS,
        sw_ip_mac_file: Path = DEFAULT_SW_IP_MAC,
        trunk_regex_file: Path = DEFAULT_TRUNK_REGEX,
        mac_threshold: int = 5,
        debug: bool = False,
    ):
        self.fdb_dir = fdb_dir
        self.neighbors_file = neighbors_file
        self.sw_ip_mac_file = sw_ip_mac_file
        self.trunk_regex_file = trunk_regex_file
        self.mac_threshold = mac_threshold
        self.debug = debug

        self.ip_to_name: Dict[str, str] = {}
        self.trunk_ports: Set[Tuple[str, str]] = set()
        self.inter_switch_links: Set[Tuple[str, str]] = set()
        self.port_mac_count: Counter[Tuple[str, str]] = Counter()
        self.mac_locations: Dict[str, List[Tuple[str, str]]] = {}
        self.switch_hierarchy: Dict[str, Set[str]] = {}  # parent -> children
        self.mndp_neighbors: Dict[str, Tuple[str, str]] = {}  # mac -> (switch, port)

        self._load_mappings()
        self._load_neighbors()
        self._load_trunk_regex()
        self._load_fdb()

    def _load_mappings(self) -> None:
        """Load switch IP to switch hostname mapping."""
        if self.sw_ip_mac_file.is_file():
            try:
                with open(self.sw_ip_mac_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 2:
                            self.ip_to_name[parts[0]] = parts[1]
                if self.debug:
                    console.print(f"[dim]Loaded {len(self.ip_to_name)} switch IP-name mappings from {self.sw_ip_mac_file}[/dim]")
            except Exception as ex:
                if self.debug:
                    console.print(f"[yellow]Warning loading {self.sw_ip_mac_file}: {ex}[/yellow]")

    def resolve_switch_name(self, raw_name: str) -> str:
        """Normalize switch IP or hostname to standard switch name."""
        if raw_name in self.ip_to_name:
            return self.ip_to_name[raw_name]
        # Check if raw_name is an IP address
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", raw_name):
            try:
                host = socket.gethostbyaddr(raw_name)[0]
                short = host.split(".")[0]
                self.ip_to_name[raw_name] = short
                return short
            except Exception:
                pass
        return raw_name

    def _load_neighbors(self) -> None:
        """Parse neighbors.tab for inter-switch trunk links and hierarchy."""
        if not self.neighbors_file.is_file():
            if self.debug:
                console.print(f"[yellow]Neighbors file {self.neighbors_file} not found.[/yellow]")
            return

        try:
            with open(self.neighbors_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.strip().split("\t")
                    if len(parts) >= 5:
                        local_sw = self.resolve_switch_name(parts[0])
                        local_port = normalize_port_name(parts[1])
                        remote_port = normalize_port_name(parts[3])
                        remote_name = parts[4].strip()

                        # Check if remote name is an infrastructure switch
                        if remote_name.startswith("sw-") or remote_name == "sw-core":
                            remote_sw = self.resolve_switch_name(remote_name)
                            self.inter_switch_links.add((local_sw, local_port))
                            if remote_port:
                                self.inter_switch_links.add((remote_sw, remote_port))
                            self.switch_hierarchy.setdefault(local_sw, set()).add(remote_sw)
                        elif len(parts) >= 3:
                            # Check if parts[2] is a learned neighbor MAC
                            n_mac = normalize_mac(parts[2])
                            if re.match(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", n_mac):
                                if (
                                    local_port not in ("24", "48", "50", "52", "sfp2-2")
                                    and not local_port.startswith("vlan")
                                    and (local_sw, local_port) not in self.inter_switch_links
                                ):
                                    self.mndp_neighbors[n_mac] = (local_sw, local_port)
            if self.debug:
                console.print(f"[dim]Loaded {len(self.inter_switch_links)} inter-switch links and {len(self.mndp_neighbors)} MNDP neighbors from {self.neighbors_file}[/dim]")
        except Exception as ex:
            if self.debug:
                console.print(f"[yellow]Warning reading neighbors file: {ex}[/yellow]")

    def _load_trunk_regex(self) -> None:
        """Load known trunk ports from trunk.regex if available."""
        if not self.trunk_regex_file.is_file():
            return
        try:
            with open(self.trunk_regex_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r"^/([^:]+):[^\s]+\s+\.\*\s+(\d+)\$$", line.strip())
                    if m:
                        sw = self.resolve_switch_name(m.group(1))
                        port = m.group(2)
                        self.trunk_ports.add((sw, port))
            if self.debug:
                console.print(f"[dim]Loaded {len(self.trunk_ports)} trunk ports from {self.trunk_regex_file}[/dim]")
        except Exception as ex:
            if self.debug:
                console.print(f"[yellow]Warning reading trunk regex file: {ex}[/yellow]")

    def _load_fdb(self) -> None:
        """Load all switch MAC-to-port forwarding tables from fdb_dir."""
        if not self.fdb_dir.is_dir():
            if self.debug:
                console.print(f"[yellow]FDB directory {self.fdb_dir} not found.[/yellow]")
            return

        files = glob.glob(str(self.fdb_dir / "*"))
        for file_path in files:
            p = Path(file_path)
            raw_sw = p.name
            sw_name = self.resolve_switch_name(raw_sw)

            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 4:
                            # format: sw-name vlan mac port
                            line_sw = self.resolve_switch_name(parts[0]) or sw_name
                            mac = normalize_mac(parts[2])
                            port = parts[3]
                            key = (line_sw, port)
                            self.port_mac_count[key] += 1
                            self.mac_locations.setdefault(mac, []).append(key)
            except Exception as ex:
                if self.debug:
                    console.print(f"[yellow]Error reading FDB file {p}: {ex}[/yellow]")

        if self.debug:
            console.print(
                f"[dim]Loaded FDB tables: {len(files)} switches, "
                f"{len(self.mac_locations)} unique MACs indexed.[/dim]"
            )

    def find_upstream_switch(self, mac: str) -> Optional[Tuple[str, str, int, str]]:
        """Identify the direct upstream switch and port for a given MAC address.

        Returns:
            Tuple of (switch_name, port, learned_mac_count, method_description)
            or None if unmapped.
        """
        mac = normalize_mac(mac)

        # Priority 1: Check direct MikroTik MNDP neighbor table
        if mac in self.mndp_neighbors:
            m_sw, m_port = self.mndp_neighbors[mac]
            norm_m_port = normalize_port_name(m_port)
            if (m_sw, m_port) not in self.inter_switch_links and (m_sw, norm_m_port) not in self.inter_switch_links:
                cnt = self.port_mac_count.get((m_sw, m_port), 1)
                return (m_sw, m_port, cnt, "MikroTik MNDP direct neighbor")

        locs = self.mac_locations.get(mac, [])
        if not locs:
            return None

        # Filter 1: Access ports with learned MAC count <= threshold
        candidates: List[Tuple[str, str, int]] = []
        for sw, port in locs:
            norm_p = normalize_port_name(port)
            cnt = self.port_mac_count[(sw, port)]

            # Check if classified as trunk
            if cnt > self.mac_threshold:
                continue
            if (sw, port) in self.trunk_ports or (sw, norm_p) in self.trunk_ports:
                continue
            if (sw, port) in self.inter_switch_links or (sw, norm_p) in self.inter_switch_links:
                continue

            candidates.append((sw, port, cnt))

        if not candidates:
            return None

        # Deduplicate candidates per switch (keep port with lowest learned MAC count)
        by_switch: Dict[str, Tuple[str, int]] = {}
        for sw, port, cnt in candidates:
            if sw not in by_switch or cnt < by_switch[sw][1]:
                by_switch[sw] = (port, cnt)

        if len(by_switch) == 1:
            sw, (port, cnt) = list(by_switch.items())[0]
            return (sw, port, cnt, f"Access port (FDB count={cnt})")

        # Multiple candidates: resolve hierarchy
        # If Switch A is known to be an upstream parent of Switch B, prefer Switch B (the leaf switch)
        survivors = set(by_switch.keys())
        for sw_a in list(survivors):
            children = self.switch_hierarchy.get(sw_a, set())
            for sw_b in list(survivors):
                if sw_b != sw_a and sw_b in children:
                    # sw_a connects to sw_b; sw_b is downstream/closer to leaf
                    survivors.discard(sw_a)
                    break

        if len(survivors) == 1:
            sw = list(survivors)[0]
            port, cnt = by_switch[sw]
            return (sw, port, cnt, f"Leaf switch via hierarchy (FDB count={cnt})")

        # If still multiple, pick the one with lowest MAC count
        sorted_cand = sorted(by_switch.items(), key=lambda item: item[1][1])
        sw, (port, cnt) = sorted_cand[0]
        return (sw, port, cnt, f"Lowest MAC count heuristic ({cnt} MACs)")


@dataclass
class SwOSSwitchInfo:
    ip: str
    mac: str
    current_identity: str
    board: str
    version: str
    uplink_index: int
    uplink_name: str
    uplink_reason: str
    upstream_switch: Optional[str] = None
    upstream_port: Optional[str] = None
    upstream_mac_count: Optional[int] = None
    proposed_identity: Optional[str] = None
    status: str = "PENDING"
    error: Optional[str] = None


def format_swos_identity(
    octet: str, upstream: str, pattern: str = "swos-{octet}-{upstream}", max_len: int = 16
) -> str:
    """Format SwOS identity strictly respecting SwOS 16-character hardware limit."""
    name = pattern.format(octet=octet, upstream=upstream)
    if len(name) <= max_len:
        return name

    # 1. Strip 'sw-' prefix
    upstream_short = upstream[3:] if upstream.startswith("sw-") else upstream
    name = f"swos-{octet}-{upstream_short}"
    if len(name) <= max_len:
        return name

    # 2. For multi-part names like 'a117-cervantes', use the location code 'a117'
    parts = upstream_short.split("-")
    if len(parts) > 1:
        name_primary = f"swos-{octet}-{parts[0]}"
        if len(name_primary) <= max_len:
            return name_primary

    return name[:max_len]


def inspect_swos_switch(
    ip: str,
    discovery: TopologyDiscovery,
    pattern: str = "swos-{octet}-{upstream}",
    username: str = "admin",
    password: str = "",
    timeout: float = 2.0,
    debug: bool = False,
) -> SwOSSwitchInfo:
    """Connect to a SwOS switch, inspect its uplink, and determine proposed name."""
    octet = ip.split(".")[-1]
    try:
        client = SwOSClient(ip, username=username, password=password, timeout=timeout)
        sys_data = client.get("sys.b")
        rstp_data = client.get("rstp.b")
        dhost_data = client.get("!dhost.b")
        link_data = client.get("link.b")
    except SwOSConnectionError as ex:
        return SwOSSwitchInfo(
            ip=ip,
            mac="",
            current_identity="",
            board="",
            version="",
            uplink_index=-1,
            uplink_name="",
            uplink_reason="",
            status="UNREACHABLE",
            error=str(ex),
        )
    except Exception as ex:
        return SwOSSwitchInfo(
            ip=ip,
            mac="",
            current_identity="",
            board="",
            version="",
            uplink_index=-1,
            uplink_name="",
            uplink_reason="",
            status="ERROR",
            error=str(ex),
        )

    # Decode basic sys properties
    raw_mac = sys_data.get("mac", "")
    mac = normalize_mac(raw_mac)
    curr_id = decode_hex_str(sys_data.get("id", "")) or "MikroTik"
    board = decode_hex_str(sys_data.get("brd", ""))
    ver = decode_hex_str(sys_data.get("ver", ""))

    # Port names from link.b
    port_names = list(DEFAULT_PORT_NAMES)
    raw_nm = link_data.get("nm", [])
    for idx, hex_name in enumerate(raw_nm):
        if idx < len(port_names) and hex_name:
            decoded = decode_hex_str(hex_name)
            if decoded:
                port_names[idx] = decoded

    # Uplink determination:
    # Method 1: RSTP Root Port (role == 2)
    roles = rstp_data.get("role", [])
    uplink_idx = -1
    uplink_reason = ""
    for idx, role in enumerate(roles):
        if role == 2:  # Root port
            uplink_idx = idx
            uplink_reason = f"RSTP Root Port (role=2)"
            break

    # Method 2: Fallback to highest host count from !dhost.b
    port_hosts: Counter[int] = Counter()
    for h in (dhost_data or []):
        p = h.get("prt")
        if p is not None:
            port_hosts[p] += 1

    if uplink_idx == -1:
        if port_hosts:
            best_port, count = port_hosts.most_common(1)[0]
            uplink_idx = best_port
            uplink_reason = f"DHost Max Learned ({count} MACs)"
        else:
            uplink_idx = 0
            uplink_reason = "Default (Port 0)"
    else:
        root_hosts = port_hosts.get(uplink_idx, 0)
        uplink_reason += f", learned {root_hosts} MACs"

    uplink_name = port_names[uplink_idx] if 0 <= uplink_idx < len(port_names) else f"Port{uplink_idx+1}"

    # Upstream switch identification via TopologyDiscovery
    upstream_match = discovery.find_upstream_switch(mac)
    upstream_sw = None
    upstream_port = None
    upstream_cnt = None
    proposed_id = None

    if upstream_match:
        upstream_sw, upstream_port, upstream_cnt, _ = upstream_match
        proposed_id = format_swos_identity(octet=octet, upstream=upstream_sw, pattern=pattern)

    info = SwOSSwitchInfo(
        ip=ip,
        mac=mac,
        current_identity=curr_id,
        board=board,
        version=ver,
        uplink_index=uplink_idx,
        uplink_name=uplink_name,
        uplink_reason=uplink_reason,
        upstream_switch=upstream_sw,
        upstream_port=upstream_port,
        upstream_mac_count=upstream_cnt,
        proposed_identity=proposed_id,
    )

    if not upstream_match:
        info.status = "UPSTREAM_UNRESOLVED"
    elif curr_id == proposed_id:
        info.status = "ALREADY_NAMED"
    else:
        info.status = "READY_TO_RENAME"

    return info


def apply_switch_rename(
    info: SwOSSwitchInfo,
    rename_uplink_port: bool = False,
    username: str = "admin",
    password: str = "",
    timeout: float = 2.0,
    debug: bool = False,
) -> bool:
    """Apply new identity (and optionally uplink port name) to SwOS switch."""
    if not info.proposed_identity or info.status not in ("READY_TO_RENAME", "DRY_RUN"):
        return False

    client = SwOSClient(info.ip, username=username, password=password, timeout=timeout)
    try:
        # Update identity in sys.b
        client.set_system(identity=info.proposed_identity)
        time.sleep(0.3)

        # Verify change in sys.b with retry
        try:
            verified = client.get("sys.b")
        except Exception:
            time.sleep(0.5)
            verified = client.get("sys.b")

        actual_id = decode_hex_str(verified.get("id", ""))
        if actual_id != info.proposed_identity:
            info.status = "FAILED"
            info.error = f"Verification failed: expected '{info.proposed_identity}', got '{actual_id}'"
            return False

        # Optionally rename uplink port in link.b
        if rename_uplink_port and info.uplink_index >= 0 and info.upstream_switch and info.upstream_port:
            port_label = f"up:{info.upstream_switch}:{info.upstream_port}"
            # Limit port name to 16 chars if necessary
            port_label = port_label[:16]
            client.set_port(info.uplink_index, name=port_label)

        info.current_identity = actual_id
        info.status = "RENAMED"
        return True
    except Exception as ex:
        if debug:
            console.print(f"[red]Error applying rename on {info.ip}: {ex}[/red]")
        info.status = "ERROR"
        info.error = str(ex)
        return False


def load_inventory_ips(inventory_path: Path) -> List[str]:
    """Parse switch IPs from inventory file (m-swos-ip-mac format)."""
    if not inventory_path.is_file():
        if FALLBACK_INVENTORY.is_file():
            inventory_path = FALLBACK_INVENTORY
        else:
            return []

    ips: List[str] = []
    with open(inventory_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if parts and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[0]):
                ips.append(parts[0])
    return sorted(ips, key=lambda ip: [int(x) for x in ip.split(".")])


def update_inventory_file(
    inventory_path: Path, results: List[SwOSSwitchInfo], debug: bool = False
) -> int:
    """Update switch comments in inventory file (m-swos-ip-mac) with verified identities."""
    if not inventory_path.is_file():
        if FALLBACK_INVENTORY.is_file():
            inventory_path = FALLBACK_INVENTORY
        else:
            return 0

    id_map = {
        r.ip: r.proposed_identity
        for r in results
        if r.proposed_identity and r.status in ("RENAMED", "ALREADY_NAMED")
    }
    if not id_map:
        return 0

    new_lines = []
    updated_count = 0
    with open(inventory_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            clean = line.strip()
            if not clean or clean.startswith("#"):
                new_lines.append(line)
                continue

            parts = clean.split()
            ip = parts[0]
            if ip in id_map:
                mac = parts[1] if len(parts) > 1 else ""
                new_id = id_map[ip]
                new_line = f"{ip} {mac} # {new_id}\n"
                if new_line != line:
                    updated_count += 1
                new_lines.append(new_line)
            else:
                new_lines.append(line)

    if updated_count > 0:
        with open(inventory_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        console.print(f"[green]Updated {updated_count} switch comments in {inventory_path}[/green]")
    return updated_count


def refresh_dell_fdb(debug: bool = False) -> None:
    """Execute parallel snmp-mac-port refresh on black.ffzg.hr."""
    console.print("[bold blue]Refreshing Dell SNMP FDB tables across campus fleet...[/bold blue]")
    cmd = "cd /home/dpavlin/dell-switch && ./sw-names | xargs -i echo ./snmp-mac-port {} | parallel -j 20"
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0 and debug:
        console.print(f"[yellow]parallel snmp-mac-port returned {res.returncode}: {res.stderr[:200]}[/yellow]")
    
    # Also regenerate trunk regex
    trunk_cmd = ". /home/dpavlin/dell-switch/shm-trunk.regex"
    subprocess.run(trunk_cmd, shell=True, capture_output=True, text=True)
    console.print("[green]SNMP FDB tables refreshed in /dev/shm/snmp-mac-port/[/green]")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("targets", nargs=-1)
@click.option("--all", "all_switches", is_flag=True, help="Run on all switches in inventory file.")
@click.option("--inventory", type=click.Path(path_type=Path), default=DEFAULT_INVENTORY, help="Path to m-swos-ip-mac inventory file.")
@click.option("--range", "ip_range", type=str, default=None, help="IP range to inspect, e.g. 192.168.88.117-192.168.88.131.")
@click.option("--apply", is_flag=True, help="Apply proposed identity renaming to SwOS hardware (default: dry-run).")
@click.option("--rename-uplink-port", is_flag=True, help="Also set SwOS uplink port description (e.g. up:sw-foo:5).")
@click.option("--update-inventory", is_flag=True, help="Update comments in m-swos-ip-mac with switch identities.")
@click.option("--pattern", type=str, default="swos-{octet}-{upstream}", help="Naming template pattern (default: swos-{octet}-{upstream}).")
@click.option("--refresh-fdb", is_flag=True, help="Run parallel SNMP walk to update /dev/shm/snmp-mac-port before discovery.")
@click.option("--mac-threshold", type=int, default=5, help="Maximum learned MAC count for access port qualification (default: 5).")
@click.option("--username", type=str, default="admin", help="SwOS administrative username.")
@click.option("--password", type=str, default="", help="SwOS administrative password.")
@click.option("--timeout", type=float, default=2.0, help="HTTP connection timeout in seconds.")
@click.option("--debug", is_flag=True, help="Enable verbose technical debug output.")
def main(
    targets: Tuple[str, ...],
    all_switches: bool,
    inventory: Path,
    ip_range: Optional[str],
    apply: bool,
    rename_uplink_port: bool,
    update_inventory: bool,
    pattern: str,
    refresh_fdb: bool,
    mac_threshold: int,
    username: str,
    password: str,
    timeout: float,
    debug: bool,
) -> None:
    """MikroTik SwOS Uplink Detection and Upstream Switch Renaming Tool."""
    console.print(
        f"[bold cyan]SwOS Upstream Switch Naming Tool[/bold cyan] "
        f"[dim](Mode: {'[bold red]APPLY CHANGES[/bold red]' if apply else '[bold green]DRY RUN[/bold green]'})[/dim]"
    )

    if refresh_fdb:
        refresh_dell_fdb(debug=debug)

    # Resolve target IPs
    target_ips: List[str] = []
    if targets:
        for t in targets:
            if re.match(r"^\d+\.\d+\.\d+\.\d+$", t):
                target_ips.append(t)
            elif "-" in t:
                start_ip, end_ip = t.split("-", 1)
                s_oct = int(start_ip.strip().split(".")[-1])
                e_oct = int(end_ip.strip().split(".")[-1])
                prefix = ".".join(start_ip.strip().split(".")[:3])
                for octet in range(s_oct, e_oct + 1):
                    target_ips.append(f"{prefix}.{octet}")

    if ip_range:
        start_ip, end_ip = ip_range.split("-", 1)
        s_oct = int(start_ip.strip().split(".")[-1])
        e_oct = int(end_ip.strip().split(".")[-1])
        prefix = ".".join(start_ip.strip().split(".")[:3])
        for octet in range(s_oct, e_oct + 1):
            target_ips.append(f"{prefix}.{octet}")

    if all_switches:
        inv_ips = load_inventory_ips(inventory)
        if not inv_ips:
            console.print(f"[red]Error: No switches found in inventory {inventory}[/red]")
            sys.exit(1)
        target_ips.extend(inv_ips)

    # Deduplicate and sort
    target_ips = sorted(list(set(target_ips)), key=lambda x: [int(o) for o in x.split(".")])

    if not target_ips:
        console.print("[yellow]No target switch IPs specified. Use an IP address, --range, or --all.[/yellow]")
        sys.exit(1)

    console.print(f"Target switches to inspect: [bold]{len(target_ips)}[/bold]")

    # Initialize Topology Engine
    topo = TopologyDiscovery(mac_threshold=mac_threshold, debug=debug)

    results: List[SwOSSwitchInfo] = []
    with click.progressbar(target_ips, label="Analyzing SwOS switches", file=sys.stderr) as bar:
        for ip in bar:
            info = inspect_swos_switch(
                ip=ip,
                discovery=topo,
                pattern=pattern,
                username=username,
                password=password,
                timeout=timeout,
                debug=debug,
            )

            if apply and info.status == "READY_TO_RENAME":
                apply_switch_rename(
                    info,
                    rename_uplink_port=rename_uplink_port,
                    username=username,
                    password=password,
                    timeout=timeout,
                    debug=debug,
                )
            elif not apply and info.status == "READY_TO_RENAME":
                info.status = "PROPOSED"

            results.append(info)

    # Render Report Table
    table = Table(
        title="SwOS Fleet Uplink & Upstream Switch Renaming",
        show_header=True,
        header_style="bold magenta",
        title_style="bold white on blue",
    )
    table.add_column("Switch IP", style="cyan", no_wrap=True)
    table.add_column("MAC Address", style="dim", no_wrap=True)
    table.add_column("Uplink Port", style="blue", no_wrap=True)
    table.add_column("Current Identity", style="white", no_wrap=True)
    table.add_column("Upstream Switch", style="green", no_wrap=True)
    table.add_column("Port", justify="right", style="yellow", no_wrap=True)
    table.add_column("Proposed Identity", style="bold yellow", no_wrap=True)
    table.add_column("Status", style="bold", no_wrap=True)

    for r in results:
        status_color = {
            "RENAMED": "[green]RENAMED[/green]",
            "PROPOSED": "[cyan]PROPOSED[/cyan]",
            "ALREADY_NAMED": "[dim green]ALREADY_NAMED[/dim green]",
            "UPSTREAM_UNRESOLVED": "[yellow]UNRESOLVED[/yellow]",
            "UNREACHABLE": "[red]UNREACHABLE[/red]",
            "ERROR": "[red]ERROR[/red]",
            "FAILED": "[bold red]FAILED[/bold red]",
        }.get(r.status, r.status)

        if r.uplink_name:
            method_tag = "RSTP" if "RSTP" in r.uplink_reason else "DHost"
            uplink_str = f"{r.uplink_name} [dim]({method_tag})[/dim]"
        else:
            uplink_str = "-"

        table.add_row(
            r.ip,
            r.mac or "-",
            uplink_str,
            r.current_identity or "-",
            r.upstream_switch or "-",
            str(r.upstream_port) if r.upstream_port is not None else "-",
            r.proposed_identity or "-",
            status_color,
        )

    console.print()
    console.print(table)
    console.print()

    # Summary Statistics
    total = len(results)
    renamed = sum(1 for r in results if r.status == "RENAMED")
    proposed = sum(1 for r in results if r.status == "PROPOSED")
    already = sum(1 for r in results if r.status == "ALREADY_NAMED")
    unresolved = sum(1 for r in results if r.status == "UPSTREAM_UNRESOLVED")
    unreachable = sum(1 for r in results if r.status in ("UNREACHABLE", "ERROR", "FAILED"))

    console.print(
        f"[bold]Summary:[/bold] Total: [bold]{total}[/bold] | "
        f"Renamed: [green bold]{renamed}[/green bold] | "
        f"Proposed: [cyan bold]{proposed}[/cyan bold] | "
        f"Already Named: [green]{already}[/green] | "
        f"Unresolved Upstream: [yellow]{unresolved}[/yellow] | "
        f"Unreachable/Error: [red]{unreachable}[/red]"
    )

    if update_inventory and (apply or renamed > 0 or already > 0):
        update_inventory_file(inventory, results, debug=debug)

    if not apply and proposed > 0:
        console.print(
            "\n[bold yellow]Dry run completed.[/bold yellow] To execute the renaming on the hardware switches, "
            "re-run the command with [bold cyan]--apply[/bold cyan]."
        )


if __name__ == "__main__":
    main()
