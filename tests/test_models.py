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

    def test_detect_upstream_heuristics(self):
        from mikrotik_swos.client import SwOSClient

        client = SwOSClient("127.0.0.1", "admin", "admin")

        link_data = self.fixtures["link"]
        fwd_data = self.fixtures["fwd"]
        ports = [PortInfo.from_dicts(i, link_data, fwd_data) for i in range(6)]

        # 1. RSTP Root port heuristic (role 2 on Port2)
        rstp_data = {"role": [3, 2, 3, 3, 3, 3]}
        up = client.detect_upstream_port(ports=ports, rstp_data=rstp_data)
        self.assertEqual(up.index, 1)
        self.assertEqual(up.name, "Port2")
        self.assertEqual(up.method, "rstp")
        self.assertIn("RSTP Root Port", up.reason)

        # 2. Configured Port Name heuristic (up: or uplink)
        p_named = [PortInfo.from_dicts(i, link_data, fwd_data) for i in range(6)]
        p_named[2].name = "up:dell-sw"
        up_name = client.detect_upstream_port(ports=p_named, rstp_data={"role": [3, 3, 3, 3, 3, 3]})
        self.assertEqual(up_name.index, 2)
        self.assertEqual(up_name.name, "up:dell-sw")
        self.assertEqual(up_name.method, "name")

        # 3. Dynamic MAC Count heuristic
        dhost_data = self.fixtures["dhost"]
        port_names = ["Port1", "Port2", "Port3", "Port4", "Port5", "SFP"]
        hosts = [HostEntry.from_dict(h, port_names, dynamic=True) for h in dhost_data]
        up_dhost = client.detect_upstream_port(ports=ports, rstp_data={"role": [3, 3, 3, 3, 3, 3]}, hosts=hosts)
        self.assertEqual(up_dhost.index, 0)
        self.assertEqual(up_dhost.name, "Port1")
        self.assertEqual(up_dhost.method, "dhost")
        self.assertGreaterEqual(up_dhost.mac_count, 1)

        # 4. Single active link heuristic
        p_single = [PortInfo.from_dicts(i, link_data, fwd_data) for i in range(6)]
        for p in p_single:
            p.link_up = False
        p_single[4].link_up = True
        up_single = client.detect_upstream_port(ports=p_single, rstp_data={}, hosts=[])
        self.assertEqual(up_single.index, 4)
        self.assertEqual(up_single.name, "Port5")
        self.assertEqual(up_single.method, "single_active")

        # 5. Fallback lowest active link
        up_fallback = client.detect_upstream_port(ports=ports, rstp_data={"role": [3, 3, 3, 3, 3, 3]}, hosts=[])
        self.assertEqual(up_fallback.index, 0)
        self.assertEqual(up_fallback.name, "Port1")
        self.assertEqual(up_fallback.method, "fallback")

        # 6. No active links
        p_none = [PortInfo.from_dicts(i, link_data, fwd_data) for i in range(6)]
        for p in p_none:
            p.link_up = False
        up_none = client.detect_upstream_port(ports=p_none)
        self.assertEqual(up_none.index, -1)
        self.assertEqual(up_none.method, "none")


if __name__ == "__main__":
    unittest.main()
