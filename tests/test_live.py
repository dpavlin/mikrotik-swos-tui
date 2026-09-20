"""Live integration test against MikroTik switch at 192.168.88.1."""

import socket
import unittest

from mikrotik_swos.client import SwOSClient, SwOSConnectionError


def is_switch_reachable(host="192.168.88.1", port=80, timeout=1.0) -> bool:
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


class TestSwOSLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not is_switch_reachable():
            raise unittest.SkipTest("Switch at 192.168.88.1 is unreachable")
        cls.client = SwOSClient("192.168.88.1", "admin", "", timeout=3.0, debug=False)

    def test_live_system(self):
        info = self.client.get_system()
        self.assertEqual(info.identity, "MikroTik")
        self.assertEqual(info.board, "CSS106-5G-1S")
        self.assertEqual(info.version, "2.7")
        self.assertEqual(info.mac, "cc:2d:e0:f6:37:84")
        self.assertEqual(info.ip, "192.168.88.1")
        self.assertGreater(info.uptime_centisec, 0)

    def test_live_ports(self):
        ports = self.client.get_ports()
        self.assertEqual(len(ports), 6)
        p1 = ports[0]
        self.assertEqual(p1.name, "Port1")
        self.assertTrue(p1.link_up)

    def test_live_stats(self):
        stats = self.client.get_stats()
        self.assertEqual(len(stats), 6)
        self.assertGreater(stats[0].rx_bytes, 0)

    def test_live_hosts(self):
        hosts = self.client.get_hosts(dynamic=True, static=False)
        self.assertGreater(len(hosts), 0)

    def test_live_snmp(self):
        snmp = self.client.get_snmp()
        self.assertTrue(snmp.enabled)
        self.assertEqual(snmp.community, "public")


if __name__ == "__main__":
    unittest.main()
