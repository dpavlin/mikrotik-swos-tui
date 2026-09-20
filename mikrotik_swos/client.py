"""HTTP Client for MikroTik SwOS with Digest authentication and debug logging."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Union

import requests
from requests.auth import HTTPDigestAuth

from mikrotik_swos.codec import (
    SwOSCodec,
    decode_hex_str,
    encode_hex_str,
    encode_ip,
)
from mikrotik_swos.models import (
    HostEntry,
    PortInfo,
    PortStats,
    SfpInfo,
    SnmpInfo,
    SystemInfo,
    VlanEntry,
)

logger = logging.getLogger("swos")


class SwOSError(Exception):
    """Base exception for SwOS operations."""
    pass


class SwOSAuthError(SwOSError):
    """Authentication failure (wrong password)."""
    pass


class SwOSConnectionError(SwOSError):
    """Connection or timeout error reaching the switch."""
    pass


class SwOSClient:
    """Client for administering MikroTik switches running SwOS."""

    def __init__(
        self,
        host: str = "192.168.88.1",
        username: str = "admin",
        password: str = "",
        timeout: float = 5.0,
        debug: bool = False,
    ):
        self.host = host.rstrip("/")
        if not self.host.startswith("http://") and not self.host.startswith("https://"):
            self.base_url = f"http://{self.host}"
        else:
            self.base_url = self.host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.debug = debug
        self.session = requests.Session()
        self.auth = HTTPDigestAuth(self.username, self.password)

    def _log_debug(self, msg: str) -> None:
        if self.debug:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] [DEBUG] {msg}")
        logger.debug(msg)

    def get_raw(self, endpoint: str) -> str:
        """Issue HTTP GET to SwOS endpoint and return raw text."""
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/{endpoint}"
        self._log_debug(f"GET {url} (user={self.username})")
        start = time.perf_counter()
        try:
            resp = self.session.get(url, auth=self.auth, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._log_debug(f"GET {url} FAILED after {elapsed_ms:.1f}ms: {e}")
            raise SwOSConnectionError(f"Failed to connect to {url}: {e}") from e

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._log_debug(f"GET {url} -> {resp.status_code} ({elapsed_ms:.1f}ms, {len(resp.content)} bytes)")

        if resp.status_code == 401:
            raise SwOSAuthError(f"Authentication failed for {self.username}@{self.host}")
        if resp.status_code != 200:
            raise SwOSError(f"GET {url} returned HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.text

    def post_raw(self, endpoint: str, data: str) -> str:
        """Issue HTTP POST to SwOS endpoint with text/plain body."""
        endpoint = endpoint.lstrip("/")
        url = f"{self.base_url}/{endpoint}"
        self._log_debug(f"POST {url} payload: {data}")
        start = time.perf_counter()
        try:
            resp = self.session.post(
                url,
                data=data,
                headers={"Content-Type": "text/plain"},
                auth=self.auth,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            self._log_debug(f"POST {url} FAILED after {elapsed_ms:.1f}ms: {e}")
            raise SwOSConnectionError(f"Failed to connect to {url}: {e}") from e

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._log_debug(f"POST {url} -> {resp.status_code} ({elapsed_ms:.1f}ms, {len(resp.content)} bytes)")

        if resp.status_code == 401:
            raise SwOSAuthError(f"Authentication failed for {self.username}@{self.host}")
        if resp.status_code != 200:
            raise SwOSError(f"POST {url} returned HTTP {resp.status_code}: {resp.text[:200]}")

        return resp.text

    def get(self, endpoint: str) -> Any:
        """Fetch endpoint and parse SwOS literal response into Python structures."""
        raw = self.get_raw(endpoint)
        return SwOSCodec.parse(raw)

    def post(self, endpoint: str, data: Any) -> Any:
        """Serialize Python data to SwOS format and POST to endpoint."""
        serialized = SwOSCodec.dumps(data)
        raw_resp = self.post_raw(endpoint, serialized)
        if raw_resp.strip():
            try:
                return SwOSCodec.parse(raw_resp)
            except Exception:
                return raw_resp
        return None

    # High-level API methods

    def get_system(self) -> SystemInfo:
        """Fetch switch system info and hardware status."""
        data = self.get("sys.b")
        return SystemInfo.from_dict(data)

    def set_system(
        self,
        identity: Optional[str] = None,
        watchdog: Optional[bool] = None,
        discovery: Optional[bool] = None,
        ivl: Optional[bool] = None,
        igmp: Optional[bool] = None,
        static_ip: Optional[str] = None,
        ip_mode: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Update system settings in sys.b."""
        current = self.get("sys.b")
        updates: Dict[str, Any] = {}
        if identity is not None:
            updates["id"] = encode_hex_str(identity)
        if watchdog is not None:
            updates["wdt"] = 1 if watchdog else 0
        if discovery is not None:
            updates["dsc"] = 1 if discovery else 0
        if ivl is not None:
            updates["ivl"] = 1 if ivl else 0
        if igmp is not None:
            updates["igmp"] = 1 if igmp else 0
        if static_ip is not None:
            updates["sip"] = encode_ip(static_ip)
        if ip_mode is not None:
            updates["iptp"] = ip_mode

        current.update(updates)
        self.post("sys.b", current)
        return current

    def get_ports(self) -> List[PortInfo]:
        """Fetch port status, link parameters, and PoE information."""
        link_data = self.get("link.b")
        fwd_data = self.get("fwd.b")
        names = link_data.get("nm", [])
        num_ports = len(names) if names else 6
        ports = []
        for i in range(num_ports):
            ports.append(PortInfo.from_dicts(i, link_data, fwd_data))
        return ports

    def set_port(
        self,
        port_index: int,
        name: Optional[str] = None,
        enabled: Optional[bool] = None,
        auto_negotiation: Optional[bool] = None,
        speed: Optional[str] = None,
        duplex: Optional[str] = None,
        flow_control: Optional[bool] = None,
        poe: Optional[str] = None,
        default_vlan_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Update port settings on link.b and/or fwd.b."""
        link_data = self.get("link.b")
        fwd_data = self.get("fwd.b")

        if name is not None:
            nm = list(link_data.get("nm", []))
            while len(nm) <= port_index:
                nm.append("")
            nm[port_index] = encode_hex_str(name)
            link_data["nm"] = nm

        if enabled is not None:
            en = link_data.get("en", 0x3F)
            if enabled:
                en |= (1 << port_index)
            else:
                en &= ~(1 << port_index)
            link_data["en"] = en

        if auto_negotiation is not None:
            an = link_data.get("an", 0x3F)
            if auto_negotiation:
                an |= (1 << port_index)
            else:
                an &= ~(1 << port_index)
            link_data["an"] = an

        if speed is not None:
            speed_map = {"10M": 0, "100M": 1, "1G": 2, "1000M": 2}
            if speed.upper() in speed_map:
                spdc = list(link_data.get("spdc", [0] * 6))
                spdc[port_index] = speed_map[speed.upper()]
                link_data["spdc"] = spdc

        if duplex is not None:
            dpxc = link_data.get("dpxc", 0x3F)
            if duplex.lower().startswith("f"):
                dpxc |= (1 << port_index)
            else:
                dpxc &= ~(1 << port_index)
            link_data["dpxc"] = dpxc

        if flow_control is not None:
            fct = link_data.get("fct", 0x3F)
            if flow_control:
                fct |= (1 << port_index)
            else:
                fct &= ~(1 << port_index)
            link_data["fct"] = fct

        if poe is not None:
            poe_map = {"off": 0, "auto": 1, "on": 2, "calibr": 3}
            if poe.lower() in poe_map:
                poe_list = list(link_data.get("poe", [0] * 6))
                poe_list[port_index] = poe_map[poe.lower()]
                link_data["poe"] = poe_list

        self.post("link.b", link_data)

        if default_vlan_id is not None:
            dvid = list(fwd_data.get("dvid", [1] * 6))
            dvid[port_index] = int(default_vlan_id)
            fwd_data["dvid"] = dvid
            self.post("fwd.b", fwd_data)

        return link_data

    def get_stats(self) -> List[PortStats]:
        """Fetch port traffic and error counters."""
        link_data = self.get("link.b")
        stats_data = self.get("!stats.b")
        names = [decode_hex_str(x) for x in link_data.get("nm", [])]
        num_ports = len(names) if names else 6
        res = []
        for i in range(num_ports):
            name = names[i] if i < len(names) else f"Port{i+1}"
            res.append(PortStats.from_stats(i, name, stats_data))
        return res

    def get_hosts(self, dynamic: bool = True, static: bool = True) -> List[HostEntry]:
        """Fetch learned dynamic MAC addresses and static MAC hosts."""
        link_data = self.get("link.b")
        names = [decode_hex_str(x) for x in link_data.get("nm", [])]
        hosts: List[HostEntry] = []

        if dynamic:
            dhosts = self.get("!dhost.b") or []
            for item in dhosts:
                hosts.append(HostEntry.from_dict(item, names, dynamic=True))

        if static:
            shosts = self.get("host.b") or []
            for item in shosts:
                hosts.append(HostEntry.from_dict(item, names, dynamic=False))

        return hosts

    def get_vlans(self) -> List[VlanEntry]:
        """Fetch static VLAN table entries."""
        link_data = self.get("link.b")
        names = [decode_hex_str(x) for x in link_data.get("nm", [])]
        vlan_data = self.get("vlan.b") or []
        return [VlanEntry.from_dict(entry, names) for entry in vlan_data]

    def add_vlan(self, vlan_id: int, ports: List[int], ivl: bool = False, igmp: bool = False) -> List[Any]:
        """Add a static VLAN entry to vlan.b."""
        current = self.get("vlan.b") or []
        # SwOS prt array in vlan.b: 0=leave as is, 1=always strip, 2=add if missing, 3=not a member
        # If ports are specified, mark them 0 (leave as is) and others 3 (not a member)
        num_ports = 6
        prt_list = [0 if i in ports else 3 for i in range(num_ports)]
        new_entry = {
            "vid": vlan_id,
            "ivl": 1 if ivl else 0,
            "igmp": 1 if igmp else 0,
            "prt": prt_list,
        }
        # Replace if exists, else append
        filtered = [e for e in current if e.get("vid") != vlan_id]
        filtered.append(new_entry)
        self.post("vlan.b", filtered)
        return filtered

    def delete_vlan(self, vlan_id: int) -> List[Any]:
        """Remove a static VLAN entry by VLAN ID."""
        current = self.get("vlan.b") or []
        filtered = [e for e in current if e.get("vid") != vlan_id]
        self.post("vlan.b", filtered)
        return filtered

    def get_sfp(self) -> SfpInfo:
        """Fetch SFP transceiver diagnostic data."""
        data = self.get("sfp.b")
        return SfpInfo.from_dict(data)

    def get_snmp(self) -> SnmpInfo:
        """Fetch SNMP configuration."""
        data = self.get("snmp.b")
        return SnmpInfo.from_dict(data)

    def set_snmp(
        self,
        enabled: Optional[bool] = None,
        community: Optional[str] = None,
        contact: Optional[str] = None,
        location: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update SNMP configuration."""
        current = self.get("snmp.b")
        if enabled is not None:
            current["en"] = 1 if enabled else 0
        if community is not None:
            current["com"] = encode_hex_str(community)
        if contact is not None:
            current["ci"] = encode_hex_str(contact)
        if location is not None:
            current["loc"] = encode_hex_str(location)
        self.post("snmp.b", current)
        return current

    def get_rstp(self) -> Dict[str, Any]:
        """Fetch RSTP state and configuration."""
        return self.get("rstp.b")

    def get_forwarding(self) -> Dict[str, Any]:
        """Fetch forwarding matrix and VLAN configuration."""
        return self.get("fwd.b")

    def get_acl(self) -> List[Dict[str, Any]]:
        """Fetch ACL rules."""
        return self.get("acl.b") or []

    def backup(self) -> Dict[str, Any]:
        """Export full switch configuration from all configuration endpoints."""
        endpoints = ["sys.b", "link.b", "fwd.b", "rstp.b", "vlan.b", "host.b", "snmp.b", "acl.b"]
        backup_dict: Dict[str, Any] = {}
        for ep in endpoints:
            backup_dict[ep] = self.get(ep)
        return backup_dict

    def restore(self, backup_dict: Dict[str, Any]) -> Dict[str, bool]:
        """Restore configuration from backup dictionary."""
        order = ["snmp.b", "vlan.b", "host.b", "acl.b", "rstp.b", "fwd.b", "link.b", "sys.b"]
        results: Dict[str, bool] = {}
        for ep in order:
            if ep in backup_dict:
                try:
                    self.post(ep, backup_dict[ep])
                    results[ep] = True
                except Exception as e:
                    self._log_debug(f"Restore failed on {ep}: {e}")
                    results[ep] = False
        return results

    def reboot(self) -> bool:
        """Reboot the switch."""
        self._log_debug("Sending reboot command (* to /reboot)...")
        try:
            self.post_raw("reboot", "*")
            return True
        except SwOSConnectionError:
            # Connection drop during reboot is expected
            return True
