"""
Tkinter GUI for managing and toggling SSH tunnels (local port forwarding).
"""

import collections
import os
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from ssh_tunnel import core
from ssh_tunnel.tray import TrayIcon

POLL_INTERVAL_MS = 2000
UI_QUEUE_INTERVAL_MS = 100


def format_rate(bytes_per_sec):
    """Formats a byte rate as a short human-readable string (B/s, KB/s or MB/s)."""
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"

# Neutral gray palette (matches devserver-commander's ttk theme).
BG = "#f4f4f5"
PANEL_BG = "#ffffff"
FG = "#18181b"
MUTED_FG = "#71717a"
BORDER = "#e4e4e7"
ACCENT = "#3f3f46"
ACCENT_HOVER = "#27272a"
ACCENT_PRESSED = "#18181b"
FOCUS = "#a1a1aa"

COLOR_OK = "#16a34a"
COLOR_OFF = MUTED_FG
COLOR_ERROR = "#dc2626"
COLOR_PENDING = "#d97706"

TUNNEL_FORM_FIELDS = [
    ("name", "Name"),
    ("ssh_host", "SSH Host"),
    ("ssh_user", "SSH User"),
    ("local_port", "Local Port"),
    ("remote_host", "Remote Host"),
    ("remote_port", "Remote Port"),
]


def apply_theme(widget):
    """
    Applies the shared neutral-gray ttk theme to the given root/toplevel.

    @param widget tk.Misc Root or Toplevel to style.
    @return None
    """
    style = ttk.Style(widget)
    if "clam" in set(style.theme_names()):
        style.theme_use("clam")

    widget.configure(background=BG)
    widget.option_add("*Background", BG)
    widget.option_add("*Foreground", FG)
    widget.option_add("*Font", "TkDefaultFont 10")
    widget.option_add("*Menu.Background", PANEL_BG)
    widget.option_add("*Menu.Foreground", FG)
    widget.option_add("*Menu.ActiveBackground", ACCENT)
    widget.option_add("*Menu.ActiveForeground", "#fafafa")

    style.configure(".", background=BG, foreground=FG)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure(
        "TButton",
        padding=(12, 7),
        background=PANEL_BG,
        foreground=FG,
        borderwidth=1,
        bordercolor=BORDER,
        focusthickness=1,
        focuscolor=FOCUS,
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("active", "#fafafa"), ("pressed", BG), ("disabled", BG)],
        foreground=[("disabled", FOCUS)],
        bordercolor=[("active", "#d4d4d8"), ("disabled", BORDER)],
    )
    style.configure(
        "Primary.TButton",
        padding=(12, 7),
        background=ACCENT,
        foreground="#fafafa",
        borderwidth=0,
        focusthickness=0,
        relief="flat",
    )
    style.map(
        "Primary.TButton",
        background=[("active", ACCENT_HOVER), ("pressed", ACCENT_PRESSED), ("disabled", FOCUS)],
        foreground=[("disabled", BG)],
    )
    style.configure(
        "TEntry",
        padding=(8, 6),
        fieldbackground=PANEL_BG,
        foreground=FG,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        relief="flat",
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", FOCUS)],
        lightcolor=[("focus", FOCUS)],
        darkcolor=[("focus", FOCUS)],
    )
    style.configure(
        "Tunnels.Treeview",
        rowheight=32,
        background=PANEL_BG,
        fieldbackground=PANEL_BG,
        foreground=FG,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        font="TkDefaultFont 11",
    )
    style.map("Tunnels.Treeview", background=[("selected", "#e4e4e7")], foreground=[("selected", FG)])
    style.configure(
        "Tunnels.Treeview.Heading",
        padding=(10, 8),
        background=BG,
        foreground=MUTED_FG,
        bordercolor=BORDER,
        relief="flat",
        font="TkDefaultFont 10 bold",
    )
    style.map("Tunnels.Treeview.Heading", background=[("active", "#fafafa")], foreground=[("active", FG)])


