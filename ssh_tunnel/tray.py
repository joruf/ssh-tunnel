"""System tray integration using GTK3."""

import os
import threading


class TrayIcon:
    """
    GTK3 status icon that keeps the app available from the system tray.

    The icon lives in its own GTK main loop thread, so on_show/on_exit are
    called from that thread and MUST return immediately - they may not call into
    Tkinter (see TunnelApp.post). A callback that blocks stops the GTK loop, and
    with it the tray icon: while a context menu is open it also holds an X
    pointer grab, which makes the whole desktop appear frozen.
    """

    def __init__(self, icon_path, tooltip, on_show, on_exit):
        self._icon_path = icon_path
        self._tooltip = tooltip
        self._on_show = on_show
        self._on_exit = on_exit
        self._thread = None
        self._icon = None
        self._menu = None

    def start(self):
        """
        Starts the tray icon in a background thread.

        @return bool True when GTK3 tray support is available.
        """
        try:
            import gi

            gi.require_version("Gtk", "3.0")
        except (ImportError, ValueError):
            return False

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk, Gtk

        try:
            Gdk.notify_startup_complete()
        except (AttributeError, TypeError):
            pass

        icon = Gtk.StatusIcon()
        self._icon = icon
        if os.path.isfile(self._icon_path):
            icon.set_from_file(self._icon_path)
        icon.set_tooltip_text(self._tooltip)
        icon.connect("activate", self._handle_show)
        icon.connect("popup-menu", self._popup_menu)
        icon.set_visible(True)

        Gtk.main()

    def _handle_show(self, *_args):
        self._on_show()

    def _popup_menu(self, _icon, button, activate_time):
        from gi.repository import Gtk

        menu = Gtk.Menu()
        # Keep the menu referenced: nothing else owns it while it is popped up,
        # so letting it go out of scope can have Python garbage-collect it right
        # after the click - the menu then never appears (or vanishes at once).
        self._menu = menu

        show_item = Gtk.MenuItem(label="Show")
        show_item.connect("activate", self._handle_show)
        show_item.show()
        menu.append(show_item)

        menu.append(Gtk.SeparatorMenuItem())

        exit_item = Gtk.MenuItem(label="Exit")
        exit_item.connect("activate", self._handle_exit)
        exit_item.show()
        menu.append(exit_item)

        menu.show()
        menu.popup(None, None, None, None, button, activate_time)

    def _handle_exit(self, *_args):
        self._on_exit()
