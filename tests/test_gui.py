"""
Tests for the display-independent logic in ssh_tunnel.gui and ssh_tunnel.tray.
Importing these modules is safe without a display (Tkinter/GTK are only
touched once a widget/tray icon is actually constructed/started, which none
of these tests do), so this suite runs fine in a headless environment.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssh_tunnel import core
from ssh_tunnel.gui import TUNNEL_FORM_FIELDS, format_rate
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


if __name__ == "__main__":
    unittest.main()