class UiCallQueue:
    """
    Hands callables from other threads to the Tk main thread.

    Tkinter must only ever be called from the thread that runs mainloop(). A
    call from another thread (the GTK tray thread, a worker, a signal handler)
    is queued into Tcl and the calling thread then waits until the main thread
    is back in its event loop - so while the main thread is busy, the caller
    hangs with it. That is what froze the tray icon.

    collections.deque append/popleft are atomic in CPython, so this needs no
    lock. That matters for signal handlers too: a lock-based queue can deadlock
    when the signal interrupts the main thread while it holds the same lock.
    """

    def __init__(self):
        self._calls = collections.deque()

    def post(self, callback):
        """
        Queues a callback for the Tk main thread. Never blocks.

        @param callback callable Called without arguments on the main thread.
        @return None
        """
        self._calls.append(callback)

    def drain(self, should_stop=None):
        """
        Runs all queued callbacks in order, including ones they queue themselves.

        @param should_stop callable|None Checked before each callback; when it
            returns True the remaining queue is dropped (used to stop feeding a
            GUI that is already shutting down).
        @return None
        """
        while True:
            if should_stop is not None and should_stop():
                self._calls.clear()
                return
            try:
                callback = self._calls.popleft()
            except IndexError:
                return
            callback()


class StatusSampler:
    """
    Samples every tunnel's state and throughput in a background thread.

    Sampling blocks: core.port_is_listening() opens a socket (up to a second per
    tunnel when the connection is unhealthy) and the PID/throughput data comes
    from files. On the Tk main thread that stalls redraws - and for the reason
    described in UiCallQueue, it stalls the tray thread along with them. So it
    happens here, and only the finished snapshot is applied to the widgets.
    """

    def __init__(self, on_snapshot, interval_seconds=POLL_INTERVAL_MS / 1000):
        """
        @param on_snapshot callable Receives each finished snapshot dict. Called
            from the sampler thread, so it must not touch Tkinter.
        @param interval_seconds float Delay between samples.
        """
        self._on_snapshot = on_snapshot
        self._interval = interval_seconds
        self._wakeup = threading.Event()
        self._stopped = threading.Event()
        self._traffic_prev = {}
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stopped.set()
        self._wakeup.set()

    def request_sample(self):
        """Asks for a fresh sample now instead of waiting out the interval."""
        self._wakeup.set()

    def forget(self, tunnel_id):
        """Drops a removed tunnel's throughput history."""
        self._traffic_prev.pop(tunnel_id, None)

    def _loop(self):
        while not self._stopped.is_set():
            self._on_snapshot(self.sample())
            self._wakeup.wait(self._interval)
            self._wakeup.clear()

    def sample(self):
        """
        Reads the current state of every configured tunnel.

        @return dict Tunnel id -> (status_text, row_tag, traffic_text).
        """
        now = time.time()
        snapshot = {}
        # Copy the list: the main thread may add or remove tunnels while we read.
        for tunnel in list(core.CONFIG["tunnels"]):
            tunnel_id = tunnel["id"]
            pid_file, _ = core.tunnel_paths(tunnel_id)
            pid = core.read_pid(pid_file)
            running = core.is_running(pid)
            if not running:
                status_text, tag = "● Disconnected", "disconnected"
            elif core.port_is_listening(tunnel["local_port"]):
                status_text, tag = "● Connected", "connected"
            else:
                status_text, tag = "● Connecting ...", "pending"
            snapshot[tunnel_id] = (status_text, tag, self._traffic_text(tunnel_id, pid, running, now))
        return snapshot

    def _traffic_text(self, tunnel_id, pid, running, now):
        """
        Derives a "↓ rx/s  ↑ tx/s" throughput string for a tunnel by diffing two
        /proc/<pid>/io samples over time. Returns "–" until a second sample has
        been taken (or the tunnel isn't running / isn't running under Linux).

        @param tunnel_id str Tunnel id (used as the sample cache key).
        @param pid int|None Current PID of the tunnel's ssh process.
        @param running bool Whether the tunnel is currently running.
        @param now float Current time.time(), passed in so all tunnels use one timestamp.
        @return str The formatted throughput, or "–".
        """
        if not running:
            self._traffic_prev.pop(tunnel_id, None)
            return "–"

        counters = core.read_io_counters(pid)
        if counters is None:
            return "–"
        rchar, wchar = counters

        prev = self._traffic_prev.get(tunnel_id)
        self._traffic_prev[tunnel_id] = (pid, now, rchar, wchar)
        if prev is None or prev[0] != pid:
            return "–"

        _, prev_time, prev_rchar, prev_wchar = prev
        elapsed = now - prev_time
        if elapsed <= 0:
            return "–"
        rx_rate = max(0, (rchar - prev_rchar) / elapsed)
        tx_rate = max(0, (wchar - prev_wchar) / elapsed)
        return f"↓ {format_rate(rx_rate)}  ↑ {format_rate(tx_rate)}"


