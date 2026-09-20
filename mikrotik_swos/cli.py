"""Command-line interface for administering MikroTik SwOS switches."""

from __future__ import annotations

import datetime
import json
import os
import re
import signal
import sys
from typing import Any, List, Optional

import shutil

import click

from mikrotik_swos.client import SwOSAuthError, SwOSClient, SwOSConnectionError, SwOSError
from mikrotik_swos.codec import bitmask_to_ports, decode_hex_str, decode_ip, decode_mac, ports_to_bitmask
from mikrotik_swos.tui import run_monitor


def format_table(
    headers: List[str],
    rows: List[List[str]],
    aligns: Optional[List[str]] = None,
) -> str:
    """Format tabular data with natural column widths, zero truncation, and clean ASCII separators."""
    if not headers:
        return ""
    if not rows:
        sep = "-" * max(len(h) for h in headers)
        return "  ".join(headers) + "\n" + "  ".join(sep for _ in headers)

    aligns = aligns or ["<"] * len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(val)))
            else:
                widths.append(len(str(val)))

    fmt_parts = [f"{{{i}:{aligns[i] if i < len(aligns) else '<'}{widths[i]}}}" for i in range(len(widths))]
    fmt = "  ".join(fmt_parts)

    hdr_vals = [headers[i] if i < len(headers) else "" for i in range(len(widths))]
    hdr_line = fmt.format(*hdr_vals).rstrip()
    sep_line = "  ".join("-" * w for w in widths).rstrip()
    data_lines = [
        fmt.format(*[str(row[i]) if i < len(row) else "" for i in range(len(widths))]).rstrip()
        for row in rows
    ]
    return "\n".join([hdr_line, sep_line, *data_lines])


_MARKUP_RE = re.compile(r"\[/?(?:bold|dim|green|red|cyan|yellow|magenta|white|blue)[^\]]*\]")


def strip_markup(text: Any) -> str:
    return _MARKUP_RE.sub("", str(text))


def print_err(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)


class PlainConsole:
    """Minimal plain-text console replacement for backwards compatibility."""

    def print(self, *args: Any, **kwargs: Any) -> None:
        file = kwargs.get("file", sys.stdout)
        clean_args = [strip_markup(a) if isinstance(a, str) else a for a in args]
        print(*clean_args, file=file)


console = PlainConsole()


def get_client(ctx: click.Context) -> SwOSClient:
    return ctx.obj["client"]


def is_json(ctx: click.Context) -> bool:
    return ctx.obj.get("json_output", False)


def output_data(ctx: click.Context, data: Any, render_func) -> None:
    if is_json(ctx):
        print(json.dumps(data, indent=2, default=str))
    else:
        render_func()


