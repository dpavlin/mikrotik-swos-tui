"""Interactive live terminal dashboard for MikroTik SwOS switches."""

from __future__ import annotations

import datetime
import sys
import time
from typing import Optional

from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from mikrotik_swos.client import SwOSClient, SwOSError


def generate_dashboard(client: SwOSClient) -> Group:
    """Fetch live data from client and construct rich dashboard layout."""
    sys_info = client.get_system()
    ports = client.get_ports()
    stats = client.get_stats()
    hosts = client.get_hosts(dynamic=True, static=False)

    # 1. Header Information Panel
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header_table = Table.grid(expand=True)
    header_table.add_column(justify="left", ratio=1)
    header_table.add_column(justify="center", ratio=1)
    header_table.add_column(justify="right", ratio=1)

    left_text = Text.from_markup(
        f"[bold cyan]Switch:[/] [white]{sys_info.identity}[/]  "
        f"[bold cyan]Model:[/] [yellow]{sys_info.board}[/]  "
        f"[bold cyan]SwOS:[/] [green]{sys_info.version}[/]"
    )
    center_text = Text.from_markup(
        f"[bold cyan]IP:[/] [bright_white]{sys_info.ip}[/]  "
        f"[bold cyan]MAC:[/] [bright_white]{sys_info.mac}[/]  "
        f"[bold cyan]Serial:[/] [bright_white]{sys_info.serial}[/]"
    )
    right_text = Text.from_markup(
        f"[bold cyan]Uptime:[/] [green]{sys_info.uptime_str}[/]  "
        f"[bold cyan]Time:[/] [dim]{now_str}[/]"
    )
    header_table.add_row(left_text, center_text, right_text)
    header_panel = Panel(header_table, title="[bold white on blue] MikroTik SwOS Switch Monitor [/]", border_style="blue")

    # 2. Ports Live Status Table
    ports_table = Table(title="Port Status & Traffic", expand=True, border_style="dim")
    ports_table.add_column("Port", style="bold cyan", no_wrap=True)
    ports_table.add_column("Link", justify="center", no_wrap=True)
    ports_table.add_column("Speed", justify="center", no_wrap=True)
    ports_table.add_column("PVID", justify="right", no_wrap=True)
    ports_table.add_column("PoE", justify="center", no_wrap=True)
    ports_table.add_column("Rx Rate", justify="right", style="bright_cyan", no_wrap=True)
    ports_table.add_column("Tx Rate", justify="right", style="bright_green", no_wrap=True)
    ports_table.add_column("Rx Total", justify="right", no_wrap=True)
    ports_table.add_column("Tx Total", justify="right", no_wrap=True)
    ports_table.add_column("Errors (Rx/Tx)", justify="center", no_wrap=True)

    for i, p in enumerate(ports):
        st = stats[i] if i < len(stats) else None
        link_markup = "[bold green]UP[/]" if p.link_up else "[dim red]DOWN[/]"
        speed_str = f"{p.speed} {p.duplex[0]}" if p.link_up else "-"
        
        poe_str = p.poe_mode
        if p.poe_power_w > 0:
            poe_str += f" ({p.poe_power_w:.1f}W)"

        rx_rate = st.rx_rate_str if st else "-"
        tx_rate = st.tx_rate_str if st else "-"
        rx_bytes = st.rx_bytes_str if st else "-"
        tx_bytes = st.tx_bytes_str if st else "-"

        rx_err = st.rx_errors if st else 0
        tx_err = st.tx_errors if st else 0
        if rx_err > 0 or tx_err > 0:
            err_str = f"[bold red]{rx_err}/{tx_err}[/]"
        else:
            err_str = "0/0"

        ports_table.add_row(
            f"{p.name} (#{p.index+1})",
            link_markup,
            speed_str,
            str(p.default_vlan_id),
            poe_str,
            rx_rate,
            tx_rate,
            rx_bytes,
            tx_bytes,
            err_str,
        )

    # 3. Dynamic MAC Address Table
    mac_table = Table(title=f"Learned MAC Host Table ({len(hosts)} entries)", expand=True, border_style="dim")
    mac_table.add_column("MAC Address", style="bold yellow", no_wrap=True)
    mac_table.add_column("Port", style="cyan")
    mac_table.add_column("VLAN ID", justify="right")
    mac_table.add_column("Type", justify="center")
    mac_table.add_column("Drop", justify="center")
    mac_table.add_column("Mirror", justify="center")

    # Display up to 10 recent hosts
    for h in hosts[:12]:
        mac_table.add_row(
            escape(h.mac),
            h.port_name,
            str(h.vlan_id),
            "Dynamic" if h.dynamic else "Static",
            "Yes" if h.drop else "No",
            "Yes" if h.mirror else "No",
        )
    if len(hosts) > 12:
        mac_table.add_row(f"... and {len(hosts)-12} more ...", "", "", "", "", "")

    footer = Text.from_markup("[dim]Press [bold white]Ctrl+C[/] to exit monitor.[/]")
    return Group(header_panel, ports_table, mac_table, footer)


def run_monitor(client: SwOSClient, interval: float = 2.0) -> None:
    """Run interactive terminal monitoring loop."""
    console = Console(emoji=False)
    console.print(f"[bold cyan]Connecting to MikroTik SwOS at {client.host}...[/]")
    try:
        initial_layout = generate_dashboard(client)
    except SwOSError as e:
        console.print(f"[bold red]Error connecting to switch:[/] {e}")
        sys.exit(1)

    with Live(initial_layout, console=console, refresh_per_second=2, screen=True) as live:
        try:
            while True:
                time.sleep(interval)
                try:
                    dashboard = generate_dashboard(client)
                    live.update(dashboard)
                except SwOSError as e:
                    err_text = Text.from_markup(f"[bold red]Connection error:[/] {e} (retrying...)")
                    live.update(err_text)
        except KeyboardInterrupt:
            pass