class TunnelDialog(tk.Toplevel):
    """Dialog to add/edit a single tunnel definition and test it before saving."""

    def __init__(self, parent, on_saved, post, tunnel=None):
        """
        @param parent tk.Misc Parent window.
        @param on_saved callable Called on the main thread after a successful save.
        @param post callable TunnelApp.post, used to get the connection test's
            result back onto the Tk main thread.
        @param tunnel dict|None Tunnel to edit, or None to add a new one.
        """
        super().__init__(parent)
        self.on_saved = on_saved
        self.post = post
        self.tunnel = tunnel
        self.title("Edit Tunnel" if tunnel else "Add Tunnel")
        apply_theme(self)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        container = ttk.Frame(self, padding=(20, 16))
        container.pack()

        self.vars = {}
        for row, (key, label) in enumerate(TUNNEL_FORM_FIELDS):
            ttk.Label(container, text=label).grid(row=row, column=0, sticky="w", pady=4)
            value = tunnel[key] if tunnel else ""
            var = tk.StringVar(value=str(value))
            entry = ttk.Entry(container, textvariable=var, width=28)
            entry.grid(row=row, column=1, sticky="we", padx=(10, 0), pady=4)
            self.vars[key] = var

        self.status_var = tk.StringVar(value="")
        self.status_label = ttk.Label(
            container,
            textvariable=self.status_var,
            foreground=COLOR_OFF,
            wraplength=320,
            justify="left",
            anchor="w",
        )
        self.status_label.grid(row=len(TUNNEL_FORM_FIELDS), column=0, columnspan=2, sticky="we", pady=(8, 8))

        button_row = ttk.Frame(container)
        button_row.grid(row=len(TUNNEL_FORM_FIELDS) + 1, column=0, columnspan=2, sticky="e")

        self.test_button = ttk.Button(button_row, text="Test Connection", command=self.on_test)
        self.test_button.pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(button_row, text="Save", style="Primary.TButton", command=self.on_save).pack(side="left")

    def set_status(self, text, color=COLOR_OFF):
        self.status_var.set(text)
        self.status_label.configure(foreground=color)

    def read_values(self):
        return {key: var.get().strip() for key, var in self.vars.items()}

    def on_test(self):
        values = self.read_values()
        try:
            remote_port = int(values["remote_port"])
        except ValueError:
            self.set_status("Remote Port must be a number.", COLOR_ERROR)
            return

        self.test_button.configure(state="disabled")
        self.set_status("Testing connection ...", COLOR_PENDING)
        # core.test_connection() waits on ssh for up to ten seconds, so it runs
        # off the main thread - otherwise the dialog freezes for that long.
        threading.Thread(target=self.run_test, args=(values, remote_port), daemon=True).start()

    def run_test(self, values, remote_port):
        """
        Probes the connection. Runs in a worker thread, so it must not touch
        Tkinter - the result goes back through post().

        @param values dict Field values as entered in the dialog.
        @param remote_port int Already-validated remote port.
        @return None
        """
        ok, message = core.test_connection(values["ssh_host"], values["ssh_user"], values["remote_host"], remote_port)
        self.post(lambda: self.show_test_result(ok, message))

    def show_test_result(self, ok, message):
        """Applies a finished connection test on the Tk main thread."""
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            # Dialog (or the whole app) was closed while the test was running.
            return
        self.set_status(message, COLOR_OK if ok else COLOR_ERROR)
        self.test_button.configure(state="normal")

    def on_save(self):
        values = self.read_values()
        for key in ("name", "ssh_host", "ssh_user", "remote_host"):
            if not values[key]:
                label = dict(TUNNEL_FORM_FIELDS)[key]
                self.set_status(f"{label} must not be empty.", COLOR_ERROR)
                return
        try:
            values["local_port"] = int(values["local_port"])
            values["remote_port"] = int(values["remote_port"])
        except ValueError:
            self.set_status("Local Port and Remote Port must be numbers.", COLOR_ERROR)
            return

        if self.tunnel:
            core.update_tunnel(self.tunnel["id"], values)
        else:
            core.add_tunnel(values)
        self.on_saved()
        self.destroy()


