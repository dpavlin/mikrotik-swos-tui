#!/usr/bin/env python3
"""Automated SwOS DHCP-to-Static Migration Tool for FFZG Campus Fleet.

Discovers unassigned MikroTik CSS106-5G-1S switches running in dynamic DHCP pool
(192.168.88.230-249), allocates deterministic collision-free static IPs, reconfigures
the switch hardware via SwOS API, updates Git-tracked inventory in ~/m-swos,
takes full JSON backups, and emails an engineering report.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import click

# Add repository root to path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mikrotik_swos.cli import console, format_table
from mikrotik_swos.client import SwOSClient, SwOSConnectionError, SwOSError
from mikrotik_swos.models import SystemInfo


@dataclass
class SwitchCandidate:
    old_ip: str
    mac: str
    model: str
    serial: str
    firmware: str
    uptime: str
    current_mode: str
    new_ip: Optional[str] = None
    backup_path: Optional[str] = None
    verified: bool = False
    error: Optional[str] = None


def ping_host(ip: str, count: int = 1, timeout_sec: int = 1) -> bool:
    """Send ICMP ping to check host reachability."""
    cmd = ["ping", "-c", str(count), "-W", str(timeout_sec), ip]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0


def arp_lookup(ip: str) -> Optional[str]:
    """Check Linux kernel ARP/neighbor table for IP."""
    cmd = ["ip", "neigh", "show", ip]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and res.stdout.strip():
        # e.g. 192.168.88.231 dev eth0 lladdr d0:ea:11:04:48:85 REACHABLE
        parts = res.stdout.strip().split()
        if "lladdr" in parts:
            idx = parts.index("lladdr")
            if idx + 1 < len(parts):
                return parts[idx + 1].lower()
    return None


def parse_inventory(inventory_path: Path) -> Tuple[Dict[str, str], Set[str], int]:
    """Parse m-swos-ip-mac returning known_macs, assigned_ips, and highest_static_ip."""
    known_macs: Dict[str, str] = {}
    assigned_ips: Set[str] = set()
    highest_ip_num = 0

    if not inventory_path.exists():
        return known_macs, assigned_ips, highest_ip_num

    with open(inventory_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                ip, mac = parts[0], parts[1].lower()
                assigned_ips.add(ip)
                known_macs[mac] = ip
                # Track highest static IP in 192.168.88.0/24 (excluding dynamic range >= 230)
                if ip.startswith("192.168.88."):
                    try:
                        octet = int(ip.split(".")[3])
                        if octet < 230 and octet > highest_ip_num:
                            highest_ip_num = octet
                    except ValueError:
                        pass

    return known_macs, assigned_ips, highest_ip_num


def allocate_static_ip(
    assigned_ips: Set[str],
    start_octet: int,
    subnet_prefix: str = "192.168.88.",
    # TODO: hard-coding dhcp_start (230) and max_octet (229); should be verified against DHCP server config (e.g. deenes:/etc/dhcp/dhcpd.conf)
    dhcp_start: int = 230,
    max_octet: int = 229,
) -> str:
    """Find next available collision-free static IP."""
    candidate = max(start_octet, 11)
    while candidate <= max_octet:
        ip = f"{subnet_prefix}{candidate}"
        if ip not in assigned_ips:
            # Active ping check
            if not ping_host(ip, count=1, timeout_sec=1):
                # ARP check
                neigh_mac = arp_lookup(ip)
                if not neigh_mac:
                    assigned_ips.add(ip)
                    return ip
        candidate += 1
    raise RuntimeError(f"Exhausted available static IP addresses in {subnet_prefix}0/24 (limit {max_octet})")


def scan_dhcp_pool(
    dhcp_range: Tuple[int, int],
    known_macs: Dict[str, str],
    timeout: float = 3.0,
    debug: bool = False,
) -> List[SwitchCandidate]:
    """Scan dynamic DHCP range and interrogate SwOS switches."""
    candidates: List[SwitchCandidate] = []
    start_oct, end_oct = dhcp_range

    console.print(f"[bold cyan][*] Scanning DHCP pool:[/] 192.168.88.{start_oct} - 192.168.88.{end_oct}...")
    for octet in range(start_oct, end_oct + 1):
        ip = f"192.168.88.{octet}"
        if not ping_host(ip, count=1, timeout_sec=1):
            continue

        try:
            client = SwOSClient(host=ip, timeout=timeout, debug=debug)
            sys_info = client.get_system()
        except Exception as e:
            if debug:
                console.print(f"  [yellow][!] Failed HTTP query on {ip}: {e}[/]")
            continue

        mac_norm = sys_info.mac.lower()
        if mac_norm in known_macs:
            if debug:
                console.print(f"  [dim]Skipping {ip} ({mac_norm}): already tracked at static {known_macs[mac_norm]}[/]")
            continue

        # In SwOS, iptp == 1 is Static mode. If already static, skip
        if sys_info.ip_mode == "Static":
            if debug:
                console.print(f"  [dim]Skipping {ip} ({mac_norm}): switch already configured in static mode[/]")
            continue

        candidate = SwitchCandidate(
            old_ip=ip,
            mac=mac_norm,
            model=sys_info.board,
            serial=sys_info.serial,
            firmware=sys_info.version,
            uptime=sys_info.uptime_str,
            current_mode=sys_info.ip_mode,
        )
        candidates.append(candidate)
        console.print(f"  [bold green][+] Found unmigrated switch:[/] {ip} ({mac_norm}) - {sys_info.board} FW:{sys_info.version} SN:{sys_info.serial}")

    return candidates


def apply_migration(
    cand: SwitchCandidate,
    backup_dir: Path,
    timeout: float = 3.0,
    debug: bool = False,
) -> bool:
    """Migrate single switch from dynamic DHCP IP to static IP."""
    old_ip = cand.old_ip
    new_ip = cand.new_ip
    assert new_ip is not None

    console.print(f"[bold cyan][>] Migrating switch {cand.mac} ({cand.model}):[/] {old_ip} -> {new_ip}...")

    # 1. Connect to old IP and apply static IP settings
    client = SwOSClient(host=old_ip, timeout=timeout, debug=debug)
    try:
        # ip_mode: 1 = Static in SwOS sys.b
        client.set_system(static_ip=new_ip, ip_mode=1)
    except (SwOSConnectionError, Exception) as e:
        # Expected: SwOS drops connection immediately upon IP rebind
        if debug:
            console.print(f"  [dim]Connection terminated during rebind (normal): {e}[/]")

    # 2. Wait for network stack to rebind
    time.sleep(3.0)

    # 3. Verify reachability at new IP
    verified = False
    for attempt in range(1, 5):
        if ping_host(new_ip, count=1, timeout_sec=1):
            try:
                verify_client = SwOSClient(host=new_ip, timeout=timeout, debug=debug)
                info = verify_client.get_system()
                if info.mac.lower() == cand.mac and info.ip == new_ip and info.ip_mode == "Static":
                    verified = True
                    break
            except Exception as e:
                if debug:
                    console.print(f"  [dim]Verification attempt {attempt} failed: {e}[/]")
        time.sleep(1.0)

    if not verified:
        cand.error = f"Failed to verify reachability on {new_ip} after rebind"
        console.print(f"  [bold red][-] VERIFICATION FAILED:[/] {cand.error}")
        return False

    cand.verified = True
    console.print(f"  [bold green][✓] Verified switch on {new_ip}![/] MAC: {cand.mac}, Uptime: {cand.uptime}")

    # 4. Initial Backup on new IP
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        clean_mac = cand.mac.replace(":", "")
        backup_file = backup_dir / f"{new_ip}-{clean_mac}-{timestamp}.json"
        verify_client = SwOSClient(host=new_ip, timeout=timeout, debug=debug)
        backup_data = verify_client.backup()
        with open(backup_file, "w", encoding="utf-8") as bf:
            json.dump(backup_data, bf, indent=2)
        cand.backup_path = str(backup_file)
        console.print(f"  [bold green][✓] Configuration backup saved:[/] {backup_file} ({backup_file.stat().st_size} B)")
    except Exception as e:
        console.print(f"  [yellow][!] Backup failed on {new_ip} (non-fatal): {e}[/]")

    return True


def update_inventory_file(inventory_path: Path, new_entries: List[Tuple[str, str]]) -> None:
    """Append new static mappings and sort inventory numerically by IP."""
    lines: List[str] = []
    if inventory_path.exists():
        with open(inventory_path, "r", encoding="utf-8") as f:
            lines = [l.rstrip("\r\n") for l in f]

    for ip, mac in new_entries:
        lines.append(f"{ip} {mac}")

    # Separate comments and parse IPs
    comment_lines = []
    ip_records = []
    for line in lines:
        if not line.strip() or line.strip().startswith("#"):
            comment_lines.append(line)
        else:
            parts = line.split(maxsplit=2)
            ip = parts[0]
            mac = parts[1]
            rest = parts[2] if len(parts) > 2 else ""
            try:
                octet = int(ip.split(".")[3])
            except (IndexError, ValueError):
                octet = 999
            ip_records.append((octet, ip, mac, rest))

    # Sort numerically by 4th octet
    ip_records.sort(key=lambda x: x[0])

    # Reconstruct file
    with open(inventory_path, "w", encoding="utf-8") as f:
        for octet, ip, mac, rest in ip_records:
            if rest:
                f.write(f"{ip} {mac} {rest}\n")
            else:
                f.write(f"{ip} {mac}\n")


def git_commit_inventory(repo_dir: Path, commit_msg: str) -> Optional[str]:
    """Stage inventory change and create atomic Git commit."""
    try:
        subprocess.run(["git", "add", "m-swos-ip-mac"], cwd=repo_dir, check=True)
        res = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        # Extract short commit hash
        match = re.search(r"\[[a-zA-Z0-9_\-]+ ([a-f0-9]+)\]", res.stdout)
        commit_hash = match.group(1) if match else "HEAD"
        return commit_hash
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]Git commit failed:[/] {e.stderr}")
        return None


def send_email_report(
    recipient: str,
    subject: str,
    body: str,
) -> bool:
    """Send plain text report using system /usr/bin/mail."""
    mail_bin = subprocess.run(["which", "mail"], capture_output=True, text=True)
    if mail_bin.returncode != 0:
        console.print("[yellow][!] /usr/bin/mail not found; skipping email delivery[/]")
        return False

    cmd = ["mail", "-s", subject, recipient]
    res = subprocess.run(cmd, input=body, text=True, capture_output=True)
    if res.returncode == 0:
        console.print(f"[bold green][✓] Notification email successfully sent to:[/] {recipient}")
        return True
    else:
        console.print(f"[bold red][-] Failed to send email:[/] {res.stderr}")
        return False


# --- CLI ENTRYPOINT ---

@click.command()
@click.option("--inventory", default="/home/dpavlin/m-swos/m-swos-ip-mac", help="Path to m-swos-ip-mac inventory file")
@click.option("--backup-dir", default="/home/dpavlin/swos-backups", help="Directory to store JSON configuration backups")
@click.option("--dhcp-range", default="230-249", help="Dynamic DHCP pool range octets [default: 230-249]")
@click.option("--static-base", default=None, type=int, help="Starting static IP octet [default: auto-detect after highest assigned]")
@click.option("--apply", "apply_changes", is_flag=True, help="Apply migrations to switch hardware (defaults to dry-run)")
@click.option("--dry-run", is_flag=True, help="Preview migrations without making changes")
@click.option("--email", default="dpavlin+black@ffzg.hr", help="Recipient email address for migration reports")
@click.option("--no-email", is_flag=True, help="Disable email reporting")
@click.option("--timeout", default=3.0, type=float, help="SwOS HTTP timeout in seconds [default: 3.0]")
@click.option("--debug", is_flag=True, help="Enable verbose debug logging")
def cli(
    inventory: str,
    backup_dir: str,
    dhcp_range: str,
    static_base: Optional[int],
    apply_changes: bool,
    dry_run: bool,
    email: str,
    no_email: bool,
    timeout: float,
    debug: bool,
):
    """Automated SwOS DHCP-to-Static Migration Tool."""
    inv_path = Path(inventory)
    b_dir = Path(backup_dir)

    # Parse range
    try:
        r_start, r_end = map(int, dhcp_range.split("-"))
    except ValueError:
        console.print(f"[bold red]Invalid dhcp-range format (expected start-end):[/] {dhcp_range}")
        sys.exit(1)

    # 1. Parse inventory
    known_macs, assigned_ips, highest_ip = parse_inventory(inv_path)
    console.print(f"[bold cyan][*] Loaded inventory:[/] {inv_path} ({len(known_macs)} switches, highest static: 192.168.88.{highest_ip})")

    # Determine allocation base
    if static_base is not None:
        alloc_base = static_base
    else:
        alloc_base = max(highest_ip + 1, 116)
    console.print(f"[bold cyan][*] Next static allocation base:[/] 192.168.88.{alloc_base}")

    # 2. Scan DHCP pool
    candidates = scan_dhcp_pool((r_start, r_end), known_macs, timeout=timeout, debug=debug)
    if not candidates:
        console.print("[bold green]✓ No unassigned switches found on DHCP. Everything is up-to-date.[/]")
        sys.exit(0)

    # 3. Plan static IP allocations
    for cand in candidates:
        new_ip = allocate_static_ip(assigned_ips, alloc_base)
        cand.new_ip = new_ip
        alloc_base = int(new_ip.split(".")[3]) + 1

    # Render plan table
    headers = ["Old DHCP IP", "Target Static IP", "MAC Address", "Model", "Firmware", "Serial", "Uptime"]
    rows = [
        [
            cand.old_ip,
            cand.new_ip or "N/A",
            cand.mac,
            cand.model,
            cand.firmware,
            cand.serial,
            cand.uptime,
        ]
        for cand in candidates
    ]
    print(f"\nDiscovered Switches on DHCP ({len(candidates)} total):")
    print(format_table(headers, rows))

    # 4. Check if dry-run
    if dry_run or not apply_changes:
        console.print(f"\n[bold yellow]DRY-RUN MODE:[/] {len(candidates)} switch(es) planned for migration. Run with [bold green]--apply[/] to execute.")
        sys.exit(0)

    # 5. Apply migrations
    console.print(f"\n[bold green]=== Starting Production Migration for {len(candidates)} switch(es) ===[/]")
    success_count = 0
    new_entries: List[Tuple[str, str]] = []

    for idx, cand in enumerate(candidates, start=1):
        console.print(f"\n[bold white][{idx}/{len(candidates)}][/]")
        ok = apply_migration(cand, backup_dir=b_dir, timeout=timeout, debug=debug)
        if ok and cand.new_ip:
            success_count += 1
            new_entries.append((cand.new_ip, cand.mac))

    # 6. Update inventory and commit to Git
    commit_hash = None
    if new_entries:
        console.print(f"\n[bold cyan][*] Updating inventory file:[/] {inv_path} (+{len(new_entries)} entries)...")
        update_inventory_file(inv_path, new_entries)

        repo_dir = inv_path.parent
        min_ip = new_entries[0][0]
        max_ip = new_entries[-1][0]
        commit_msg = f"auto: migrate {len(new_entries)} switches to static IPs ({min_ip}..{max_ip})"
        commit_hash = git_commit_inventory(repo_dir, commit_msg)
        if commit_hash:
            console.print(f"[bold green][✓] Committed to Git:[/] {commit_hash} in {repo_dir}")

        # Update /dev/shm/name-mac.mikrotik if script exists
        shm_cmd = f"cat {inv_path} | sed 's/192.168.88./msw_/' | sort -k 2 | cut -d' ' -f-2 > /dev/shm/name-mac.mikrotik"
        subprocess.run(shm_cmd, shell=True)
        console.print("[bold green][✓] Updated /dev/shm/name-mac.mikrotik[/]")

    # 7. Format report and email
    report_lines = [
        f"SwOS Fleet Migration Report - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 78,
        f"Host: black.ffzg.hr",
        f"Switches Migrated: {success_count} / {len(candidates)}",
        f"Inventory File: {inv_path}",
    ]
    if commit_hash:
        report_lines.append(f"Git Commit: {commit_hash}")

    report_lines.append("\nMigration Details:")
    report_lines.append(f"{'Old DHCP IP':<16} {'New Static IP':<16} {'MAC Address':<19} {'Model':<14} {'Firmware':<8} {'Status':<10}")
    report_lines.append("-" * 85)
    for cand in candidates:
        status = "OK" if cand.verified else "FAILED"
        report_lines.append(f"{cand.old_ip:<16} {cand.new_ip or 'N/A':<16} {cand.mac:<19} {cand.model:<14} {cand.firmware:<8} {status:<10}")

    report_body = "\n".join(report_lines)
    console.print("\n" + report_body)

    if not no_email and email and email.lower() != "none" and success_count > 0:
        subject = f"[SWOS] Migrated {success_count} switches from DHCP to Static IP"
        send_email_report(email, subject, report_body)


if __name__ == "__main__":
    cli()
