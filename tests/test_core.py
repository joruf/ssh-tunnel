"""Tests for ssh_tunnel.core. All config/PID/log paths are patched to temp
locations so these tests never touch the real tunnels.json or ~/.cache/ssh-tunnel."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssh_tunnel import core


class LoadEnvTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(core.load_env("/nonexistent/path/.env"), {})

    def test_parses_key_value_pairs_and_ignores_comments(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# a comment\n\nSSH_HOST=example.com\nSSH_USER=\"bob\"\n")
            path = f.name
        try:
            self.assertEqual(core.load_env(path), {"SSH_HOST": "example.com", "SSH_USER": "bob"})
        finally:
            os.remove(path)


class ConfigPersistenceTests(unittest.TestCase):
    def test_save_and_load_config_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "tunnels.json")
            config = {"app_name": "Test App", "tunnels": [{"id": "abc123", "name": "T"}]}
            with patch.object(core, "CONFIG_FILE", config_file):
                core.save_config(config)
                loaded = core.load_config()
            self.assertEqual(loaded, config)

    def test_load_config_returns_default_when_nothing_exists(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "tunnels.json")
            env_file = os.path.join(tmp_dir, ".env")
            with patch.object(core, "CONFIG_FILE", config_file), patch.object(core, "LEGACY_ENV_FILE", env_file):
                loaded = core.load_config()
            self.assertEqual(loaded, {"app_name": core.DEFAULT_APP_NAME, "tunnels": []})

    def test_migrates_legacy_env_on_first_load(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = os.path.join(tmp_dir, "tunnels.json")
            env_file = os.path.join(tmp_dir, ".env")
            with open(env_file, "w") as f:
                f.write(
                    "SSH_HOST=old.example.com\nSSH_USER=alice\nLOCAL_PORT=3307\n"
                    "REMOTE_HOST=127.0.0.1\nREMOTE_PORT=3306\nAPP_NAME=Old App\n"
                )
            with patch.object(core, "CONFIG_FILE", config_file), patch.object(core, "LEGACY_ENV_FILE", env_file):
                config = core.load_config()

            self.assertEqual(config["app_name"], "Old App")
            self.assertEqual(len(config["tunnels"]), 1)
            tunnel = config["tunnels"][0]
            self.assertEqual(tunnel["ssh_host"], "old.example.com")
            self.assertEqual(tunnel["ssh_user"], "alice")
            self.assertEqual(tunnel["local_port"], 3307)
            self.assertTrue(os.path.isfile(config_file))
            self.assertFalse(os.path.isfile(env_file))
            self.assertTrue(os.path.isfile(env_file + ".migrated"))


class TunnelCrudTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.config_file = os.path.join(self.tmp_dir.name, "tunnels.json")
        self.config = {"app_name": "Test", "tunnels": []}

        patcher_file = patch.object(core, "CONFIG_FILE", self.config_file)
        patcher_config = patch.object(core, "CONFIG", self.config)
        patcher_file.start()
        patcher_config.start()
        self.addCleanup(patcher_file.stop)
        self.addCleanup(patcher_config.stop)

    @staticmethod
    def _tunnel_data(**overrides):
        data = {
            "name": "My Tunnel",
            "ssh_host": "host.example.com",
            "ssh_user": "user",
            "local_port": 3307,
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
        }
        data.update(overrides)
        return data

    def test_add_tunnel_assigns_id_and_persists(self):
        tunnel = core.add_tunnel(self._tunnel_data())
        self.assertIn("id", tunnel)
        self.assertEqual(len(core.CONFIG["tunnels"]), 1)
        with open(self.config_file) as f:
            saved = json.load(f)
        self.assertEqual(len(saved["tunnels"]), 1)

    def test_find_tunnel_by_id_and_name_case_insensitive(self):
        tunnel = core.add_tunnel(self._tunnel_data(name="My Tunnel"))
        self.assertEqual(core.find_tunnel(tunnel["id"])["id"], tunnel["id"])
        self.assertEqual(core.find_tunnel("my tunnel")["id"], tunnel["id"])
        self.assertIsNone(core.find_tunnel("does-not-exist"))

    def test_update_tunnel_keeps_id_and_changes_fields(self):
        tunnel = core.add_tunnel(self._tunnel_data(name="Old Name"))
        updated = core.update_tunnel(tunnel["id"], self._tunnel_data(name="New Name"))
        self.assertEqual(updated["id"], tunnel["id"])
        self.assertEqual(updated["name"], "New Name")

    def test_update_tunnel_persists_to_file(self):
        tunnel = core.add_tunnel(self._tunnel_data(name="Old Name"))
        core.update_tunnel(tunnel["id"], self._tunnel_data(name="New Name"))
        with open(self.config_file) as f:
            saved = json.load(f)
        self.assertEqual(saved["tunnels"][0]["name"], "New Name")

    def test_update_unknown_tunnel_returns_none(self):
        self.assertIsNone(core.update_tunnel("nope", self._tunnel_data()))

    def test_remove_tunnel_deletes_entry(self):
        tunnel = core.add_tunnel(self._tunnel_data())
        core.remove_tunnel(tunnel["id"])
        self.assertIsNone(core.find_tunnel(tunnel["id"]))


class ProcessStateTests(unittest.TestCase):
    def test_read_pid_missing_file(self):
        self.assertIsNone(core.read_pid("/nonexistent/file.pid"))

    def test_read_pid_valid_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("12345")
            path = f.name
        try:
            self.assertEqual(core.read_pid(path), 12345)
        finally:
            os.remove(path)

    def test_read_pid_corrupt_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("not-a-pid")
            path = f.name
        try:
            self.assertIsNone(core.read_pid(path))
        finally:
            os.remove(path)

    def test_is_running_true_for_current_process(self):
        self.assertTrue(core.is_running(os.getpid()))

    def test_is_running_false_for_bogus_pid(self):
        self.assertFalse(core.is_running(999999999))

    def test_is_running_false_for_none(self):
        self.assertFalse(core.is_running(None))

    def test_port_is_listening_true_when_bound(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.assertTrue(core.port_is_listening(port))
        finally:
            server.close()

    def test_port_is_listening_false_when_nothing_bound(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.close()
        self.assertFalse(core.port_is_listening(port))

    def test_read_io_counters_for_current_process(self):
        counters = core.read_io_counters(os.getpid())
        self.assertIsNotNone(counters)
        rchar, wchar = counters
        self.assertGreaterEqual(rchar, 0)
        self.assertGreaterEqual(wchar, 0)

    def test_read_io_counters_none_for_nonexistent_pid(self):
        self.assertIsNone(core.read_io_counters(999999999))


class TailLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        patcher = patch.object(core, "RUN_DIR", self.tmp_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_log_file_returns_empty_string(self):
        self.assertEqual(core.tail_log("no-such-tunnel"), "")

    def test_returns_last_n_lines_only(self):
        _, log_file = core.tunnel_paths("t1")
        with open(log_file, "w") as f:
            for i in range(10):
                f.write(f"line {i}\n")
        self.assertEqual(core.tail_log("t1", lines=3), "line 7\nline 8\nline 9")

    def test_returns_all_lines_when_fewer_than_requested(self):
        _, log_file = core.tunnel_paths("t2")
        with open(log_file, "w") as f:
            f.write("only line\n")
        self.assertEqual(core.tail_log("t2", lines=5), "only line")


class AppLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.lock_file = os.path.join(self.tmp_dir.name, "app.pid")
        patcher_lock = patch.object(core, "APP_LOCK_FILE", self.lock_file)
        patcher_run_dir = patch.object(core, "RUN_DIR", self.tmp_dir.name)
        patcher_lock.start()
        patcher_run_dir.start()
        self.addCleanup(patcher_lock.stop)
        self.addCleanup(patcher_run_dir.stop)

    def test_running_app_pid_is_none_when_no_lock(self):
        self.assertIsNone(core.running_app_pid())

    def test_acquire_then_running_app_pid_reports_self(self):
        core.acquire_app_lock()
        self.assertEqual(core.running_app_pid(), os.getpid())

    def test_running_app_pid_none_for_stale_lock(self):
        with open(self.lock_file, "w") as f:
            f.write("999999999")
        self.assertIsNone(core.running_app_pid())

    def test_release_app_lock_removes_file(self):
        core.acquire_app_lock()
        core.release_app_lock()
        self.assertFalse(os.path.isfile(self.lock_file))

    def test_release_app_lock_ignores_other_processes_lock(self):
        with open(self.lock_file, "w") as f:
            f.write("1")
        core.release_app_lock()
        self.assertTrue(os.path.isfile(self.lock_file))


class TunnelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        patcher = patch.object(core, "RUN_DIR", self.tmp_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stop_tunnel_when_not_running(self):
        ok, msg = core.stop_tunnel("nope")
        self.assertTrue(ok)
        self.assertEqual(msg, "Was not active.")

    def test_stop_tunnel_kills_running_process(self):
        proc = subprocess.Popen(["sleep", "30"])
        pid_file, _ = core.tunnel_paths("test-tunnel")
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))
        try:
            ok, msg = core.stop_tunnel("test-tunnel")
            self.assertTrue(ok)
            self.assertIn("Disconnected", msg)
            self.assertFalse(os.path.isfile(pid_file))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_start_tunnel_when_already_running(self):
        proc = subprocess.Popen(["sleep", "30"])
        pid_file, _ = core.tunnel_paths("already-running")
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))
        try:
            tunnel = {
                "id": "already-running",
                "name": "Already",
                "ssh_host": "unused",
                "ssh_user": "unused",
                "local_port": 1,
                "remote_host": "unused",
                "remote_port": 1,
            }
            ok, msg = core.start_tunnel(tunnel)
            self.assertTrue(ok)
            self.assertIn("Already running", msg)
        finally:
            proc.kill()
            proc.wait()

    def test_start_tunnel_reports_failure_and_cleans_up_pid_file(self):
        tunnel = {
            "id": "bad-host",
            "name": "Bad Host",
            "ssh_host": "this-host-does-not-resolve.invalid",
            "ssh_user": "nobody",
            "local_port": 3399,
            "remote_host": "127.0.0.1",
            "remote_port": 3306,
        }
        ok, msg = core.start_tunnel(tunnel)
        self.assertFalse(ok)
        pid_file, _ = core.tunnel_paths("bad-host")
        self.assertFalse(os.path.isfile(pid_file))


class TestConnectionTests(unittest.TestCase):
    @staticmethod
    def _result(returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_success_when_target_reachable(self):
        with patch("subprocess.run", return_value=self._result(stdout="TARGET_OK\n")):
            ok, msg = core.test_connection("host", "user", "127.0.0.1", 80)
        self.assertTrue(ok)
        self.assertIn("reachable", msg)

    def test_failure_when_target_unreachable(self):
        with patch("subprocess.run", return_value=self._result(stdout="TARGET_UNREACHABLE\n")):
            ok, msg = core.test_connection("host", "user", "127.0.0.1", 80)
        self.assertFalse(ok)
        self.assertIn("not reachable", msg)

    def test_failure_when_ssh_itself_fails(self):
        with patch("subprocess.run", return_value=self._result(returncode=255, stderr="Permission denied\n")):
            ok, msg = core.test_connection("host", "user", "127.0.0.1", 80)
        self.assertFalse(ok)
        self.assertIn("SSH connection failed", msg)

    def test_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=5)):
            ok, msg = core.test_connection("host", "user", "127.0.0.1", 80)
        self.assertFalse(ok)
        self.assertEqual(msg, "Connection timed out.")

    def test_os_error(self):
        with patch("subprocess.run", side_effect=OSError("ssh not found")):
            ok, msg = core.test_connection("host", "user", "127.0.0.1", 80)
        self.assertFalse(ok)
        self.assertIn("ssh not found", msg)


if __name__ == "__main__":
    unittest.main()