class TunnelApp:
    def __init__(self, root, start_hidden=False):
        """
        @param root tk.Tk The application's root window.
        @param start_hidden bool True to stay in the system tray on start instead of
            showing the window (used by the autostart entry via run.py --tray).
        """
        self.root = root
        self.start_hidden = start_hidden
        if start_hidden:
            # Withdraw before any widget is drawn so the window never flashes up.
            self.root.withdraw()
        self.root.title(core.CONFIG.get("app_name", core.DEFAULT_APP_NAME))
        if os.path.isfile(core.ICON_FILE):
            self.root.iconphoto(True, tk.PhotoImage(file=core.ICON_FILE))
        apply_theme(self.root)
        self.root.minsize(700, 360)
        self.busy_ids = set()
        self.tray_icon = None
        self.exiting = False
        self.ui_calls = UiCallQueue()
        # maxlen=1: if the GUI was busy, only the newest snapshot is still worth showing.
        self.status_snapshots = collections.deque(maxlen=1)
        self.last_status = {}
        self.sampler = StatusSampler(on_snapshot=self.status_snapshots.append)

        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        toolbar = ttk.Frame(root, padding=(12, 10, 12, 0))
        toolbar.pack(side="top", fill="x")

        ttk.Button(toolbar, text="Add", style="Primary.TButton", command=self.open_add_dialog).pack(
            side="left", padx=(0, 6)
        )
        self.edit_button = ttk.Button(toolbar, text="Edit", command=self.open_edit_dialog, state="disabled")
        self.edit_button.pack(side="left", padx=(0, 6))
        self.remove_button = ttk.Button(toolbar, text="Remove", command=self.remove_selected, state="disabled")
        self.remove_button.pack(side="left")

        detail_bar = ttk.Frame(root, padding=(12, 12, 12, 4))
        detail_bar.pack(side="top", fill="x")
        self.detail_content = ttk.Frame(detail_bar)
        self.detail_content.pack(anchor="w")
        self.render_detail(None)

        tree_frame = ttk.Frame(root, padding=(12, 10, 12, 6))
        tree_frame.pack(side="top", fill="both", expand=True)

        columns = ("connection", "status", "traffic")
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="tree headings",
            height=10,
            style="Tunnels.Treeview",
        )
        self.tree.heading("#0", text="Name")
        self.tree.heading("connection", text="Connection")
        self.tree.heading("status", text="Status")
        self.tree.heading("traffic", text="Traffic")
        self.tree.column("#0", width=170, anchor="w")
        self.tree.column("connection", width=340, anchor="w")
        self.tree.column("status", width=130, anchor="w")
        self.tree.column("traffic", width=190, anchor="w")
        self.tree.tag_configure("connected", foreground=COLOR_OK)
        self.tree.tag_configure("pending", foreground=COLOR_PENDING)
        self.tree.tag_configure("disconnected", foreground=COLOR_OFF)

        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", self.on_row_double_click)

        self.message_var = tk.StringVar(value="Click a tunnel to connect or disconnect it.")
        message_bar = ttk.Frame(root, padding=(12, 0, 12, 10))
        message_bar.pack(side="top", fill="x")
        self.message_label = ttk.Label(
            message_bar, textvariable=self.message_var, foreground=COLOR_OFF, anchor="w"
        )
        self.message_label.pack(anchor="w", fill="x")

        self.refresh_tree()
        self.sampler.start()
        self.root.after(UI_QUEUE_INTERVAL_MS, self.pump)
        self.root.after_idle(self.start_tray_icon)

    # -- cross-thread plumbing ----------------------------------------------

    def post(self, callback):
        """
        Queues work for the Tk main thread.

        This is the only safe way for the tray thread, a worker thread or a
        signal handler to reach the GUI: calling Tkinter from another thread
        blocks that thread until the main thread is back in its event loop. That
        used to freeze the GTK tray thread for as long as a tunnel took to start
        - and a tray menu popped open at that moment holds an X pointer grab
        while it waits, which makes the whole desktop look stuck.

        @param callback callable Called without arguments on the main thread.
        @return None
        """
        self.ui_calls.post(callback)

    def run_in_background(self, work, *args):
        """
        Runs blocking work off the Tk main thread. The callable must report back
        through post() rather than touching widgets itself.

        @param work callable Blocking function to run.
        @param args tuple Arguments for it.
        @return None
        """
        threading.Thread(target=work, args=args, daemon=True).start()

    def pump(self):
        """Runs queued cross-thread callbacks and applies the newest snapshot."""
        self.ui_calls.drain(should_stop=lambda: self.exiting)
        if self.exiting:
            return
        self.apply_status()
        self.root.after(UI_QUEUE_INTERVAL_MS, self.pump)

    # -- tray -----------------------------------------------------------

    def start_tray_icon(self):
        tray = TrayIcon(
            icon_path=core.ICON_FILE,
            tooltip=core.CONFIG.get("app_name", core.DEFAULT_APP_NAME),
            on_show=self.request_show,
            on_exit=lambda: self.post(self.quit_application),
        )
        if tray.start():
            self.tray_icon = tray
        else:
            # No tray support (e.g. GTK3 bindings missing): closing the window must
            # actually quit, otherwise there would be no way to bring it back.
            self.root.protocol("WM_DELETE_WINDOW", self.quit_application)
            if self.start_hidden:
                # Same reason: without a tray icon, a hidden window is unreachable.
                self.show_from_tray()

    def hide_to_tray(self):
        if self.tray_icon is None:
            self.quit_application()
            return
        self.root.withdraw()

    def request_show(self):
        """
        Asks for the window from outside the Tk main thread - used by the tray
        thread and by run.py's SIGUSR1 handler when a second instance hands over.
        Returns immediately, so neither the tray's GTK loop nor a signal handler
        ever waits on the GUI.
        """
        self.post(self.show_from_tray)

    def show_from_tray(self):
        if self.exiting:
            return
        self.root.deiconify()
        # Let the window map before focusing it; focusing an unmapped window is
        # what left an empty frame behind on some window managers.
        self.root.update_idletasks()
        self.root.lift()
        self.root.focus_force()

    def quit_application(self):
        if self.exiting:
            return
        self.exiting = True
        self.sampler.stop()
        self.root.destroy()

    # -- tunnel CRUD --------------------------------------------------------

    def selected_tunnel_id(self):
        selection = self.tree.selection()
        return selection[0] if selection else None

    def on_select(self, _event=None):
        tunnel_id = self.selected_tunnel_id()
        tunnel = core.find_tunnel(tunnel_id) if tunnel_id else None
        state = "normal" if tunnel else "disabled"
        self.edit_button.configure(state=state)
        self.remove_button.configure(state=state)
        self.render_detail(tunnel)

    def render_detail(self, tunnel):
        """
        Redraws the endpoint/hop badges above the list for the given tunnel.

        @param tunnel dict|None The selected tunnel, or None to show a placeholder.
        @return None
        """
        for child in self.detail_content.winfo_children():
            child.destroy()

        if tunnel is None:
            ttk.Label(
                self.detail_content,
                text="Select a tunnel to see where it connects.",
                foreground=MUTED_FG,
            ).pack(side="left")
            return

        badge_font = tkfont.Font(size=10, weight="bold")
        arrow_font = tkfont.Font(size=12)
        hop_font = tkfont.Font(size=10)

        def badge(text):
            tk.Label(
                self.detail_content,
                text=text,
                font=badge_font,
                bg=ACCENT,
                fg="#fafafa",
                padx=10,
                pady=5,
            ).pack(side="left")

        def arrow():
            tk.Label(self.detail_content, text=" → ", font=arrow_font, bg=BG, fg=FOCUS).pack(side="left")

        def hop(text):
            tk.Label(self.detail_content, text=text, font=hop_font, bg=BG, fg=MUTED_FG).pack(side="left")

        badge(f"127.0.0.1:{tunnel['local_port']}")
        arrow()
        hop(f"via {tunnel['ssh_user']}@{tunnel['ssh_host']}")
        arrow()
        badge(f"{tunnel['remote_host']}:{tunnel['remote_port']}")

    def open_add_dialog(self):
        TunnelDialog(self.root, on_saved=self.refresh_tree, post=self.post)

    def open_edit_dialog(self):
        tunnel_id = self.selected_tunnel_id()
        tunnel = core.find_tunnel(tunnel_id) if tunnel_id else None
        if tunnel is None:
            return
        TunnelDialog(self.root, on_saved=self.refresh_tree, post=self.post, tunnel=tunnel)

    def remove_selected(self):
        tunnel_id = self.selected_tunnel_id()
        tunnel = core.find_tunnel(tunnel_id) if tunnel_id else None
        if tunnel is None:
            return
        if not messagebox.askyesno(
            "Remove Tunnel",
            f"Remove tunnel \"{tunnel['name']}\"? This will disconnect it if currently connected.",
            parent=self.root,
        ):
            return
        self.set_message(f"Removing \"{tunnel['name']}\" ...", COLOR_PENDING)
        self.run_in_background(self.do_remove, tunnel)

    def do_remove(self, tunnel):
        """
        Removes a tunnel, disconnecting it first. Runs in a worker thread,
        because stopping a tunnel waits for the ssh process to go away.

        @param tunnel dict The tunnel to remove.
        @return None
        """
        core.remove_tunnel(tunnel["id"])
        self.post(lambda: self.finish_remove(tunnel))

    def finish_remove(self, tunnel):
        """Applies a finished removal on the Tk main thread."""
        self.sampler.forget(tunnel["id"])
        self.set_message(f"Removed \"{tunnel['name']}\".")
        self.refresh_tree()

    # -- list + status ------------------------------------------------------

    def connection_text(self, tunnel):
        return (
            f"127.0.0.1:{tunnel['local_port']}  →  {tunnel['ssh_user']}@{tunnel['ssh_host']}"
            f"  →  {tunnel['remote_host']}:{tunnel['remote_port']}"
        )

    def refresh_tree(self):
        selected = self.selected_tunnel_id()
        self.tree.delete(*self.tree.get_children())
        for tunnel in core.CONFIG["tunnels"]:
            self.tree.insert(
                "",
                "end",
                iid=tunnel["id"],
                text=tunnel["name"],
                values=(self.connection_text(tunnel), "", "–"),
            )
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
        self.on_select()
        self.render_status()
        self.sampler.request_sample()

    def apply_status(self):
        """Takes the newest snapshot from the sampler thread, if there is one."""
        try:
            self.last_status = self.status_snapshots.popleft()
        except IndexError:
            return
        self.render_status()

    def render_status(self):
        """
        Writes the last known snapshot into the tree. Rows whose tunnel is
        mid connect/disconnect are skipped so their pending text stays put.
        """
        for tunnel_id, (status_text, tag, traffic) in self.last_status.items():
            if tunnel_id in self.busy_ids or not self.tree.exists(tunnel_id):
                continue
            self.tree.set(tunnel_id, "status", status_text)
            self.tree.set(tunnel_id, "traffic", traffic)
            self.tree.item(tunnel_id, tags=(tag,))

    def set_message(self, text, color=COLOR_OFF):
        self.message_var.set(text)
        self.message_label.configure(foreground=color)

    # -- connect / disconnect ------------------------------------------------

    def on_row_double_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region not in ("tree", "cell"):
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        self.toggle_tunnel(row_id)

    def toggle_tunnel(self, tunnel_id):
        if tunnel_id in self.busy_ids:
            return
        tunnel = core.find_tunnel(tunnel_id)
        if tunnel is None:
            return

        self.busy_ids.add(tunnel_id)
        if core.is_tunnel_running(tunnel_id):
            self.tree.set(tunnel_id, "status", "● Disconnecting ...")
            self.tree.item(tunnel_id, tags=("pending",))
            self.set_message(f"Disconnecting \"{tunnel['name']}\" ...", COLOR_PENDING)
            self.run_in_background(self.do_stop, tunnel)
        else:
            self.tree.set(tunnel_id, "status", "● Connecting ...")
            self.tree.item(tunnel_id, tags=("pending",))
            self.set_message(f"Establishing SSH connection for \"{tunnel['name']}\" ...", COLOR_PENDING)
            self.run_in_background(self.do_start, tunnel)

    def do_start(self, tunnel):
        """
        Starts a tunnel. Runs in a worker thread (core.start_tunnel waits several
        seconds for ssh to come up), so it must not touch Tkinter - the result
        goes back through post().

        @param tunnel dict The tunnel to start.
        @return None
        """
        ok, msg = core.start_tunnel(tunnel)
        if ok:
            text = f"{tunnel['name']}: {msg}"
            color = COLOR_OK if core.port_is_listening(tunnel["local_port"]) else COLOR_PENDING
        else:
            text = f"{tunnel['name']} failed: {msg}"
            color = COLOR_ERROR
        self.post(lambda: self.finish_action(tunnel["id"], text, color))

    def do_stop(self, tunnel):
        """
        Stops a tunnel. Runs in a worker thread for the same reason as do_start:
        core.stop_tunnel waits for the ssh process to actually exit.

        @param tunnel dict The tunnel to stop.
        @return None
        """
        ok, msg = core.stop_tunnel(tunnel["id"])
        self.post(lambda: self.finish_action(tunnel["id"], f"{tunnel['name']}: {msg}", COLOR_OK if ok else COLOR_ERROR))

    def finish_action(self, tunnel_id, message, color):
        """
        Applies a finished start/stop on the Tk main thread.

        @param tunnel_id str The tunnel that is no longer busy.
        @param message str Result message for the status bar.
        @param color str Colour for that message.
        @return None
        """
        self.busy_ids.discard(tunnel_id)
        self.set_message(message, color)
        self.sampler.request_sample()


def main():
    root = tk.Tk()
    TunnelApp(root)
    root.mainloop()
