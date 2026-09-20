"""Unit tests for models."""

import json
import os
import unittest

from mikrotik_swos.codec import SwOSCodec
from mikrotik_swos.models import (
    HostEntry,
    PortInfo,
    PortStats,
    SfpInfo,
    SnmpInfo,
    SystemInfo,
    VlanEntry,
)


class TestModels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "remote")
        cls.fixtures = {}
        for name in ["sys", "link", "fwd", "rstp", "stats", "dhost", "host", "sfp", "snmp", "vlan", "acl"]:
            path = os.path.join(fixtures_dir, f"{name}.b.json")
            if os.path.exists(path):
                with open(path) as fp:
                    cls.fixtures[name] = SwOSCodec.parse(fp.read())

    def test_system_info(self):
        sys_data = self.fixtures["sys"]
        info = SystemInfo.from_dict(sys_data)
        self.assertEqual(info.identity, "MikroTik")
        self.assertEqual(info.board, "CSS106-5G-1S")
        self.assertEqual(info.version, "2.7")
        self.assertEqual(info.serial, "8A3D08242E3A")
        self.assertEqual(info.mac, "cc:2d:e0:f6:37:84")
        self.assertEqual(info.ip, "192.168.88.1")
        self.assertEqual(info.ip_mode, "DHCP with fallback")
        self.assertTrue(info.watchdog)
        self.assertTrue(info.discovery)

    def test_ports(self):
        link_data = self.fixtures["link"]
        fwd_data = self.fixtures["fwd"]
        ports = [PortInfo.from_dicts(i, link_data, fwd_data) for i in range(6)]
        self.assertEqual(len(ports), 6)
        # Port 1
        p1 = ports[0]
        self.assertEqual(p1.name, "Port1")
        self.assertTrue(p1.enabled)
        self.assertTrue(p1.link_up)
        self.assertEqual(p1.speed, "1G")
        self.assertEqual(p1.duplex, "Full")
        self.assertEqual(p1.default_vlan_id, 1)

        # Port 4 (Down)
        p4 = ports[3]
        self.assertEqual(p4.name, "Port4")
        self.assertTrue(p4.enabled)
        self.assertFalse(p4.link_up)
        self.assertEqual(p4.speed, "Down")

    def test_port_stats(self):
        stats_data = self.fixtures["stats"]
        p1_stats = PortStats.from_stats(0, "Port1", stats_data)
        self.assertEqual(p1_stats.name, "Port1")
        self.assertGreater(p1_stats.rx_bytes, 0)
        self.assertGreater(p1_stats.tx_bytes, 0)
        self.assertIn("GiB", p1_stats.rx_bytes_str)

    def test_hosts(self):
        dhost_data = self.fixtures["dhost"]
        port_names = ["Port1", "Port2", "Port3", "Port4", "Port5", "SFP"]
        hosts = [HostEntry.from_dict(h, port_names, dynamic=True) for h in dhost_data]
        self.assertEqual(len(hosts), 16)
        self.assertEqual(hosts[0].mac, "18:fe:34:f1:cf:34")
        self.assertEqual(hosts[0].port_name, "Port1")
        self.assertTrue(hosts[0].dynamic)

    def test_snmp(self):
        snmp_data = self.fixtures["snmp"]
        snmp = SnmpInfo.from_dict(snmp_data)
        self.assertTrue(snmp.enabled)
        self.assertEqual(snmp.community, "public")


if __name__ == "__main__":
    unittest.main()
