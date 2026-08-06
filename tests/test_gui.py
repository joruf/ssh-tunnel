"""
Tests for the display-independent logic in ssh_tunnel.gui and ssh_tunnel.tray.
Importing these modules is safe without a display (Tkinter/GTK are only
touched once a widget/tray icon is actually constructed/started, which none
of these tests do), so this suite runs fine in a headless environment.
"""

import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssh_tunnel import core
from ssh_tunnel.gui import TUNNEL_FORM_FIELDS, StatusSampler, UiCallQueue, format_rate
from ssh_tunnel.tray import TrayIcon


class FormatRateTests(unittest.TestCase):
    def test_bytes_per_second(self):
        self.assertEqual(format_rate(0), "0 B/s")
        self.assertEqual(format_rate(512), "512 B/s")
        self.assertEqual(format_rate(1023), "1023 B/s")

    def test_kilobytes_per_second(self):
        self.assertEqual(format_rate(1024), "1.0 KB/s")
        self.assertEqual(format_rate(1536), "1.5 KB/s")
        self.assertEqual(format_rate(1024 * 1024 - 1), "1024.0 KB/s")

    def test_megabytes_per_second(self):
        self.assertEqual(format_rate(1024 * 1024), "1.0 MB/s")
        self.assertEqual(format_rate(1024 * 1024 * 2.5), "2.5 MB/s")


class TunnelFormFieldsConsistencyTests(unittest.TestCase):
    """Guards against the Add/Edit dialog's fields drifting out of sync with
    core.TUNNEL_FIELDS (the keys actually persisted to tunnels.json)."""

    def test_form_field_keys_match_core_tunnel_fields(self):
        form_keys = [key for key, _label in TUNNEL_FORM_FIELDS]
        self.assertEqual(form_keys, core.TUNNEL_FIELDS)


class FakeTrayProcess:
    """Stands in for the tray subprocess, handing out canned protocol lines."""

    def __init__(self, lines=()):
        self._lines = list(lines)
        self.stdout = self
        self.terminated = False

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def __iter__(self):
        while self._lines:
            yield self._lines.pop(0)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class TrayIconConstructionTests(unittest.TestCase):
    def test_stores_constructor_arguments_without_starting_anything(self):
        on_show = lambda: None
        on_exit = lambda: None
        tray = TrayIcon(icon_path="/tmp/icon.png", tooltip="Test", on_show=on_show, on_exit=on_exit)
        self.assertEqual(tray._icon_path, "/tmp/icon.png")
        self.assertEqual(tray._tooltip, "Test")
        self.assertIs(tray._on_show, on_show)
        self.assertIs(tray._on_exit, on_exit)
        self.assertIsNone(tray._on_lost)
        self.assertIsNone(tray._process)
        self.assertIsNone(tray._reader)

    def test_stop_without_a_started_process_is_a_noop(self):
        TrayIcon(icon_path="/tmp/icon.png", tooltip="Test", on_show=lambda: None, on_exit=lambda: None).stop()

    def test_child_env_makes_the_package_importable(self):
        env = TrayIcon._child_env()
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], os.path.dirname(os.path.dirname(__file__)))


class TrayIconProtocolTests(unittest.TestCase):
    """The tray icon lives in its own process and reports back one line at a
    time; this is the parent side of that protocol."""

    def setUp(self):
        self.shown = []
        self.exited = []
        self.lost = []

    def tray(self, with_on_lost=False):
        return TrayIcon(
            icon_path="/tmp/icon.png",
            tooltip="Test",
            on_show=lambda: self.shown.append(True),
            on_exit=lambda: self.exited.append(True),
            on_lost=(lambda: self.lost.append(True)) if with_on_lost else None,
        )

    def test_show_and_exit_lines_reach_their_callbacks(self):
        self.tray()._read_events(FakeTrayProcess(["show\n", "exit\n"]))
        self.assertEqual(len(self.shown), 1)
        self.assertEqual(len(self.exited), 1)

    def test_unknown_lines_are_ignored(self):
        self.tray()._read_events(FakeTrayProcess(["ready\n", "\n", "nonsense\n"]))
        self.assertEqual(self.shown, [])
        self.assertEqual(self.exited, [])

    def test_a_tray_process_that_dies_reports_the_loss(self):
        self.tray(with_on_lost=True)._read_events(FakeTrayProcess())
        self.assertEqual(len(self.lost), 1)

    def test_an_exit_the_user_asked_for_is_not_reported_as_a_loss(self):
        self.tray(with_on_lost=True)._read_events(FakeTrayProcess(["exit\n"]))
        self.assertEqual(self.lost, [])

    def test_start_succeeds_once_the_child_reports_ready(self):
        tray = self.tray()
        process = FakeTrayProcess(["ready\n"])
        with mock.patch("ssh_tunnel.tray.subprocess.Popen", return_value=process):
            self.assertTrue(tray.start())
        self.assertIs(tray._process, process)
        tray.stop()
        self.assertTrue(process.terminated)
        self.assertIsNone(tray._process)

    def test_start_fails_and_cleans_up_when_the_child_never_reports_ready(self):
        tray = self.tray()
        process = FakeTrayProcess()
        with mock.patch("ssh_tunnel.tray.subprocess.Popen", return_value=process):
            self.assertFalse(tray.start())
        self.assertTrue(process.terminated)
        self.assertIsNone(tray._process)

    def test_start_fails_when_the_child_cannot_be_spawned(self):
        tray = self.tray()
        with mock.patch("ssh_tunnel.tray.subprocess.Popen", side_effect=OSError("no python")):
            self.assertFalse(tray.start())


