"""Tests for CLI commands using click CliRunner."""

import unittest
from click.testing import CliRunner

from mikrotik_swos.cli import cli


class TestSwOSCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_cli_help(self):
        res = self.runner.invoke(cli, ["--help"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("MikroTik SwOS Command-Line Administration Tool", res.output)
        self.assertIn("system", res.output)
        self.assertIn("ports", res.output)
        self.assertIn("stats", res.output)
        self.assertIn("hosts", res.output)
        self.assertIn("vlan", res.output)

    def test_cli_system(self):
        res = self.runner.invoke(cli, ["system"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("MikroTik", res.output)
        self.assertIn("CSS106-5G-1S", res.output)
        self.assertIn("cc:2d:e0:f6:37:84", res.output)

    def test_cli_system_json(self):
        res = self.runner.invoke(cli, ["--json", "system"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn('"identity": "MikroTik"', res.output)
        self.assertIn('"mac": "cc:2d:e0:f6:37:84"', res.output)

    def test_cli_ports(self):
        res = self.runner.invoke(cli, ["ports"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("SFP", res.output)
        json_res = self.runner.invoke(cli, ["--json", "ports"])
        self.assertEqual(json_res.exit_code, 0)
        self.assertIn('"name": "Port1"', json_res.output)

    def test_cli_stats(self):
        res = self.runner.invoke(cli, ["stats"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("SFP", res.output)
        json_res = self.runner.invoke(cli, ["--json", "stats"])
        self.assertEqual(json_res.exit_code, 0)
        self.assertIn('"name": "Port1"', json_res.output)

    def test_cli_hosts(self):
        res = self.runner.invoke(cli, ["hosts"])
        self.assertEqual(res.exit_code, 0)
        self.assertIn("MAC Address Host Table", res.output)


if __name__ == "__main__":
    unittest.main()
