"""Tests for SwOSCodec and value converters."""

import glob
import os
import unittest

from mikrotik_swos.codec import (
    SwOSCodec,
    bitmask_to_ports,
    decode_hex_str,
    decode_ip,
    decode_mac,
    decode_signed32,
    encode_hex_str,
    encode_ip,
    encode_mac,
    format_bytes,
    format_uptime,
    ports_to_bitmask,
)


class TestSwOSCodec(unittest.TestCase):
    def test_parse_simple_object(self):
        text = "{en:0x01,com:'7075626c6963',ci:'',loc:''}"
        obj = SwOSCodec.parse(text)
        self.assertEqual(obj["en"], 1)
        self.assertEqual(obj["com"], "7075626c6963")
        self.assertEqual(obj["ci"], "")
        self.assertEqual(obj["loc"], "")

    def test_parse_and_dump_roundtrip(self):
        text = "{en:0x01,com:'7075626c6963',ci:'',loc:''}"
        obj = SwOSCodec.parse(text)
        dumped = SwOSCodec.dumps(obj)
        reparsed = SwOSCodec.parse(dumped)
        self.assertEqual(obj, reparsed)

    def test_parse_all_remote_fixtures(self):
        fixture_dir = os.path.join(os.path.dirname(__file__), "..", "remote")
        json_files = glob.glob(os.path.join(fixture_dir, "*.b.json"))
        self.assertGreater(len(json_files), 0, "No fixtures found in remote/")
        for f in json_files:
            with open(f, "r") as fp:
                raw = fp.read().strip()
            data = SwOSCodec.parse(raw)
            self.assertIsNotNone(data, f"Failed to parse {f}")
            dumped = SwOSCodec.dumps(data)
            reparsed = SwOSCodec.parse(dumped)
            self.assertEqual(data, reparsed, f"Roundtrip failed for {f}")

    def test_decode_hex_str(self):
        self.assertEqual(decode_hex_str("4d696b726f54696b"), "MikroTik")
        self.assertEqual(decode_hex_str("384133443038323432453341"), "8A3D08242E3A")
        self.assertEqual(decode_hex_str(""), "")
        self.assertEqual(encode_hex_str("MikroTik"), "4d696b726f54696b")

    def test_ip_codecs(self):
        self.assertEqual(decode_ip(0x0158A8C0), "192.168.88.1")
        self.assertEqual(encode_ip("192.168.88.1"), 0x0158A8C0)
        self.assertEqual(decode_ip(0), "0.0.0.0")
        self.assertEqual(encode_ip("0.0.0.0"), 0)

    def test_mac_codecs(self):
        self.assertEqual(decode_mac("cc2de0f63784"), "cc:2d:e0:f6:37:84")
        self.assertEqual(encode_mac("CC:2D:E0:F6:37:84"), "cc2de0f63784")
        self.assertEqual(encode_mac("cc-2d-e0-f6-37-84"), "cc2de0f63784")

    def test_signed32(self):
        self.assertEqual(decode_signed32(0xFFFFFF80), -128)
        self.assertEqual(decode_signed32(100), 100)

    def test_bitmask_conversions(self):
        self.assertEqual(bitmask_to_ports(0x07, 6), [0, 1, 2])
        self.assertEqual(bitmask_to_ports(0x3F, 6), [0, 1, 2, 3, 4, 5])
        self.assertEqual(ports_to_bitmask([0, 1, 2]), 0x07)
        self.assertEqual(ports_to_bitmask([0, 1, 2, 3, 4, 5]), 0x3F)

    def test_format_uptime(self):
        # 100 centisecs = 1 sec
        self.assertEqual(format_uptime(366100), "01h 01m 01s")
        self.assertEqual(format_uptime(8640000), "1d 00h 00m 00s")

    def test_format_bytes(self):
        self.assertEqual(format_bytes(500), "500 B")
        self.assertEqual(format_bytes(1024), "1.0 KiB")
        self.assertEqual(format_bytes(1048576), "1.0 MiB")


if __name__ == "__main__":
    unittest.main()
