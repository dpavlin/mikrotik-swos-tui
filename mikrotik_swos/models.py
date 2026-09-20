"""Data models for MikroTik SwOS configurations, statuses, and counters."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from mikrotik_swos.codec import (
    bitmask_to_ports,
    decode_hex_str,
    decode_ip,
    decode_mac,
    decode_signed32,
    decode_timestamp,
    format_bytes,
    format_rate,
    format_uptime,
)

SPEED_MAP = {0: "10M", 1: "100M", 2: "1G", 3: "Down"}
POE_MODE_MAP = {0: "off", 1: "auto", 2: "on", 3: "calibr"}
POE_STATUS_MAP = {
    0: "disabled",
    6: "voltage too low",
    7: "power cycle",
    10: "short circuit",
    14: "current too low",
    15: "overload",
    16: "powered on",
}
VLAN_MODE_MAP = {0: "disabled", 1: "optional", 2: "enabled", 3: "strict"}
VLAN_HEADER_MAP = {0: "leave as is", 1: "always strip", 2: "add if missing"}
VLAN_RECV_MAP = {0: "any", 1: "only tagged", 2: "only untagged"}
RSTP_ROLE_MAP = {0: "disabled", 1: "alternate", 2: "root", 3: "designated", 4: "backup"}
IPTP_MAP = {0: "DHCP with fallback", 1: "Static", 2: "DHCP only"}


@dataclass
class SystemInfo:
    """System information from sys.b."""
    identity: str
    board: str
    version: str
    build_timestamp: int
    build_datetime: datetime.datetime
    serial: str
    mac: str
    ip: str
    static_ip: str
    ip_mode: str
    uptime_centisec: int
    uptime_str: str
    watchdog: bool
    discovery: bool
    ivl: bool
    igmp_snooping: bool
    allow_from_ip: str
    allow_from_mask: int
    allow_from_ports: List[int]
    allow_from_vlan: int
    bridge_priority: int
    root_bridge_priority: int
    root_bridge_mac: str
    voltage_mv: int
    temperature_c: int
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SystemInfo:
        bld = data.get("bld", 0)
        upt = data.get("upt", 0)
        allp_mask = data.get("allp", 0x3F)
        return cls(
            identity=decode_hex_str(data.get("id")),
            board=decode_hex_str(data.get("brd")),
            version=decode_hex_str(data.get("ver")),
            build_timestamp=bld,
            build_datetime=decode_timestamp(bld) if bld else datetime.datetime.fromtimestamp(0, tz=datetime.timezone.utc),
            serial=decode_hex_str(data.get("sid")),
            mac=decode_mac(data.get("mac")),
            ip=decode_ip(data.get("ip")),
            static_ip=decode_ip(data.get("sip")),
            ip_mode=IPTP_MAP.get(data.get("iptp", 0), f"unknown ({data.get('iptp')})"),
            uptime_centisec=upt,
            uptime_str=format_uptime(upt),
            watchdog=bool(data.get("wdt", 0)),
            discovery=bool(data.get("dsc", 0)),
            ivl=bool(data.get("ivl", 0)),
            igmp_snooping=bool(data.get("igmp", 0)),
            allow_from_ip=decode_ip(data.get("alla", 0)),
            allow_from_mask=data.get("allm", 0),
            allow_from_ports=bitmask_to_ports(allp_mask, 6),
            allow_from_vlan=data.get("avln", 0),
            bridge_priority=data.get("prio", 0x8000),
            root_bridge_priority=data.get("rpr", 0x8000),
            root_bridge_mac=decode_mac(data.get("rmac")),
            voltage_mv=data.get("volt", 0),
            temperature_c=decode_signed32(data.get("temp", 0)),
            raw=data,
        )


@dataclass
class PortInfo:
    """Individual switch port configuration and link state."""
    index: int
    name: str
    enabled: bool
    link_up: bool
    speed: str
    duplex: str
    auto_negotiation: bool
    flow_control: bool
    poe_mode: str
    poe_status: str
    poe_current_ma: int
    poe_power_w: float
    default_vlan_id: int
    vlan_mode: str
    vlan_receive: str
    vlan_header: str

    @classmethod
    def from_dicts(cls, index: int, link_data: Dict[str, Any], fwd_data: Optional[Dict[str, Any]] = None) -> PortInfo:
        fwd = fwd_data or {}
        names = [decode_hex_str(x) for x in link_data.get("nm", [])]
        name = names[index] if index < len(names) else f"Port{index+1}"
        en = bool(link_data.get("en", 0) & (1 << index))
        lnk = bool(link_data.get("lnk", 0) & (1 << index))
        dpx = "Full" if (link_data.get("dpx", 0) & (1 << index)) else "Half"
        an = bool(link_data.get("an", 0) & (1 << index))
        fct = bool(link_data.get("fct", 0) & (1 << index))

        spd_raw = link_data.get("spd", [])
        spd_code = spd_raw[index] if index < len(spd_raw) else 3
        speed = SPEED_MAP.get(spd_code, "Unknown") if lnk else "Down"

        poe_raw = link_data.get("poe", [])
        poe_code = poe_raw[index] if index < len(poe_raw) else 0
        poe_mode = POE_MODE_MAP.get(poe_code, "off")

        poes_raw = link_data.get("poes", [])
        poes_code = poes_raw[index] if index < len(poes_raw) else 0
        poe_status = POE_STATUS_MAP.get(poes_code, f"code {poes_code}")

        curr_raw = link_data.get("curr", [])
        curr_ma = curr_raw[index] if index < len(curr_raw) else 0

        pwr_raw = link_data.get("pwr", [])
        pwr_w = (pwr_raw[index] / 1000.0) if index < len(pwr_raw) else 0.0

        dvid_raw = fwd.get("dvid", [])
        dvid = dvid_raw[index] if index < len(dvid_raw) else 1

        vlan_raw = fwd.get("vlan", [])
        vlan_code = vlan_raw[index] if index < len(vlan_raw) else 1
        vlan_mode = VLAN_MODE_MAP.get(vlan_code, "optional")

        vlni_raw = fwd.get("vlni", [])
        vlni_code = vlni_raw[index] if index < len(vlni_raw) else 0
        vlan_receive = VLAN_RECV_MAP.get(vlni_code, "any")

        vlnh_raw = fwd.get("vlnh", [])
        vlnh_code = vlnh_raw[index] if index < len(vlnh_raw) else 0
        vlan_header = VLAN_HEADER_MAP.get(vlnh_code, "leave as is")

        return cls(
            index=index,
            name=name,
            enabled=en,
            link_up=lnk,
            speed=speed,
            duplex=dpx,
            auto_negotiation=an,
            flow_control=fct,
            poe_mode=poe_mode,
            poe_status=poe_status,
            poe_current_ma=curr_ma,
            poe_power_w=pwr_w,
            default_vlan_id=dvid,
            vlan_mode=vlan_mode,
            vlan_receive=vlan_receive,
            vlan_header=vlan_header,
        )


@dataclass
class PortStats:
    """Traffic and error statistics for a single port."""
    index: int
    name: str
    rx_rate_bps: float
    tx_rate_bps: float
    rx_packet_rate: float
    tx_packet_rate: float
    rx_bytes: int
    tx_bytes: int
    rx_packets: int
    tx_packets: int
    rx_unicast: int
    tx_unicast: int
    rx_broadcast: int
    tx_broadcast: int
    rx_multicast: int
    tx_multicast: int
    rx_errors: int
    tx_errors: int
    rx_fcs_errors: int
    rx_align_errors: int
    rx_runts: int
    rx_fragments: int
    rx_too_long: int
    rx_overflows: int
    tx_collisions: int
    tx_excessive_collisions: int
    tx_late_collisions: int
    tx_underruns: int
    tx_too_long: int
    rx_pauses: int
    tx_pauses: int

    @property
    def rx_bytes_str(self) -> str:
        return format_bytes(self.rx_bytes)

    @property
    def tx_bytes_str(self) -> str:
        return format_bytes(self.tx_bytes)

    @property
    def rx_rate_str(self) -> str:
        return format_rate(self.rx_rate_bps)

    @property
    def tx_rate_str(self) -> str:
        return format_rate(self.tx_rate_bps)

    @classmethod
    def from_stats(cls, index: int, name: str, stats: Dict[str, Any]) -> PortStats:
        def get_stat(key: str) -> int:
            arr = stats.get(key, [])
            return arr[index] if index < len(arr) else 0

        # SwOS rate formulas from engine.js:
        # scale 0.08 for rates: rate_bps = raw / 0.08 = raw * 12.5
        # scale 0.64 for packet rates: pkt_rate = raw / 0.64 = raw * 1.5625
        rrb = get_stat("rrb") / 0.08
        trb = get_stat("trb") / 0.08
        rrp = get_stat("rrp") / 0.64
        trp = get_stat("trp") / 0.64

        return cls(
            index=index,
            name=name,
            rx_rate_bps=rrb,
            tx_rate_bps=trb,
            rx_packet_rate=rrp,
            tx_packet_rate=trp,
            rx_bytes=get_stat("rb"),
            tx_bytes=get_stat("tb"),
            rx_packets=get_stat("rtp"),
            tx_packets=get_stat("ttp"),
            rx_unicast=get_stat("rup"),
            tx_unicast=get_stat("tup"),
            rx_broadcast=get_stat("rbp"),
            tx_broadcast=get_stat("tbp"),
            rx_multicast=get_stat("rmp"),
            tx_multicast=get_stat("tmp"),
            rx_errors=get_stat("rte"),
            tx_errors=get_stat("tte"),
            rx_fcs_errors=get_stat("rfcs"),
            rx_align_errors=get_stat("rae"),
            rx_runts=get_stat("rr"),
            rx_fragments=get_stat("fr"),
            rx_too_long=get_stat("rtl"),
            rx_overflows=get_stat("rov"),
            tx_collisions=get_stat("tcl"),
            tx_excessive_collisions=get_stat("tec"),
            tx_late_collisions=get_stat("tlc"),
            tx_underruns=get_stat("tur"),
            tx_too_long=get_stat("ttl"),
            rx_pauses=get_stat("rpp"),
            tx_pauses=get_stat("tpp"),
        )


@dataclass
class HostEntry:
    """Host MAC address table entry (dynamic or static)."""
    mac: str
    port: int
    port_name: str
    vlan_id: int
    dynamic: bool
    drop: bool = False
    mirror: bool = False

    @classmethod
    def from_dict(cls, data: Dict[str, Any], port_names: List[str], dynamic: bool = True) -> HostEntry:
        prt = data.get("prt", 0)
        pname = port_names[prt] if prt < len(port_names) else f"Port{prt+1}"
        return cls(
            mac=decode_mac(data.get("adr")),
            port=prt,
            port_name=pname,
            vlan_id=data.get("vid", 0),
            dynamic=dynamic,
            drop=bool(data.get("drp", 0)),
            mirror=bool(data.get("mir", 0)),
        )


@dataclass
class VlanEntry:
    """Static VLAN membership entry from vlan.b."""
    vlan_id: int
    ivl: bool
    igmp_snooping: bool
    ports: List[int]
    port_names: List[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any], all_port_names: List[str]) -> VlanEntry:
        prt_val = data.get("prt", [])
        # In SwOS vlan.b, prt is list of port handling codes (0: leave as is, 1: always strip, 2: add if missing, 3: not a member)
        member_ports = []
        member_names = []
        if isinstance(prt_val, list):
            for idx, mode in enumerate(prt_val):
                if mode != 3:  # 3 is 'not a member'
                    member_ports.append(idx)
                    pname = all_port_names[idx] if idx < len(all_port_names) else f"Port{idx+1}"
                    member_names.append(pname)
        elif isinstance(prt_val, int):
            member_ports = bitmask_to_ports(prt_val, len(all_port_names))
            member_names = [all_port_names[i] for i in member_ports if i < len(all_port_names)]

        return cls(
            vlan_id=data.get("vid", 1),
            ivl=bool(data.get("ivl", 0)),
            igmp_snooping=bool(data.get("igmp", 0)),
            ports=member_ports,
            port_names=member_names,
        )


@dataclass
class SfpInfo:
    """SFP optical transceiver diagnostic information from sfp.b."""
    present: bool
    vendor: str
    part_number: str
    revision: str
    serial: str
    date: str
    sfp_type: str
    temperature_c: Optional[float]
    voltage_v: Optional[float]
    tx_bias_ma: Optional[float]
    tx_power_dbm: Optional[float]
    rx_power_dbm: Optional[float]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SfpInfo:
        vnd = decode_hex_str(data.get("vnd", ""))
        pnr = decode_hex_str(data.get("pnr", ""))
        present = bool(vnd or pnr)
        
        tmp_raw = data.get("tmp", 0)
        tmp_c = float(decode_signed32(tmp_raw)) if present else None
        
        vcc_raw = data.get("vcc", 0)
        vcc_v = (vcc_raw / 10000.0) if present and vcc_raw else None
        
        tbs_raw = data.get("tbs", 0)
        tbs_ma = (tbs_raw / 500.0) if present and tbs_raw else None

        tpw_raw = data.get("tpw", 0)
        tpw_dbm = (tpw_raw / 10000.0) if present and tpw_raw else None

        rpw_raw = data.get("rpw", 0)
        rpw_dbm = (rpw_raw / 10000.0) if present and rpw_raw else None

        return cls(
            present=present,
            vendor=vnd,
            part_number=pnr,
            revision=decode_hex_str(data.get("rev", "")),
            serial=decode_hex_str(data.get("ser", "")),
            date=decode_hex_str(data.get("dat", "")),
            sfp_type=decode_hex_str(data.get("typ", "")),
            temperature_c=tmp_c,
            voltage_v=vcc_v,
            tx_bias_ma=tbs_ma,
            tx_power_dbm=tpw_dbm,
            rx_power_dbm=rpw_dbm,
        )


@dataclass
class SnmpInfo:
    """SNMP configuration from snmp.b."""
    enabled: bool
    community: str
    contact: str
    location: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SnmpInfo:
        return cls(
            enabled=bool(data.get("en", 0)),
            community=decode_hex_str(data.get("com", "")),
            contact=decode_hex_str(data.get("ci", "")),
            location=decode_hex_str(data.get("loc", "")),
        )
