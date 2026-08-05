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


class TrayIconConstructionTests(unittest.TestCase):
    def test_stores_constructor_arguments_without_starting_anything(self):
        on_show = lambda: None
        on_exit = lambda: None
        tray = TrayIcon(icon_path="/tmp/icon.png", tooltip="Test", on_show=on_show, on_exit=on_exit)
        self.assertEqual(tray._icon_path, "/tmp/icon.png")
        self.assertEqual(tray._tooltip, "Test")
        self.assertIs(tray._on_show, on_show)
        self.assertIs(tray._on_exit, on_exit)
        self.assertIsNone(tray._thread)
        self.assertIsNone(tray._icon)
        self.assertIsNone(tray._menu)


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
             mock.patch.object(core, "is_running", return_value=running), \
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
             mock.patch.object(core, "is_running", return_value=False):
            sampler.start()
            sampler._thread.join(timeout=0.1)  # keeps sampling until stop() is honoured
            self.assertTrue(sampler._thread.is_alive())
            sampler.stop()
            sampler._thread.join(timeout=2)
        self.assertFalse(sampler._thread.is_alive())
        self.assertTrue(snapshots)


if __name__ == "__main__":
    unittest.main()
