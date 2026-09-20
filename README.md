# mikrotik-sw-tui

Command-line and Terminal User Interface (TUI) administration tool for MikroTik switches running SwitchOS (SwOS v2.x).

Developed and verified against **MikroTik CSS106-1G-4P-1S / RB260GSP** (SwOS 2.7, MAC `cc:2d:e0:f6:37:84`) at `192.168.88.1`.

---

## Features

- **Full SwOS API Support:** Communicates directly over HTTP Digest authentication (`admin:<password>`) with custom JavaScript object literal parser and serializer (`SwOSCodec`).
- **Data-Dense CLI Output:** Color-coded rich terminal tables and JSON output mode (`--json`) for scripting.
- **Real-Time TUI Dashboard:** Live full-screen terminal monitor displaying ports, Tx/Rx rates, PoE power/current, error counters, and learned MAC addresses.
- **Port Administration:** Configure link state, auto-negotiation, speed (10M/100M/1G), duplex, flow control, PoE out modes (`off`, `auto`, `on`, `calibr`), and PVID.
- **VLAN Management:** Inspect per-port VLAN modes and add/remove static VLAN table entries.
- **System Settings:** Configure identity name, static IP, acquisition mode (DHCP / static), watchdog, and discovery protocol (MNDP).
- **Traffic & Error Statistics:** Detailed per-port counters (bytes, packets, unicast, multicast, broadcast, FCS errors, fragments, runts, collisions).
- **Backup & Restore:** Full configuration state export and import to structured JSON files.
- **Raw API Access:** Low-level `raw get <endpoint>` and `raw post <endpoint> <payload>` for debugging and automation.

---

## Installation & Requirements

Python 3.9+ with standard packages:
```bash
pip install click requests rich textual
```

Run directly with `./swos.py`:
```bash
./swos.py --help
```

Or install in editable mode:
```bash
pip install -e .
```

---

## Configuration & Environment Variables

Credentials and target host can be passed via CLI arguments or environment variables:

| Parameter | CLI Flag | Environment Variable | Default |
|-----------|----------|----------------------|---------|
| Target Host | `-H`, `--host` | `SWOS_HOST` | `192.168.88.1` |
| Username | `-u`, `--user` | `SWOS_USER` | `admin` |
| Password | `-p`, `--password` | `SWOS_PASSWORD` | `""` (empty) |
| Timeout | `--timeout` | `SWOS_TIMEOUT` | `5.0` |
| Debug Log | `--debug` | - | `False` |
| JSON Output | `--json` | - | `False` |

---

## CLI Usage Examples

### 1. Switch Overview & System Information
```bash
./swos.py system
./swos.py system --json
```

### 2. Port Link Status & PoE Overview
```bash
./swos.py ports
```

### 3. Live TUI Dashboard
```bash
./swos.py monitor
# or alias:
./swos.py tui --interval 1.5
```

### 4. Traffic & Error Counters
```bash
./swos.py stats
./swos.py stats --errors
```

### 5. MAC Address Host Table
```bash
./swos.py hosts
./swos.py hosts --port 1
./swos.py hosts --vlan 1
```

### 6. VLAN Configuration
```bash
# View VLAN settings and static table
./swos.py vlan

# Add static VLAN 100 with member ports 1, 2, 3
./swos.py add-vlan 100 --ports 1,2,3

# Delete static VLAN 100
./swos.py del-vlan 100
```

### 7. Port Configuration
```bash
# Set Port 2 PoE to auto
./swos.py set-port 2 --poe auto

# Set Port 3 PVID to 10
./swos.py set-port 3 --pvid 10

# Disable Port 4
./swos.py set-port 4 --disable
```

### 8. System Configuration
```bash
# Rename switch
./swos.py set-system --identity "Core-CSS106"

# Toggle watchdog or MNDP discovery
./swos.py set-system --watchdog 1 --discovery 1
```

### 9. Backup & Restore
```bash
# Export full configuration backup
./swos.py backup backup-core.json

# Restore configuration
./swos.py restore backup-core.json --yes
```

### 10. Low-Level Raw API Access
```bash
./swos.py raw get sys.b
./swos.py raw get link.b
./swos.py raw get '!stats.b'
./swos.py raw post snmp.b "{en:0x01,com:'7075626c6963',ci:'',loc:''}"
```

---

## Architecture & Codebase

```
mikrotik-sw-tui/
├── mikrotik_swos/
│   ├── __init__.py      # Package metadata
│   ├── codec.py         # SwOS JS literal parser, serializer & data type decoders
│   ├── models.py        # Typed dataclasses (SystemInfo, PortInfo, PortStats, etc.)
│   ├── client.py        # SwOSClient with HTTP Digest auth and API endpoints
│   ├── cli.py           # Click CLI with Rich formatting & JSON modes
│   └── tui.py           # Live interactive terminal monitor
├── remote/              # Captured SwOS endpoint payloads and hardware baselines
├── tests/
│   ├── conftest.py      # Pytest workspace configuration
│   ├── test_codec.py    # Unit tests for parser, serializer and roundtrips
│   ├── test_models.py   # Unit tests for models using recorded fixtures
│   ├── test_live.py     # Live integration tests against switch
│   └── test_cli.py      # Click runner tests for CLI commands
├── swos.py              # Executable entry point script
└── pyproject.toml       # Package build configuration
```

---

## Testing

Run unit tests and live switch tests:
```bash
pytest -v
```
