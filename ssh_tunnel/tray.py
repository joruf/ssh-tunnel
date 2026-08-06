"""System tray integration, driven by a separate GTK3 process."""

import os
import subprocess
import sys
import threading

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TRAY_MODULE = "ssh_tunnel.tray_process"
READY_TOKEN = "ready"
READY_TIMEOUT_SECONDS = 15
STOP_TIMEOUT_SECONDS = 3


class TrayIcon:
    """
    System tray icon for the app, hosted in a child process.

    The icon is a GTK3 status icon, and it deliberately does not live in the
    GUI process. Tk and GTK each drive their own event loop over their own Xlib
    connection, and GDK calls XInitThreads() when it initialises - which Xlib
    only allows before the first display is opened, long after Tk has opened
    its own. Sharing one libX11 that way eventually left the Tk main thread
    blocked inside Xlib waiting for a reply that never arrived: the window
    stayed on screen but stopped reacting to clicks, and a double click no
    longer even selected a row.

    In its own process each toolkit gets its own libX11 back. The two talk over
    the child's stdout, one line per message (see ssh_tunnel.tray_process).
    """

    def __init__(self, icon_path, tooltip, on_show, on_exit, on_lost=None):
        """
        @param icon_path str Path to the tray icon image.
        @param tooltip str Tooltip text for the icon.
        @param on_show callable Called when the user asks for the window.
        @param on_exit callable Called when the user picks "Exit".
        @param on_lost callable|None Called if the tray process dies on its own,
            so the GUI can stop hiding into a tray that is no longer there.
        """
        self._icon_path = icon_path
        self._tooltip = tooltip
        self._on_show = on_show
        self._on_exit = on_exit
        self._on_lost = on_lost
        self._process = None
        self._reader = None
        self._stopping = threading.Event()

    def start(self):
        """
        Starts the tray process and waits for it to report that its icon is up.

        Blocks until the child is ready (or fails), so call this off the Tk main
        thread.

        @return bool True when the tray icon is showing.
        """
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", TRAY_MODULE, self._icon_path, self._tooltip],
                cwd=APP_ROOT,
                env=self._child_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError:
            return False

        # The child should answer within a second; the timer is only there so a
        # child that hangs on startup cannot keep this thread waiting forever.
        watchdog = threading.Timer(READY_TIMEOUT_SECONDS, process.kill)
        watchdog.start()
        try:
            first_line = process.stdout.readline()
        finally:
            watchdog.cancel()

        if first_line.strip() != READY_TOKEN:
            self._terminate(process)
            return False

        self._process = process
        self._reader = threading.Thread(target=self._read_events, args=(process,), daemon=True)
        self._reader.start()
        return True

    def stop(self):
        """Shuts the tray process down. Safe to call more than once."""
        self._stopping.set()
        process, self._process = self._process, None
        if process is not None:
            self._terminate(process)

    @staticmethod
    def _child_env():
        """Environment for the child, with the app importable as a package."""
        python_path = os.environ.get("PYTHONPATH", "")
        return {
            **os.environ,
            "PYTHONPATH": APP_ROOT + (os.pathsep + python_path if python_path else ""),
        }

    @staticmethod
    def _terminate(process):
        """Ends the tray process, escalating to SIGKILL if it does not go."""
        try:
            process.terminate()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        except OSError:
            pass

    def _read_events(self, process):
        """
        Turns the child's protocol lines into callbacks. Runs in its own thread,
        so the callbacks must not touch Tkinter directly (see TunnelApp.post).

        @param process subprocess.Popen The tray process to read from.
        @return None
        """
        for line in process.stdout:
            command = line.strip()
            if command == "show":
                self._on_show()
            elif command == "exit":
                # Expected shutdown: the child quits right after this line, so
                # its closing stdout below must not be reported as a crash.
                self._stopping.set()
                self._on_exit()

        # stdout is closed, so the tray process has ended - either because it
        # was told to (Exit, or stop()), or because it died. In the latter case
        # the GUI must be told, or the window could be hidden into a tray that
        # no longer exists and become unreachable.
        if not self._stopping.is_set() and self._on_lost is not None:
            self._on_lost()
