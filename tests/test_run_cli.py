"""
Tests for run.py's CLI functions. core.CONFIG/RUN_DIR are patched to isolated
temp locations so these never touch the real tunnels.json or running tunnels.
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssh_tunnel import core
import run


def _output_of(func, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args)
    return buf.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.tunnel = {
            "id": "t1",
            "name": "Test Tunnel",
            "ssh_host": "host.example.com",
            "ssh_user": "user",
            "local_port": 3307,
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
        }
        self.config = {"app_name": "Test", "tunnels": [self.tunnel]}

        patcher_config = patch.object(core, "CONFIG", self.config)
        patcher_run_dir = patch.object(core, "RUN_DIR", self.tmp_dir.name)
        patcher_config.start()
        patcher_run_dir.start()
        self.addCleanup(patcher_config.stop)
        self.addCleanup(patcher_run_dir.stop)


class DescribeTests(CliTestCase):
    def test_describe_inactive_tunnel(self):
        text = run.describe(self.tunnel)
        self.assertIn("Test Tunnel: INACTIVE", text)
        self.assertIn("localhost:3307", text)
        self.assertIn("host.example.com", text)
        self.assertIn("127.0.0.1:3306", text)


class FindTunnelOrExitTests(CliTestCase):
    def test_found_returns_tunnel(self):
        tunnel = run.find_tunnel_or_exit("Test Tunnel")
        self.assertEqual(tunnel["id"], "t1")

    def test_found_by_id(self):
        tunnel = run.find_tunnel_or_exit("t1")
        self.assertEqual(tunnel["name"], "Test Tunnel")

    def test_not_found_exits_with_code_1(self):
        with self.assertRaises(SystemExit) as ctx:
            run.find_tunnel_or_exit("nope")
        self.assertEqual(ctx.exception.code, 1)


class RunListTests(CliTestCase):
    def test_lists_all_tunnels(self):
        output = _output_of(run.run_list)
        self.assertIn("Test Tunnel", output)

    def test_empty_list_shows_hint(self):
        with patch.object(core, "CONFIG", {"app_name": "Test", "tunnels": []}):
            output = _output_of(run.run_list)
        self.assertIn("No tunnels defined", output)


class RunStatusTests(CliTestCase):
    def test_specific_tunnel(self):
        output = _output_of(run.run_status, "Test Tunnel")
        self.assertIn("INACTIVE", output)

    def test_none_lists_all(self):
        output = _output_of(run.run_status, None)
        self.assertIn("Test Tunnel", output)

    def test_unknown_name_exits_with_code_1(self):
        with self.assertRaises(SystemExit) as ctx:
            _output_of(run.run_status, "nope")
        self.assertEqual(ctx.exception.code, 1)


class RunCliTests(CliTestCase):
    def test_missing_name_exits_with_code_1(self):
        with self.assertRaises(SystemExit) as ctx:
            run.run_cli("start", None)
        self.assertEqual(ctx.exception.code, 1)

    def test_unknown_tunnel_exits_with_code_1(self):
        with self.assertRaises(SystemExit) as ctx:
            run.run_cli("start", "nope")
        self.assertEqual(ctx.exception.code, 1)

    def test_start_success_exits_with_code_0(self):
        with patch.object(core, "start_tunnel", return_value=(True, "Connected (PID 123).")):
            with self.assertRaises(SystemExit) as ctx:
                _output_of(run.run_cli, "start", "Test Tunnel")
        self.assertEqual(ctx.exception.code, 0)

    def test_start_failure_exits_with_code_1(self):
        with patch.object(core, "start_tunnel", return_value=(False, "boom")):
            with self.assertRaises(SystemExit) as ctx:
                _output_of(run.run_cli, "start", "Test Tunnel")
        self.assertEqual(ctx.exception.code, 1)

    def test_stop_does_not_exit_and_prints_message(self):
        with patch.object(core, "stop_tunnel", return_value=(True, "Disconnected (PID 123).")):
            output = _output_of(run.run_cli, "stop", "Test Tunnel")
        self.assertIn("Disconnected", output)

    def test_toggle_starts_when_not_running(self):
        with patch.object(core, "start_tunnel", return_value=(True, "Connected.")) as mock_start:
            with self.assertRaises(SystemExit) as ctx:
                run.run_cli("toggle", "Test Tunnel")
        mock_start.assert_called_once()
        self.assertEqual(ctx.exception.code, 0)

    def test_toggle_stops_when_running(self):
        with patch.object(core, "is_tunnel_running", return_value=True), patch.object(
            core, "stop_tunnel", return_value=(True, "Disconnected.")
        ) as mock_stop:
            with self.assertRaises(SystemExit) as ctx:
                run.run_cli("toggle", "Test Tunnel")
        mock_stop.assert_called_once()
        self.assertEqual(ctx.exception.code, 0)


class MainTrayFlagTests(CliTestCase):
    def _main_with_argv(self, argv):
        with patch.object(sys, "argv", ["run.py"] + argv):
            return run.main()

    def test_no_arguments_starts_visible_gui(self):
        with patch.object(run, "run_gui") as mock_gui:
            self._main_with_argv([])
        mock_gui.assert_called_once_with(False)

    def test_tray_flag_starts_hidden_gui(self):
        with patch.object(run, "run_gui") as mock_gui:
            self._main_with_argv(["--tray"])
        mock_gui.assert_called_once_with(True)

    def test_tray_flag_with_cli_command_exits_with_code_1(self):
        with patch.object(run, "run_gui") as mock_gui:
            with self.assertRaises(SystemExit) as ctx:
                _output_of(self._main_with_argv, ["--tray", "list"])
        mock_gui.assert_not_called()
        self.assertEqual(ctx.exception.code, 1)


class RunGuiTests(CliTestCase):
    def test_tray_start_does_not_raise_existing_window(self):
        with patch.object(core, "running_app_pid", return_value=4242), patch.object(os, "kill") as mock_kill:
            output = _output_of(run.run_gui, True)
        mock_kill.assert_not_called()
        self.assertIn("nothing to do", output)

    def test_normal_start_raises_existing_window(self):
        with patch.object(core, "running_app_pid", return_value=4242), patch.object(os, "kill") as mock_kill:
            output = _output_of(run.run_gui, False)
        mock_kill.assert_called_once()
        self.assertIn("bringing that window to front", output)


if __name__ == "__main__":
    unittest.main()
