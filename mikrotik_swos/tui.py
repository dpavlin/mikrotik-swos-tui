"""Fixed-width Unix-style terminal monitor for MikroTik SwOS switches."""

from __future__ import annotations

import datetime
import sys
import time
from typing import Any, Optional

from mikrotik_swos.client import SwOSClient, SwOSError


def generate_dashboard(client: SwOSClient) -> str:
    """Fetch live data from client and format a fixed-width, jitter-free dashboard string."""
    sys_info = client.get_system()
    ports = client.get_ports()
    stats = client.get_stats()
    hosts = client.get_hosts(dynamic=True, static=False)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    lines.append(f"=== MikroTik SwOS Switch Monitor: {sys_info.identity} ({sys_info.board}) {sys_info.ip} [{sys_info.mac}] ===")
    lines.append(f"SwOS: {sys_info.version} | Serial: {sys_info.serial} | Uptime: {sys_info.uptime_str} | {now_str}")
    lines.append("")

    # 1. Ports Section
    lines.append("Port Status & Traffic:")
    p_hdr = f"{'PORT':<12} {'LINK':<6} {'SPEED':<8} {'PVID':>5} {'POE':<10} {'RX RATE':>11} {'TX RATE':>11} {'RX TOTAL':>11} {'TX TOTAL':>11} {'ERR (R/T)':>10}"
    lines.append(p_hdr)
    lines.append("-" * len(p_hdr))

    for i, p in enumerate(ports):
        st = stats[i] if i < len(stats) else None
        link_str = "UP" if p.link_up else "DOWN"
        speed_str = f"{p.speed} {p.duplex[0]}" if p.link_up else "-"
        poe_str = p.poe_mode
        if p.poe_power_w > 0:
            poe_str += f" ({p.poe_power_w:.1f}W)"
        rx_rate = st.rx_rate_str if st else "-"
        tx_rate = st.tx_rate_str if st else "-"
        rx_bytes = st.rx_bytes_str if st else "-"
        tx_bytes = st.tx_bytes_str if st else "-"
        err_str = f"{st.rx_errors}/{st.tx_errors}" if st else "0/0"

        port_label = f"{p.name} (#{p.index+1})"
        row = f"{port_label:<12} {link_str:<6} {speed_str:<8} {p.default_vlan_id:>5} {poe_str:<10} {rx_rate:>11} {tx_rate:>11} {rx_bytes:>11} {tx_bytes:>11} {err_str:>10}"
        lines.append(row)

    lines.append("-" * len(p_hdr))
    lines.append("")

    # 2. Hosts Section
    lines.append(f"Learned MAC Host Table ({len(hosts)} entries):")
    h_hdr = f"{'MAC ADDRESS':<19} {'PORT':<10} {'VLAN':>5}  {'TYPE':<8} {'DROP':<5} {'MIRROR':<6}"
    lines.append(h_hdr)
    lines.append("-" * len(h_hdr))

    for h in hosts[:12]:
        type_str = "Dynamic" if h.dynamic else "Static"
        drop_str = "Yes" if h.drop else "No"
        mirr_str = "Yes" if h.mirror else "No"
        h_row = f"{h.mac:<19} {h.port_name:<10} {h.vlan_id:>5}  {type_str:<8} {drop_str:<5} {mirr_str:<6}".rstrip()
        lines.append(h_row)

    if len(hosts) > 12:
        lines.append(f"... and {len(hosts)-12} more hosts ...")

    lines.append("-" * len(h_hdr))
    lines.append("Press Ctrl+C to exit monitor.")

    return "\n".join(lines)


def run_monitor(
    client: SwOSClient,
    interval: float = 2.0,
    once: bool = False,
    console: Optional[Any] = None,
) -> None:
    """Run interactive terminal monitoring loop or render a single snapshot."""
    # When --once is passed or when stdout is not an interactive TTY (piped to less, cat, file, test runner):
    # render a single clean untruncated snapshot and return immediately.
    if once or not sys.stdout.isatty():
        try:
            dashboard = generate_dashboard(client)
            print(dashboard)
            return
        except SwOSError as e:
            print(f"Error connecting to switch: {e}", file=sys.stderr)
            sys.exit(1)

    # Interactive TTY Mode
    # 1. Clear screen once at initial startup and hide cursor
    sys.stdout.write("\033[2J\033[H\033[?25l")
    sys.stdout.flush()

    try:
        while True:
            try:
                dashboard = generate_dashboard(client)
                # Overwrite in-place from (0,0) without clearing screen (zero flicker, zero column shifting)
                sys.stdout.write(f"\033[H{dashboard}\n\033[J")
                sys.stdout.flush()
            except SwOSError as e:
                now_str = datetime.datetime.now().strftime("%H:%M:%S")
                sys.stdout.write(f"\033[HConnection error at {now_str}: {e} (retrying...)\033[J\n")
                sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        # Restore cursor and move cursor down
        sys.stdout.write("\033[?25h\n")
        sys.stdout.flush()
