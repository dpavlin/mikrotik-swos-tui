"""Unit tests for swos_name_upstream topology discovery and uplink detection."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from swos_name_upstream import (
    TopologyDiscovery,
    inspect_swos_switch,
    normalize_mac,
    normalize_port_name,
)


class TestNameUpstreamFunctions(unittest.TestCase):
    def test_normalize_mac(self):
        self.assertEqual(normalize_mac("CC2DE0F63622"), "cc:2d:e0:f6:36:22")
        self.assertEqual(normalize_mac("cc:2d:e0:f6:36:22"), "cc:2d:e0:f6:36:22")
        self.assertEqual(normalize_mac("48-A9-8A-64-2C-C4"), "48:a9:8a:64:2c:c4")
        self.assertEqual(normalize_mac("48a9.8a64.2cc4"), "48:a9:8a:64:2c:c4")

    def test_normalize_port_name(self):
        self.assertEqual(normalize_port_name("g2"), "2")
        self.assertEqual(normalize_port_name("ether17"), "17")
        self.assertEqual(normalize_port_name("Gi1/0/42"), "42")
        self.assertEqual(normalize_port_name("Te1/0/28"), "28")
        self.assertEqual(normalize_port_name("24"), "24")
        self.assertEqual(normalize_port_name("bridge1/ether24"), "24")
        # Virtual interfaces should be preserved
        self.assertEqual(normalize_port_name("vlan20"), "vlan20")
        self.assertEqual(normalize_port_name("MGMT"), "MGMT")

    def test_topology_discovery_access_port(self):
        # Create a TopologyDiscovery with empty/mocked files
        topo = TopologyDiscovery(
            fdb_dir=Path("/nonexistent"),
            neighbors_file=Path("/nonexistent"),
            sw_ip_mac_file=Path("/nonexistent"),
            trunk_regex_file=Path("/nonexistent"),
            mac_threshold=5,
        )

        # Inject simulated topology:
        # sw-core has trunk port sfp2-2 with 100 MACs
        # sw-a100-j has trunk port 24 with 50 MACs, port 2 connecting to sw-a117-cervantes
        # sw-a200 has port 5 with 1 MAC (our target)
        topo.ip_to_name["10.20.0.20"] = "sw-a200"
        topo.inter_switch_links.add(("sw-a100-j", "2"))
        topo.inter_switch_links.add(("sw-core", "sfp2-2"))
        topo.trunk_ports.add(("sw-core", "sfp2-2"))

        # Setup FDB
        target_mac = "48:a9:8a:64:2d:73"
        topo.mac_locations[target_mac] = [
            ("sw-core", "sfp2-2"),
            ("sw-a200", "5"),
        ]
        topo.port_mac_count[("sw-core", "sfp2-2")] = 100
        topo.port_mac_count[("sw-a200", "5")] = 1

        match = topo.find_upstream_switch(target_mac)
        self.assertIsNotNone(match)
        sw, port, cnt, method = match
        self.assertEqual(sw, "sw-a200")
        self.assertEqual(port, "5")
        self.assertEqual(cnt, 1)

    def test_topology_discovery_inter_switch_exclusion(self):
        topo = TopologyDiscovery(
            fdb_dir=Path("/nonexistent"),
            neighbors_file=Path("/nonexistent"),
            sw_ip_mac_file=Path("/nonexistent"),
            trunk_regex_file=Path("/nonexistent"),
            mac_threshold=5,
        )
        # Both sw-a100-j:2 and sw-a117-cervantes:17 see the MAC
        # But sw-a100-j:2 is an inter-switch link to sw-a117-cervantes
        topo.inter_switch_links.add(("sw-a100-j", "2"))
        target_mac = "48:a9:8a:64:2c:c4"
        topo.mac_locations[target_mac] = [
            ("sw-a100-j", "2"),
            ("sw-a117-cervantes", "17"),
        ]
        topo.port_mac_count[("sw-a100-j", "2")] = 2
        topo.port_mac_count[("sw-a117-cervantes", "17")] = 1

        match = topo.find_upstream_switch(target_mac)
        self.assertIsNotNone(match)
        sw, port, cnt, method = match
        self.assertEqual(sw, "sw-a117-cervantes")
        self.assertEqual(port, "17")

    @patch("swos_name_upstream.SwOSClient")
    def test_inspect_swos_switch_rstp_root(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # Mock sys.b, rstp.b, !dhost.b, link.b
        mock_client.get.side_effect = lambda endpoint: {
            "sys.b": {
                "mac": "48a98a642d73",
                "id": "4d696b726f54696b",  # MikroTik
                "brd": "4353533130362d35472d3153",  # CSS106-5G-1S
                "ver": "322e3136",  # 2.16
            },
            "rstp.b": {
                "role": [2, 3, 3, 3, 3, 3],  # Port 0 is root
            },
            "!dhost.b": [
                {"prt": 0, "mac": "aa0000a51872"},
                {"prt": 0, "mac": "744d283d3982"},
            ],
            "link.b": {
                "nm": ["506f727431", "506f727432", "506f727433", "506f727434", "506f727435", "534650"],
            },
        }[endpoint]

        topo = MagicMock()
        topo.find_upstream_switch.return_value = ("sw-a200", "5", 1, "Access port")

        info = inspect_swos_switch("192.168.88.131", topo)
        self.assertEqual(info.ip, "192.168.88.131")
        self.assertEqual(info.mac, "48:a9:8a:64:2d:73")
        self.assertEqual(info.current_identity, "MikroTik")
        self.assertEqual(info.uplink_index, 0)
        self.assertEqual(info.uplink_name, "Port1")
        self.assertIn("RSTP Root Port", info.uplink_reason)
        self.assertEqual(info.upstream_switch, "sw-a200")
        self.assertEqual(info.upstream_port, "5")
        self.assertEqual(info.proposed_identity, "swos-131-sw-a200")
        self.assertEqual(info.status, "READY_TO_RENAME")

    @patch("swos_name_upstream.SwOSClient")
    def test_inspect_swos_switch_dhost_fallback(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        # If RSTP role is not set (e.g. all designated or disabled), fallback to max dhost
        mock_client.get.side_effect = lambda endpoint: {
            "sys.b": {
                "mac": "c4ad34471323",
                "id": "4d696b726f54696b",
                "brd": "4353533130362d35472d3153",
                "ver": "322e37",
            },
            "rstp.b": {
                "role": [3, 3, 3, 3, 3, 3],  # No root role
            },
            "!dhost.b": [
                {"prt": 4, "mac": "aa0000a51872"},
                {"prt": 4, "mac": "744d283d3982"},
                {"prt": 4, "mac": "00be43f7e8f7"},
            ],
            "link.b": {
                "nm": ["506f727431", "506f727432", "506f727433", "506f727434", "506f727435", "534650"],
            },
        }[endpoint]

        topo = MagicMock()
        topo.find_upstream_switch.return_value = ("sw-fond-1", "7", 1, "Access port")

        info = inspect_swos_switch("192.168.88.115", topo)
        self.assertEqual(info.uplink_index, 4)
        self.assertEqual(info.uplink_name, "Port5")
        self.assertIn("DHost Max Learned", info.uplink_reason)
        self.assertEqual(info.upstream_switch, "sw-fond-1")
        self.assertEqual(info.proposed_identity, "swos-115-sw-fond-1")


if __name__ == "__main__":
    unittest.main()
