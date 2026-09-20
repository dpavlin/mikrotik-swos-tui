"""MikroTik SwOS serialization, deserialization, and field codecs."""

from __future__ import annotations

import datetime
from datetime import timezone
import socket
import struct
from typing import Any, Dict, List, Optional, Sequence, Union


class SwOSCodec:
    """Parser and serializer for MikroTik SwOS JavaScript literal objects."""

    @classmethod
    def parse(cls, text: str) -> Any:
        """Parse SwOS JavaScript literal text into Python objects."""
        text = text.strip()
        if not text:
            return None

        idx = 0
        n = len(text)

        def skip_ws():
            nonlocal idx
            while idx < n and text[idx] in " \t\r\n":
                idx += 1

        def parse_value():
            skip_ws()
            if idx >= n:
                return None
            ch = text[idx]
            if ch == "{":
                return parse_object()
            elif ch == "[":
                return parse_array()
            elif ch in ("'", '"'):
                return parse_string()
            else:
                return parse_literal()

        def parse_object() -> Dict[str, Any]:
            nonlocal idx
            idx += 1  # skip '{'
            obj: Dict[str, Any] = {}
            skip_ws()
            if idx < n and text[idx] == "}":
                idx += 1
                return obj
            while idx < n:
                skip_ws()
                if idx < n and text[idx] == "}":
                    idx += 1
                    break
                # parse key
                key_start = idx
                while idx < n and (text[idx].isalnum() or text[idx] in "_-!"):
                    idx += 1
                key = text[key_start:idx]
                skip_ws()
                if idx < n and text[idx] == ":":
                    idx += 1
                else:
                    raise ValueError(f"Expected ':' at index {idx} for key '{key}' in: {text[max(0, idx-20):idx+20]}")
                val = parse_value()
                obj[key] = val
                skip_ws()
                if idx < n and text[idx] == ",":
                    idx += 1
                elif idx < n and text[idx] == "}":
                    idx += 1
                    break
            return obj

        def parse_array() -> List[Any]:
            nonlocal idx
            idx += 1  # skip '['
            arr: List[Any] = []
            skip_ws()
            if idx < n and text[idx] == "]":
                idx += 1
                return arr
            while idx < n:
                skip_ws()
                if idx < n and text[idx] == "]":
                    idx += 1
                    break
                val = parse_value()
                arr.append(val)
                skip_ws()
                if idx < n and text[idx] == ",":
                    idx += 1
                elif idx < n and text[idx] == "]":
                    idx += 1
                    break
            return arr

        def parse_string() -> str:
            nonlocal idx
            quote = text[idx]
            idx += 1
            res: List[str] = []
            while idx < n:
                ch = text[idx]
                if ch == "\\":
                    idx += 1
                    if idx < n:
                        res.append(text[idx])
                        idx += 1
                elif ch == quote:
                    idx += 1
                    return "".join(res)
                else:
                    res.append(ch)
                    idx += 1
            return "".join(res)

        def parse_literal() -> Any:
            nonlocal idx
            start = idx
            while idx < n and text[idx] not in ",}] \t\r\n":
                idx += 1
            lit = text[start:idx]
            if lit.startswith(("0x", "0X")):
                return int(lit, 16)
            try:
                if "." in lit:
                    return float(lit)
                return int(lit)
            except ValueError:
                if lit == "true":
                    return True
                elif lit == "false":
                    return False
                elif lit in ("null", "undefined"):
                    return None
                return lit

        return parse_value()

    @classmethod
    def dumps(cls, obj: Any, is_array: bool = False) -> str:
        """Serialize Python object into SwOS JavaScript literal format."""
        if isinstance(obj, dict):
            items = [f"{k}:{cls.dumps(v)}" for k, v in obj.items()]
            return "{" + ",".join(items) + "}"
        elif isinstance(obj, list):
            items = [cls.dumps(v, is_array=True) for v in obj]
            return "[" + ",".join(items) + "]"
        elif isinstance(obj, bool):
            return "0x01" if obj else "0x00"
        elif isinstance(obj, int):
            if obj < 0:
                obj = (1 << 32) + obj
            h = hex(obj)[2:]
            if len(h) % 2 != 0:
                h = "0" + h
            return "0x" + h
        elif isinstance(obj, float):
            return str(obj)
        elif isinstance(obj, str):
            escaped = obj.replace("'", "\\'")
            return f"'{escaped}'"
        elif obj is None:
            return "''"
        return str(obj)


