"""
The system tray icon, running as a process of its own.

Started by ssh_tunnel.tray.TrayIcon, whose docstring explains why this is a
separate process rather than a thread. Everything GTK lives in here and nowhere
else, so the GUI process never loads GTK at all.

Protocol - one line per message on stdout:
    ready   printed once, as soon as the icon is visible
    show    the user clicked the icon or picked "Show"
    exit    the user picked "Exit"

Exit status 1 means GTK3 is unavailable; that is how the GUI learns that this
desktop cannot show a tray icon.
"""

import os
import sys

PARENT_POLL_SECONDS = 5


def emit(message):
    """
    Sends one protocol line to the GUI process.

    @param message str Line to send, without the newline.
    @return None
    """
    try:
        sys.stdout.write(message + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, ValueError):
        # The GUI process is gone, so there is nobody left to report to and
        # nothing left to show an icon for.
        os._exit(0)


class TrayProcess:
    """Owns the GTK status icon, its menu and the GTK main loop."""

    def __init__(self, icon_path, tooltip):
        """
        @param icon_path str Path to the icon image; a missing file just means
            the desktop's fallback icon is used.
        @param tooltip str Tooltip text for the icon.
        """
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.parent_pid = os.getppid()
        self.menu = None

    def run(self):
        """Shows the icon and runs the GTK main loop until "Exit" or orphaning."""
        from gi.repository import GLib, Gtk

        icon = Gtk.StatusIcon()
        if os.path.isfile(self.icon_path):
            icon.set_from_file(self.icon_path)
        icon.set_tooltip_text(self.tooltip)
        icon.connect("activate", self.on_activate)
        icon.connect("popup-menu", self.on_popup_menu)
        icon.set_visible(True)

        GLib.timeout_add_seconds(PARENT_POLL_SECONDS, self.exit_when_orphaned)

        emit("ready")
        Gtk.main()

    def exit_when_orphaned(self):
        """
        Quits once the GUI process is gone, so its icon does not linger in the
        tray as a dead entry. A killed parent leaves us reparented to init.

        @return bool True to keep the timer running.
        """
        if os.getppid() != self.parent_pid:
            from gi.repository import Gtk

            Gtk.main_quit()
            return False
        return True

    def on_activate(self, *_args):
        emit("show")

    def on_popup_menu(self, _icon, button, activate_time):
        from gi.repository import Gtk

        menu = Gtk.Menu()
        # Keep the menu referenced: nothing else owns it while it is popped up,
        # so letting it go out of scope can have Python garbage-collect it right
        # after the click - the menu then never appears (or vanishes at once).
        self.menu = menu

        show_item = Gtk.MenuItem(label="Show")
        show_item.connect("activate", lambda *_a: emit("show"))
        show_item.show()
        menu.append(show_item)

        menu.append(Gtk.SeparatorMenuItem())

        exit_item = Gtk.MenuItem(label="Exit")
        exit_item.connect("activate", self.on_exit_clicked)
        exit_item.show()
        menu.append(exit_item)

        menu.show()
        menu.popup(None, None, None, None, button, activate_time)

    def on_exit_clicked(self, *_args):
        from gi.repository import Gtk

        emit("exit")
        Gtk.main_quit()


def main(argv):
    """
    @param argv list Command line arguments: icon path, tooltip.
    @return int Process exit status.
    """
    icon_path = argv[0] if argv else ""
    tooltip = argv[1] if len(argv) > 1 else "SSH Tunnel"

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # noqa: F401  (import is the availability check)
    except (ImportError, ValueError):
        print("GTK3 (python3-gi) is unavailable, so there will be no tray icon.", file=sys.stderr)
        return 1

    TrayProcess(icon_path, tooltip).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