@click.group(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("-H", "--host", default=lambda: os.environ.get("SWOS_HOST", "192.168.88.1"), help="Switch IP address or hostname [default: 192.168.88.1]")
@click.option("-u", "--user", default=lambda: os.environ.get("SWOS_USER", "admin"), help="SwOS username [default: admin]")
@click.option("-p", "--password", default=lambda: os.environ.get("SWOS_PASSWORD", ""), help="SwOS password [default: empty]")
@click.option("--timeout", default=5.0, type=float, help="HTTP request timeout in seconds [default: 5.0]")
@click.option("--debug", is_flag=True, help="Enable verbose HTTP debug logging")
@click.option("--json", "json_output", is_flag=True, help="Output data as JSON")
@click.pass_context
def cli(ctx: click.Context, host: str, user: str, password: str, timeout: float, debug: bool, json_output: bool):
    """MikroTik SwOS Command-Line Administration Tool."""
    ctx.ensure_object(dict)
    client = SwOSClient(host=host, username=user, password=password, timeout=timeout, debug=debug)
    ctx.obj["client"] = client
    ctx.obj["json_output"] = json_output


# --- SYSTEM / INFO ---

@cli.command("system")
@click.pass_context
def cmd_system(ctx: click.Context):
    """Display switch system, hardware, and network operational status."""
    client = get_client(ctx)
    try:
        info = client.get_system()
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    data = {
        "identity": info.identity,
        "board": info.board,
        "version": info.version,
        "build_datetime": info.build_datetime.isoformat(),
        "serial": info.serial,
        "mac": info.mac,
        "ip": info.ip,
        "static_ip": info.static_ip,
        "ip_mode": info.ip_mode,
        "uptime_centisec": info.uptime_centisec,
        "uptime": info.uptime_str,
        "watchdog": info.watchdog,
        "discovery": info.discovery,
        "ivl": info.ivl,
        "igmp_snooping": info.igmp_snooping,
        "allow_from": f"{info.allow_from_ip}/{info.allow_from_mask}",
        "allow_ports": [p + 1 for p in info.allow_from_ports],
        "allow_vlan": info.allow_from_vlan,
        "bridge_priority": hex(info.bridge_priority),
        "root_bridge": f"{hex(info.root_bridge_priority)}.{info.root_bridge_mac}",
    }

    def render():
        rows = [
            ["Identity", info.identity],
            ["Board Model", info.board],
            ["SwOS Firmware Version", f"{info.version} (Build: {info.build_datetime.strftime('%Y-%m-%d %H:%M:%S UTC')})"],
            ["Serial Number", info.serial],
            ["MAC Address", info.mac],
            ["Current IP Address", info.ip],
            ["Static IP Address", info.static_ip],
            ["IP Acquisition Mode", info.ip_mode],
            ["System Uptime", f"{info.uptime_str} ({info.uptime_centisec / 100:.1f}s)"],
            ["Watchdog Timer", "Enabled" if info.watchdog else "Disabled"],
            ["MikroTik Discovery Protocol", "Enabled" if info.discovery else "Disabled"],
            ["Independent VLAN Lookup (IVL)", "Enabled" if info.ivl else "Disabled"],
            ["IGMP Snooping", "Enabled" if info.igmp_snooping else "Disabled"],
            ["Allow Admin From IP", f"{info.allow_from_ip}/{info.allow_from_mask}"],
            ["Allow Admin From Ports", ", ".join(f"Port{p+1}" for p in info.allow_from_ports)],
            ["Allow Admin From VLAN", str(info.allow_from_vlan)],
            ["Bridge Priority", f"{hex(info.bridge_priority)} ({info.bridge_priority})"],
            ["Root Bridge", f"{hex(info.root_bridge_priority)}.{info.root_bridge_mac}"],
        ]
        print(f"System Information: {info.identity} ({info.board})")
        print(format_table(["Property", "Value"], rows))

    output_data(ctx, data, render)


# --- PORTS ---

@cli.command("ports")
@click.pass_context
def cmd_ports(ctx: click.Context):
    """Display port status, link parameters, and PoE settings."""
    client = get_client(ctx)
    try:
        ports = client.get_ports()
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    data = [
        {
            "index": p.index,
            "port": p.index + 1,
            "name": p.name,
            "enabled": p.enabled,
            "link": p.link_up,
            "speed": p.speed,
            "duplex": p.duplex,
            "auto_neg": p.auto_negotiation,
            "flow_control": p.flow_control,
            "default_vlan": p.default_vlan_id,
            "vlan_mode": p.vlan_mode,
            "poe_mode": p.poe_mode,
            "poe_status": p.poe_status,
            "poe_current_ma": p.poe_current_ma,
            "poe_power_w": p.poe_power_w,
        }
        for p in ports
    ]

    def render():
        headers = ["#", "Name", "State", "Link", "Speed", "Duplex", "AutoNeg", "FlowCtrl", "PVID", "VLAN Mode", "PoE Mode", "PoE W"]
        aligns = [">", "<", "<", "<", "<", "<", "<", "<", ">", "<", "<", ">"]
        rows = []
        for p in ports:
            state_str = "Enabled" if p.enabled else "Disabled"
            link_str = "UP" if p.link_up else "DOWN"
            speed_str = p.speed if p.link_up else "-"
            duplex_str = p.duplex if p.link_up else "-"
            an_str = "Yes" if p.auto_negotiation else "No"
            fct_str = "Yes" if p.flow_control else "No"
            pwr_str = f"{p.poe_power_w:.1f} W" if p.poe_power_w > 0 else "-"
            rows.append([
                str(p.index + 1),
                p.name,
                state_str,
                link_str,
                speed_str,
                duplex_str,
                an_str,
                fct_str,
                str(p.default_vlan_id),
                p.vlan_mode,
                p.poe_mode,
                pwr_str,
            ])
        print("SwOS Port Overview:")
        print(format_table(headers, rows, aligns))

    output_data(ctx, data, render)


# --- STATS ---

@cli.command("stats")
@click.option("--errors", is_flag=True, help="Display detailed error counters per port")
@click.option("--packets", is_flag=True, help="Display packet size distribution counters")
@click.pass_context
def cmd_stats(ctx: click.Context, errors: bool, packets: bool):
    """Display traffic statistics and error counters for each port."""
    client = get_client(ctx)
    try:
        stats = client.get_stats()
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    data = [
        {
            "port": s.index + 1,
            "name": s.name,
            "rx_bytes": s.rx_bytes,
            "tx_bytes": s.tx_bytes,
            "rx_rate_bps": s.rx_rate_bps,
            "tx_rate_bps": s.tx_rate_bps,
            "rx_packets": s.rx_packets,
            "tx_packets": s.tx_packets,
            "rx_unicast": s.rx_unicast,
            "tx_unicast": s.tx_unicast,
            "rx_broadcast": s.rx_broadcast,
            "tx_broadcast": s.tx_broadcast,
            "rx_multicast": s.rx_multicast,
            "tx_multicast": s.tx_multicast,
            "rx_errors": s.rx_errors,
            "tx_errors": s.tx_errors,
            "rx_fcs_errors": s.rx_fcs_errors,
            "rx_runts": s.rx_runts,
            "rx_fragments": s.rx_fragments,
            "tx_collisions": s.tx_collisions,
        }
        for s in stats
    ]

    def render():
        if errors:
            headers = ["Port", "Rx Errors", "Tx Errors", "Rx FCS", "Rx Align", "Rx Runts", "Rx Frag", "Rx TooLong", "Tx Collis", "Tx Underrun"]
            aligns = ["<"] + [">"] * 9
            rows = []
            for s in stats:
                rows.append([
                    s.name,
                    str(s.rx_errors),
                    str(s.tx_errors),
                    str(s.rx_fcs_errors),
                    str(s.rx_align_errors),
                    str(s.rx_runts),
                    str(s.rx_fragments),
                    str(s.rx_too_long),
                    str(s.tx_collisions),
                    str(s.tx_underruns),
                ])
            print("Port Error Counters:")
            print(format_table(headers, rows, aligns))
        else:
            headers = ["Port", "Rx Rate", "Tx Rate", "Rx Bytes", "Tx Bytes", "Rx Packets", "Tx Packets", "Rx Ucast", "Tx Ucast", "Rx Bcast", "Tx Bcast", "Errors (Rx/Tx)"]
            aligns = ["<", ">", ">", ">", ">", ">", ">", ">", ">", ">", ">", "<"]
            rows = []
            for s in stats:
                err_str = f"{s.rx_errors}/{s.tx_errors}"
                rows.append([
                    s.name,
                    s.rx_rate_str,
                    s.tx_rate_str,
                    s.rx_bytes_str,
                    s.tx_bytes_str,
                    f"{s.rx_packets:,}",
                    f"{s.tx_packets:,}",
                    f"{s.rx_unicast:,}",
                    f"{s.tx_unicast:,}",
                    f"{s.rx_broadcast:,}",
                    f"{s.tx_broadcast:,}",
                    err_str,
                ])
            print("Port Traffic Counters:")
            print(format_table(headers, rows, aligns))

    output_data(ctx, data, render)


# --- HOSTS ---

@cli.command("hosts")
@click.option("--port", type=int, help="Filter by port number (1-6)")
@click.option("--vlan", type=int, help="Filter by VLAN ID")
@click.option("--static-only", is_flag=True, help="Show only static hosts")
@click.option("--dynamic-only", is_flag=True, help="Show only learned dynamic hosts")
@click.pass_context
def cmd_hosts(ctx: click.Context, port: Optional[int], vlan: Optional[int], static_only: bool, dynamic_only: bool):
    """Display learned dynamic MAC addresses and static MAC table."""
    client = get_client(ctx)
    inc_dyn = not static_only
    inc_sta = not dynamic_only
    try:
        hosts = client.get_hosts(dynamic=inc_dyn, static=inc_sta)
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    if port is not None:
        hosts = [h for h in hosts if (h.port + 1) == port]
    if vlan is not None:
        hosts = [h for h in hosts if h.vlan_id == vlan]

    data = [
        {
            "mac": h.mac,
            "port": h.port + 1,
            "port_name": h.port_name,
            "vlan_id": h.vlan_id,
            "dynamic": h.dynamic,
            "drop": h.drop,
            "mirror": h.mirror,
        }
        for h in hosts
    ]

    def render():
        headers = ["MAC Address", "Port", "VLAN ID", "Type", "Drop", "Mirror"]
        aligns = ["<", "<", ">", "<", "<", "<"]
        rows = []
        for h in hosts:
            rows.append([
                h.mac,
                h.port_name,
                str(h.vlan_id),
                "Dynamic" if h.dynamic else "Static",
                "Yes" if h.drop else "No",
                "Yes" if h.mirror else "No",
            ])
        print(f"MAC Address Host Table ({len(hosts)} entries):")
        print(format_table(headers, rows, aligns))

    output_data(ctx, data, render)


# --- VLAN ---

@cli.command("vlan")
@click.pass_context
def cmd_vlan(ctx: click.Context):
    """Display VLAN configuration and static VLAN table."""
    client = get_client(ctx)
    try:
        ports = client.get_ports()
        vlans = client.get_vlans()
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    data = {
        "ports": [
            {
                "port": p.index + 1,
                "name": p.name,
                "default_vlan_id": p.default_vlan_id,
                "vlan_mode": p.vlan_mode,
                "vlan_receive": p.vlan_receive,
                "vlan_header": p.vlan_header,
            }
            for p in ports
        ],
        "static_vlans": [
            {
                "vlan_id": v.vlan_id,
                "ivl": v.ivl,
                "igmp": v.igmp_snooping,
                "ports": [x + 1 for x in v.ports],
                "port_names": v.port_names,
            }
            for v in vlans
        ],
    }

    def render():
        p_headers = ["Port", "PVID (Default VID)", "VLAN Mode", "VLAN Receive", "VLAN Header"]
        p_aligns = ["<", ">", "<", "<", "<"]
        p_rows = [
            [p.name, str(p.default_vlan_id), p.vlan_mode, p.vlan_receive, p.vlan_header]
            for p in ports
        ]
        print("Per-Port VLAN Modes:")
        print(format_table(p_headers, p_rows, p_aligns))
        print("")

        v_headers = ["VLAN ID", "IVL", "IGMP Snooping", "Member Ports"]
        v_aligns = [">", "<", "<", "<"]
        v_rows = []
        if not vlans:
            v_rows.append(["None", "-", "-", "No static VLANs defined"])
        else:
            for v in vlans:
                ports_str = ", ".join(v.port_names) if v.port_names else "None"
                v_rows.append([
                    str(v.vlan_id),
                    "Yes" if v.ivl else "No",
                    "Yes" if v.igmp_snooping else "No",
                    ports_str,
                ])
        print(f"Static VLAN Table ({len(vlans)} entries):")
        print(format_table(v_headers, v_rows, v_aligns))

    output_data(ctx, data, render)


# --- FORWARDING ---

@cli.command("fwd")
@click.pass_context
def cmd_fwd(ctx: click.Context):
    """Display port forwarding matrix and mirroring configuration."""
    client = get_client(ctx)
    try:
        fwd = client.get_forwarding()
        link_data = client.get("link.b")
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    names = [decode_hex_str(x) for x in link_data.get("nm", [])]

    # Parse forwarding matrix
    matrix = {}
    for i in range(len(names)):
        key = f"fp{i+1}"
        mask = fwd.get(key, 0)
        matrix[names[i]] = [names[p] for p in bitmask_to_ports(mask, len(names))]

    data = {
        "forwarding_matrix": matrix,
        "mirror_ingress": bitmask_to_ports(fwd.get("imr", 0), len(names)),
        "mirror_egress": bitmask_to_ports(fwd.get("omr", 0), len(names)),
        "mirror_to_port": fwd.get("mrto", 0) + 1,
    }

    def render():
        headers = ["Ingress Port", "Can Forward To Ports"]
        rows = []
        for src, dsts in matrix.items():
            rows.append([src, ", ".join(dsts) if dsts else "Isolated"])
        print("Port Forwarding Isolation Matrix:")
        print(format_table(headers, rows))
        print("")

        mrto = fwd.get("mrto", 0)
        target_name = names[mrto] if mrto < len(names) else f"Port{mrto+1}"
        imr_ports = [names[p] for p in bitmask_to_ports(fwd.get("imr", 0), len(names))]
        omr_ports = [names[p] for p in bitmask_to_ports(fwd.get("omr", 0), len(names))]

        m_headers = ["Parameter", "Value"]
        m_rows = [
            ["Mirror Target Port", target_name],
            ["Mirror Ingress From", ", ".join(imr_ports) if imr_ports else "None"],
            ["Mirror Egress From", ", ".join(omr_ports) if omr_ports else "None"],
        ]
        print("Port Mirroring:")
        print(format_table(m_headers, m_rows))

    output_data(ctx, data, render)


# --- RSTP ---

@cli.command("rstp")
@click.pass_context
def cmd_rstp(ctx: click.Context):
    """Display RSTP spanning tree bridge and per-port states."""
    client = get_client(ctx)
    try:
        rstp = client.get_rstp()
        sys_data = client.get("sys.b")
        link_data = client.get("link.b")
    except SwOSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    names = [decode_hex_str(x) for x in link_data.get("nm", [])]
    roles = ["disabled", "alternate", "root", "designated", "backup"]

    data = {
        "bridge_priority": hex(sys_data.get("prio", 0x8000)),
        "root_bridge": f"{hex(sys_data.get('rpr', 0x8000))}.{decode_mac(sys_data.get('rmac', ''))}",
        "ports": [],
    }

    role_list = rstp.get("role", [])
    cst_list = rstp.get("cst", [])
    rpc_list = rstp.get("rpc", [])

    for i in range(len(names)):
        en = bool(rstp.get("ena", 0) & (1 << i))
        role_code = role_list[i] if i < len(role_list) else 0
        role_str = roles[role_code] if role_code < len(roles) else f"code {role_code}"
        cost = cst_list[i] if i < len(cst_list) else 0
        rpc = rpc_list[i] if i < len(rpc_list) else 0
        p2p = bool(rstp.get("p2p", 0) & (1 << i))
        edge = bool(rstp.get("edge", 0) & (1 << i))

        data["ports"].append({
            "port": i + 1,
            "name": names[i],
            "enabled": en,
            "role": role_str,
            "path_cost": cost,
            "root_path_cost": rpc,
            "p2p": p2p,
            "edge": edge,
        })

    def render():
        print(f"Spanning Tree (RSTP): Root Bridge: {data['root_bridge']}  Bridge Priority: {data['bridge_priority']}")
        print("")
        headers = ["Port", "RSTP", "Role", "Path Cost", "Root Path Cost", "P2P", "Edge"]
        aligns = ["<", "<", "<", ">", ">", "<", "<"]
        rows = []
        for p in data["ports"]:
            rows.append([
                p["name"],
                "Enabled" if p["enabled"] else "Disabled",
                p["role"],
                str(p["path_cost"]),
                str(p["root_path_cost"]),
                "Yes" if p["p2p"] else "No",
                "Yes" if p["edge"] else "No",
            ])
        print("RSTP Per-Port Status:")
        print(format_table(headers, rows, aligns))

    output_data(ctx, data, render)


# --- SFP ---

@cli.command("sfp")
@click.pass_context
def cmd_sfp(ctx: click.Context):
    """Display SFP optical transceiver diagnostics (DDM)."""
    client = get_client(ctx)
    try:
        sfp = client.get_sfp()
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    data = {
        "present": sfp.present,
        "vendor": sfp.vendor,
        "part_number": sfp.part_number,
        "revision": sfp.revision,
        "serial": sfp.serial,
        "date": sfp.date,
        "type": sfp.sfp_type,
        "temperature_c": sfp.temperature_c,
        "voltage_v": sfp.voltage_v,
        "tx_bias_ma": sfp.tx_bias_ma,
        "tx_power_dbm": sfp.tx_power_dbm,
        "rx_power_dbm": sfp.rx_power_dbm,
    }

    def render():
        headers = ["Parameter", "Value"]
        rows = []
        if not sfp.present:
            rows.append(["Module Status", "No SFP transceiver detected / Module not present"])
        else:
            rows.append(["Vendor", sfp.vendor])
            rows.append(["Part Number", sfp.part_number])
            rows.append(["Revision", sfp.revision])
            rows.append(["Serial Number", sfp.serial])
            rows.append(["Manufacturing Date", sfp.date])
            rows.append(["Transceiver Type", sfp.sfp_type])
            if sfp.temperature_c is not None:
                rows.append(["Temperature", f"{sfp.temperature_c:.1f} °C"])
            if sfp.voltage_v is not None:
                rows.append(["Supply Voltage", f"{sfp.voltage_v:.2f} V"])
            if sfp.tx_bias_ma is not None:
                rows.append(["Tx Bias Current", f"{sfp.tx_bias_ma:.2f} mA"])
            if sfp.tx_power_dbm is not None:
                rows.append(["Tx Power", f"{sfp.tx_power_dbm:.2f} dBm"])
            if sfp.rx_power_dbm is not None:
                rows.append(["Rx Power", f"{sfp.rx_power_dbm:.2f} dBm"])
        print("SFP Optical Transceiver Information:")
        print(format_table(headers, rows))

    output_data(ctx, data, render)


# --- SNMP ---

@cli.command("snmp")
@click.pass_context
def cmd_snmp(ctx: click.Context):
    """Display SNMP settings."""
    client = get_client(ctx)
    try:
        snmp = client.get_snmp()
    except SwOSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    data = {
        "enabled": snmp.enabled,
        "community": snmp.community,
        "contact": snmp.contact,
        "location": snmp.location,
    }

    def render():
        headers = ["Setting", "Value"]
        rows = [
            ["SNMP Service", "Enabled" if snmp.enabled else "Disabled"],
            ["Community", snmp.community],
            ["Contact Info", snmp.contact or "Not set"],
            ["Location", snmp.location or "Not set"],
        ]
        print("SNMP Configuration:")
        print(format_table(headers, rows))

    output_data(ctx, data, render)


# --- ACL ---

@cli.command("acl")
@click.pass_context
def cmd_acl(ctx: click.Context):
    """Display Access Control List (ACL) filter rules."""
    client = get_client(ctx)
    try:
        rules = client.get_acl()
    except SwOSError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    def render():
        headers = ["#", "From", "MAC Match", "IP Match", "Action"]
        aligns = [">", "<", "<", "<", "<"]
        rows = []
        if not rules:
            rows.append(["-", "-", "No ACL rules configured", "-", "-"])
        else:
            for idx, r in enumerate(rules):
                rows.append([
                    str(idx + 1),
                    bin(r.get("frm", 0)),
                    f"Src:{decode_mac(r.get('smac'))} Dst:{decode_mac(r.get('dmac'))}",
                    f"Src:{decode_ip(r.get('sip'))} Dst:{decode_ip(r.get('dip'))}",
                    f"Redirect:{r.get('snd', '-')} Mirror:{r.get('mirr', 0)} Rate:{r.get('rate', '-')}",
                ])
        print(f"Access Control List Rules ({len(rules)} rules):")
        print(format_table(headers, rows, aligns))

    output_data(ctx, rules, render)


# --- SET-PORT ---

@cli.command("set-port")
@click.argument("port")
@click.option("--name", help="New port name")
@click.option("--enable/--disable", default=None, help="Enable or disable port")
@click.option("--auto-neg/--no-auto-neg", default=None, help="Enable/disable auto negotiation")
@click.option("--speed", type=click.Choice(["10M", "100M", "1G"], case_sensitive=False), help="Forced link speed")
@click.option("--duplex", type=click.Choice(["full", "half"], case_sensitive=False), help="Forced duplex mode")
@click.option("--flow-control/--no-flow-control", default=None, help="Enable/disable flow control")
@click.option("--poe", type=click.Choice(["off", "auto", "on", "calibr"], case_sensitive=False), help="PoE output mode")
@click.option("--pvid", type=int, help="Default VLAN ID (PVID)")
@click.pass_context
def cmd_set_port(
    ctx: click.Context,
    port: str,
    name: Optional[str],
    enable: Optional[bool],
    auto_neg: Optional[bool],
    speed: Optional[str],
    duplex: Optional[str],
    flow_control: Optional[bool],
    poe: Optional[str],
    pvid: Optional[int],
):
    """Configure port settings (speed, duplex, PoE, PVID, name)."""
    client = get_client(ctx)
    # Resolve port argument (e.g. '1', 'Port1', 'sfp')
    try:
        ports = client.get_ports()
    except SwOSError as e:
        print_err(str(e))
        sys.exit(1)

    port_idx = None
    if port.isdigit():
        idx = int(port) - 1
        if 0 <= idx < len(ports):
            port_idx = idx
    else:
        for p in ports:
            if p.name.lower() == port.lower() or f"port{p.index+1}" == port.lower():
                port_idx = p.index
                break

    if port_idx is None:
        print_err(f"Unknown port '{port}'. Available: {', '.join(p.name for p in ports)}")
        sys.exit(1)

    print(f"Updating port #{port_idx+1} ({ports[port_idx].name})...")
    try:
        client.set_port(
            port_index=port_idx,
            name=name,
            enabled=enable,
            auto_negotiation=auto_neg,
            speed=speed,
            duplex=duplex,
            flow_control=flow_control,
            poe=poe,
            default_vlan_id=pvid,
        )
        updated_ports = client.get_ports()
        up = updated_ports[port_idx]
        print(f"✓ Port #{port_idx+1} updated successfully: {up.name} (enabled={up.enabled}, speed={up.speed}, poe={up.poe_mode}, pvid={up.default_vlan_id})")
    except SwOSError as e:
        print_err(f"Failed to update port: {e}")
        sys.exit(1)


# --- SET-SYSTEM ---

@cli.command("set-system")
@click.option("--identity", help="Set switch system identity name")
@click.option("--watchdog", type=click.Choice(["0", "1"]), help="Enable (1) or disable (0) hardware watchdog")
@click.option("--discovery", type=click.Choice(["0", "1"]), help="Enable (1) or disable (0) MikroTik discovery protocol")
@click.option("--ivl", type=click.Choice(["0", "1"]), help="Enable (1) or disable (0) Independent VLAN Lookup")
@click.option("--igmp", type=click.Choice(["0", "1"]), help="Enable (1) or disable (0) IGMP snooping")
@click.option("--static-ip", help="Set static IPv4 address (e.g. 192.168.88.1)")
@click.option("--ip-mode", type=click.Choice(["dhcp", "static", "dhcp-only"], case_sensitive=False), help="Address acquisition mode")
@click.option("--allow-from", help="Allowed subnet for management access (e.g. 192.168.88.0/24 or 0.0.0.0/0)")
@click.option("--allow-ports", help="Comma-separated ports allowed for management access (e.g. 1,2,3,4,5,6)")
@click.option("--allow-vlan", type=int, help="VLAN ID allowed for management access (0 for any)")
@click.option("--poe-in-long-cable", type=click.Choice(["0", "1"]), help="Port1 PoE In Long Cable mode (0 or 1)")
@click.pass_context
def cmd_set_system(
    ctx: click.Context,
    identity: Optional[str],
    watchdog: Optional[str],
    discovery: Optional[str],
    ivl: Optional[str],
    igmp: Optional[str],
    static_ip: Optional[str],
    ip_mode: Optional[str],
    allow_from: Optional[str],
    allow_ports: Optional[str],
    allow_vlan: Optional[int],
    poe_in_long_cable: Optional[str],
):
    """Modify switch system identity, discovery, management filters, or IP settings."""
    client = get_client(ctx)
    wdt = (watchdog == "1") if watchdog is not None else None
    dsc = (discovery == "1") if discovery is not None else None
    ivl_val = (ivl == "1") if ivl is not None else None
    igmp_val = (igmp == "1") if igmp is not None else None
    lcbl_val = (poe_in_long_cable == "1") if poe_in_long_cable is not None else None
    iptp_map = {"dhcp": 0, "static": 1, "dhcp-only": 2}
    mode_val = iptp_map.get(ip_mode.lower()) if ip_mode else None

    port_indices = None
    if allow_ports is not None:
        port_indices = [int(p.strip()) - 1 for p in allow_ports.split(",") if p.strip().isdigit()]

    print("Updating system configuration...")
    try:
        client.set_system(
            identity=identity,
            watchdog=wdt,
            discovery=dsc,
            ivl=ivl_val,
            igmp=igmp_val,
            static_ip=static_ip,
            ip_mode=mode_val,
            allow_from=allow_from,
            allow_ports=port_indices,
            allow_vlan=allow_vlan,
            poe_in_long_cable=lcbl_val,
        )
        info = client.get_system()
        print(
            f"✓ System updated successfully: "
            f"Identity={info.identity}, IP={info.ip}, Mode={info.ip_mode}, "
            f"Watchdog={info.watchdog}, AllowFrom={info.allow_from_ip}/{info.allow_from_mask}, "
            f"AllowPorts={[p+1 for p in info.allow_from_ports]}, AllowVLAN={info.allow_from_vlan}"
        )
    except SwOSError as e:
        print_err(f"Failed to update system: {e}")
        sys.exit(1)


# --- SET-PASSWORD ---

@cli.command("set-password")
@click.option("--old-password", default="", help="Current administrator password [default: empty]")
@click.option("--new-password", prompt=True, hide_input=True, confirmation_prompt=True, help="New administrator password")
@click.pass_context
def cmd_set_password(ctx: click.Context, old_password: str, new_password: str):
    """Change administrative password on SwOS switch."""
    client = get_client(ctx)
    if not old_password and client.password:
        old_password = client.password

    print(f"Updating administrator password on {client.host}...")
    try:
        client.change_password(new_password=new_password, old_password=old_password)
        print("✓ Administrator password changed successfully.")
    except Exception as e:
        print_err(f"Failed to change password: {e}")
        sys.exit(1)


# --- SET-SNMP ---

@cli.command("set-snmp")
@click.option("--enable/--disable", default=None, help="Enable or disable SNMP service")
@click.option("--community", help="SNMP community string")
@click.option("--contact", help="SysContact info")
@click.option("--location", help="SysLocation info")
@click.pass_context
def cmd_set_snmp(ctx: click.Context, enable: Optional[bool], community: Optional[str], contact: Optional[str], location: Optional[str]):
    """Update SNMP settings (community, contact, location)."""
    client = get_client(ctx)
    try:
        client.set_snmp(enabled=enable, community=community, contact=contact, location=location)
        snmp = client.get_snmp()
        print(f"✓ SNMP updated successfully: Enabled={snmp.enabled}, Community='{snmp.community}'")
    except SwOSError as e:
        print_err(f"Failed to update SNMP: {e}")
        sys.exit(1)


# --- ADD-VLAN / DEL-VLAN ---

@cli.command("add-vlan")
@click.argument("vlan_id", type=int)
@click.option("--ports", required=True, help="Comma-separated member port numbers (e.g. 1,2,3)")
@click.option("--ivl", is_flag=True, help="Enable Independent VLAN Lookup for this VLAN")
@click.option("--igmp", is_flag=True, help="Enable IGMP snooping for this VLAN")
@click.pass_context
def cmd_add_vlan(ctx: click.Context, vlan_id: int, ports: str, ivl: bool, igmp: bool):
    """Add or update a static VLAN table entry."""
    client = get_client(ctx)
    port_indices = [int(p.strip()) - 1 for p in ports.split(",") if p.strip().isdigit()]
    print(f"Adding VLAN {vlan_id} with ports {[p+1 for p in port_indices]}...")
    try:
        client.add_vlan(vlan_id=vlan_id, ports=port_indices, ivl=ivl, igmp=igmp)
        vlans = client.get_vlans()
        print(f"✓ VLAN {vlan_id} configured successfully. Total static VLANs: {len(vlans)}")
    except SwOSError as e:
        print_err(f"Failed to add VLAN: {e}")
        sys.exit(1)


@cli.command("del-vlan")
@click.argument("vlan_id", type=int)
@click.pass_context
def cmd_del_vlan(ctx: click.Context, vlan_id: int):
    """Remove a static VLAN table entry."""
    client = get_client(ctx)
    print(f"Removing VLAN {vlan_id}...")
    try:
        client.delete_vlan(vlan_id)
        print(f"✓ VLAN {vlan_id} removed successfully.")
    except SwOSError as e:
        print_err(f"Failed to remove VLAN: {e}")
        sys.exit(1)


# --- BACKUP / RESTORE ---

@cli.command("backup")
@click.argument("output_file", required=False)
@click.pass_context
def cmd_backup(ctx: click.Context, output_file: Optional[str]):
    """Export complete switch configuration state to a JSON backup file."""
    client = get_client(ctx)
    if not output_file:
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        output_file = f"swos-backup-{ts}.json"

    print(f"Exporting full configuration from {client.host}...")
    try:
        backup_dict = client.backup()
        with open(output_file, "w") as f:
            json.dump(backup_dict, f, indent=2)
        print(f"✓ Configuration successfully saved to: {output_file} ({os.path.getsize(output_file)} bytes)")
    except Exception as e:
        print_err(f"Backup failed: {e}")
        sys.exit(1)


@cli.command("restore")
@click.argument("input_file")
@click.option("--yes", is_flag=True, help="Confirm restore without interactive prompt")
@click.pass_context
def cmd_restore(ctx: click.Context, input_file: str, yes: bool):
    """Restore switch configuration state from a JSON backup file."""
    client = get_client(ctx)
    if not os.path.isfile(input_file):
        print_err(f"Backup file '{input_file}' not found.")
        sys.exit(1)

    if not yes:
        click.confirm(f"Are you sure you want to restore configuration from '{input_file}' to {client.host}?", abort=True)

    print(f"Restoring configuration to {client.host}...")
    try:
        with open(input_file) as f:
            backup_dict = json.load(f)
        results = client.restore(backup_dict)
        for ep, ok in results.items():
            status = "OK" if ok else "FAILED"
            print(f"  {ep}: {status}")
        print("✓ Configuration restore completed.")
    except Exception as e:
        print_err(f"Restore failed: {e}")
        sys.exit(1)


# --- REBOOT ---

@cli.command("reboot")
@click.option("--yes", is_flag=True, help="Confirm reboot without interactive prompt")
@click.pass_context
def cmd_reboot(ctx: click.Context, yes: bool):
    """Reboot the switch."""
    client = get_client(ctx)
    if not yes:
        click.confirm(f"Are you sure you want to REBOOT switch at {client.host}?", abort=True)

    print(f"Sending reboot signal to {client.host}...")
    try:
        client.reboot()
        print("✓ Reboot initiated. The switch is restarting.")
    except Exception as e:
        print_err(f"Reboot failed: {e}")
        sys.exit(1)


# --- RAW ---

@cli.group("raw")
def raw_group():
    """Low-level raw GET / POST access to SwOS endpoints."""
    pass


@raw_group.command("get")
@click.argument("endpoint")
@click.pass_context
def cmd_raw_get(ctx: click.Context, endpoint: str):
    """Fetch raw endpoint text (e.g. 'sys.b', 'link.b', '!stats.b')."""
    client = get_client(ctx)
    try:
        raw = client.get_raw(endpoint)
        print(raw)
    except Exception as e:
        print_err(str(e))
        sys.exit(1)


@raw_group.command("post")
@click.argument("endpoint")
@click.argument("payload")
@click.pass_context
def cmd_raw_post(ctx: click.Context, endpoint: str, payload: str):
    """Send raw POST payload to endpoint."""
    client = get_client(ctx)
    try:
        resp = client.post_raw(endpoint, payload)
        print(f"Response: {resp}")
    except Exception as e:
        print_err(str(e))
        sys.exit(1)


# --- MONITOR / TUI ---

@cli.command("monitor")
@click.option("--interval", "-i", default=2.0, type=float, help="Refresh interval in seconds [default: 2.0]")
@click.option("--once", "-1", is_flag=True, help="Render a single snapshot and exit immediately")
@click.pass_context
def cmd_monitor(ctx: click.Context, interval: float, once: bool):
    """Launch interactive real-time terminal dashboard (TUI)."""
    client = get_client(ctx)
    run_monitor(client, interval=interval, once=once)


@cli.command("tui")
@click.option("--interval", "-i", default=2.0, type=float, help="Refresh interval in seconds [default: 2.0]")
@click.option("--once", "-1", is_flag=True, help="Render a single snapshot and exit immediately")
@click.pass_context
def cmd_tui(ctx: click.Context, interval: float, once: bool):
    """Alias for monitor command."""
    client = get_client(ctx)
    run_monitor(client, interval=interval, once=once)


def main():
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        cli()
    except BrokenPipeError:
        sys.exit(0)


if __name__ == "__main__":
    main()