def decode_hex_str(s: Optional[str]) -> str:
    """Decode hex-encoded ASCII string from SwOS (e.g. '4d696b726f54696b' -> 'MikroTik')."""
    if not s:
        return ""
    try:
        return bytes.fromhex(s).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return s


def encode_hex_str(s: str) -> str:
    """Encode string as hex ASCII for SwOS (e.g. 'MikroTik' -> '4d696b726f54696b')."""
    if not s:
        return ""
    return s.encode("utf-8").hex()


def decode_ip(val: Optional[int]) -> str:
    """Decode SwOS little-endian 32-bit uint IP to standard IPv4 dotted decimal."""
    if not val:
        return "0.0.0.0"
    try:
        return socket.inet_ntoa(struct.pack("<I", val & 0xFFFFFFFF))
    except Exception:
        return "0.0.0.0"


def encode_ip(ip_str: str) -> int:
    """Encode IPv4 dotted string to SwOS little-endian 32-bit uint."""
    if not ip_str or ip_str == "0.0.0.0":
        return 0
    try:
        return struct.unpack("<I", socket.inet_aton(ip_str))[0]
    except Exception:
        raise ValueError(f"Invalid IPv4 address: {ip_str}")


def decode_mac(s: Optional[str]) -> str:
    """Decode 12-char hex MAC string to colon-separated MAC address."""
    if not s or len(s) != 12:
        return s or ""
    return ":".join(s[i:i+2] for i in range(0, 12, 2)).lower()


def encode_mac(mac_str: str) -> str:
    """Normalize MAC address string to 12 lower-case hex characters."""
    cleaned = mac_str.replace(":", "").replace("-", "").replace(".", "").lower()
    if len(cleaned) != 12:
        raise ValueError(f"Invalid MAC address: {mac_str}")
    return cleaned


def decode_signed32(val: int) -> int:
    """Convert unsigned 32-bit integer to signed 32-bit integer."""
    if val >= 2147483648:
        return val - 4294967296
    return val


def decode_timestamp(val: int) -> datetime.datetime:
    """Convert Unix epoch timestamp to UTC datetime."""
    return datetime.datetime.fromtimestamp(val, tz=timezone.utc)


def format_uptime(centisecs: int) -> str:
    """Format SwOS centisecond uptime counter into days, hours, minutes, seconds."""
    total_seconds = centisecs / 100.0
    td = datetime.timedelta(seconds=int(total_seconds))
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days > 0:
        return f"{days}d {hours:02d}h {minutes:02d}m {seconds:02d}s"
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


def format_bytes(num_bytes: Union[int, float]) -> str:
    """Format byte count to human-readable string (B, KiB, MiB, GiB)."""
    val = float(num_bytes)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if abs(val) < 1024.0:
            return f"{val:3.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} PiB"


def format_rate(rate_bps: Union[int, float]) -> str:
    """Format bit rate to human-readable string (bps, Kbps, Mbps, Gbps)."""
    val = float(rate_bps)
    for unit in ["bps", "Kbps", "Mbps", "Gbps"]:
        if abs(val) < 1000.0:
            return f"{val:3.1f} {unit}" if unit != "bps" else f"{int(val)} bps"
        val /= 1000.0
    return f"{val:.1f} Tbps"


def bitmask_to_ports(mask: int, total_ports: int = 6) -> List[int]:
    """Return 0-indexed list of port indices enabled in bitmask."""
    return [i for i in range(total_ports) if (mask & (1 << i))]


def ports_to_bitmask(ports: Sequence[int]) -> int:
    """Convert list of 0-indexed port indices to bitmask integer."""
    mask = 0
    for p in ports:
        mask |= (1 << p)
    return mask