class UiCallQueueTests(unittest.TestCase):
    """The queue the tray thread, the workers and the SIGUSR1 handler use to
    reach the Tk main thread instead of calling Tkinter across threads."""

    def test_post_does_not_run_the_callback(self):
        queue = UiCallQueue()
        calls = []
        queue.post(lambda: calls.append("ran"))
        self.assertEqual(calls, [])

    def test_drain_runs_callbacks_in_order(self):
        queue = UiCallQueue()
        calls = []
        for index in range(3):
            queue.post(lambda index=index: calls.append(index))
        queue.drain()
        self.assertEqual(calls, [0, 1, 2])

    def test_drain_on_empty_queue_is_a_noop(self):
        UiCallQueue().drain()

    def test_drain_also_runs_callbacks_queued_by_callbacks(self):
        queue = UiCallQueue()
        calls = []
        queue.post(lambda: (calls.append("outer"), queue.post(lambda: calls.append("inner"))))
        queue.drain()
        self.assertEqual(calls, ["outer", "inner"])

    def test_drain_drops_the_rest_once_should_stop_returns_true(self):
        queue = UiCallQueue()
        calls = []
        exiting = []
        queue.post(lambda: (calls.append("quit"), exiting.append(True)))
        queue.post(lambda: calls.append("too late"))
        queue.drain(should_stop=lambda: bool(exiting))
        self.assertEqual(calls, ["quit"])

        # The dropped callback must not resurface on the next drain either.
        exiting.clear()
        queue.drain()
        self.assertEqual(calls, ["quit"])

    def test_posting_from_other_threads_keeps_every_callback(self):
        queue = UiCallQueue()
        calls = []
        threads = [threading.Thread(target=queue.post, args=(lambda: calls.append(1),)) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        queue.drain()
        self.assertEqual(len(calls), 20)


class StatusSamplerTests(unittest.TestCase):
    """Sampling runs off the Tk main thread; these tests exercise the sampling
    itself, which is plain logic over core."""

    def setUp(self):
        self.sampler = StatusSampler(on_snapshot=lambda snapshot: None)
        self.tunnel = {"id": "abc123", "name": "Test", "local_port": 3307}
        patcher = mock.patch.dict(core.CONFIG, {"tunnels": [self.tunnel]})
        patcher.start()
        self.addCleanup(patcher.stop)

    def sample_with(self, pid, running, listening):
        with mock.patch.object(core, "read_pid", return_value=pid), \
             mock.patch.object(core, "process_is_tunnel", return_value=running), \
             mock.patch.object(core, "port_is_listening", return_value=listening), \
             mock.patch.object(core, "read_io_counters", return_value=None):
            return self.sampler.sample()

    def test_not_running_is_disconnected(self):
        snapshot = self.sample_with(pid=None, running=False, listening=False)
        self.assertEqual(snapshot["abc123"], ("● Disconnected", "disconnected", "–"))

    def test_running_and_listening_is_connected(self):
        snapshot = self.sample_with(pid=42, running=True, listening=True)
        self.assertEqual(snapshot["abc123"], ("● Connected", "connected", "–"))

    def test_running_without_open_port_is_still_connecting(self):
        snapshot = self.sample_with(pid=42, running=True, listening=False)
        self.assertEqual(snapshot["abc123"], ("● Connecting ...", "pending", "–"))

    def test_traffic_needs_two_samples_of_the_same_process(self):
        with mock.patch.object(core, "read_io_counters", return_value=(1000, 2000)):
            self.assertEqual(self.sampler._traffic_text("abc123", 42, True, now=100.0), "–")
        with mock.patch.object(core, "read_io_counters", return_value=(1000 + 2048, 2000 + 1024)):
            self.assertEqual(
                self.sampler._traffic_text("abc123", 42, True, now=102.0),
                "↓ 1.0 KB/s  ↑ 512 B/s",
            )

    def test_traffic_history_restarts_on_a_new_pid(self):
        with mock.patch.object(core, "read_io_counters", return_value=(1000, 2000)):
            self.sampler._traffic_text("abc123", 42, True, now=100.0)
        with mock.patch.object(core, "read_io_counters", return_value=(0, 0)):
            self.assertEqual(self.sampler._traffic_text("abc123", 43, True, now=102.0), "–")

    def test_forget_drops_a_removed_tunnels_history(self):
        with mock.patch.object(core, "read_io_counters", return_value=(1000, 2000)):
            self.sampler._traffic_text("abc123", 42, True, now=100.0)
        self.assertIn("abc123", self.sampler._traffic_prev)
        self.sampler.forget("abc123")
        self.assertNotIn("abc123", self.sampler._traffic_prev)

    def test_stopping_ends_the_sampler_thread(self):
        snapshots = []
        sampler = StatusSampler(on_snapshot=snapshots.append, interval_seconds=0.01)
        with mock.patch.object(core, "read_pid", return_value=None), \
             mock.patch.object(core, "process_is_tunnel", return_value=False):
            sampler.start()
            sampler._thread.join(timeout=0.1)  # keeps sampling until stop() is honoured
            self.assertTrue(sampler._thread.is_alive())
            sampler.stop()
            sampler._thread.join(timeout=2)
        self.assertFalse(sampler._thread.is_alive())
        self.assertTrue(snapshots)


if __name__ == "__main__":
    unittest.main()
