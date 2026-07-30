"""
Tkinter GUI for managing and toggling SSH tunnels (local port forwarding).
"""

import os
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from ssh_tunnel import core
from ssh_tunnel.tray import TrayIcon

POLL_INTERVAL_MS = 2000


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


class TunnelDialog(tk.Toplevel):
    """Dialog to add/edit a single tunnel definition and test it before saving."""

    def __init__(self, parent, on_saved, tunnel=None):
        super().__init__(parent)
        self.on_saved = on_saved
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
        self.update_idletasks()

        ok, message = core.test_connection(values["ssh_host"], values["ssh_user"], values["remote_host"], remote_port)
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
        self.traffic_prev = {}
        self.tray_icon = None
        self.exiting = False

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
        self.root.after(POLL_INTERVAL_MS, self.poll)
        self.root.after_idle(self.start_tray_icon)

    # -- tray -----------------------------------------------------------

    def start_tray_icon(self):
        tray = TrayIcon(
            icon_path=core.ICON_FILE,
            tooltip=core.CONFIG.get("app_name", core.DEFAULT_APP_NAME),
            on_show=lambda: self.root.after(0, self.show_from_tray),
            on_exit=lambda: self.root.after(0, self.quit_application),
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

    def show_from_tray(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit_application(self):
        if self.exiting:
            return
        self.exiting = True
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
        TunnelDialog(self.root, on_saved=self.refresh_tree)

    def open_edit_dialog(self):
        tunnel_id = self.selected_tunnel_id()
        tunnel = core.find_tunnel(tunnel_id) if tunnel_id else None
        if tunnel is None:
            return
        TunnelDialog(self.root, on_saved=self.refresh_tree, tunnel=tunnel)

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
        core.remove_tunnel(tunnel_id)
        self.traffic_prev.pop(tunnel_id, None)
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
        self.refresh_status()

    def refresh_status(self):
        now = time.time()
        for tunnel in core.CONFIG["tunnels"]:
            tunnel_id = tunnel["id"]
            if not self.tree.exists(tunnel_id):
                continue
            if tunnel_id in self.busy_ids:
                continue
            pid_file, _ = core.tunnel_paths(tunnel_id)
            pid = core.read_pid(pid_file)
            running = core.is_running(pid)
            if running:
                listening = core.port_is_listening(tunnel["local_port"])
                if listening:
                    status_text, tag = "● Connected", "connected"
                else:
                    status_text, tag = "● Connecting ...", "pending"
            else:
                status_text, tag = "● Disconnected", "disconnected"
            self.tree.set(tunnel_id, "status", status_text)
            self.tree.set(tunnel_id, "traffic", self.traffic_text(tunnel_id, pid, running, now))
            self.tree.item(tunnel_id, tags=(tag,))

    def traffic_text(self, tunnel_id, pid, running, now):
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
            self.traffic_prev.pop(tunnel_id, None)
            return "–"

        counters = core.read_io_counters(pid)
        if counters is None:
            return "–"
        rchar, wchar = counters

        prev = self.traffic_prev.get(tunnel_id)
        self.traffic_prev[tunnel_id] = (pid, now, rchar, wchar)
        if prev is None or prev[0] != pid:
            return "–"

        _, prev_time, prev_rchar, prev_wchar = prev
        elapsed = now - prev_time
        if elapsed <= 0:
            return "–"
        rx_rate = max(0, (rchar - prev_rchar) / elapsed)
        tx_rate = max(0, (wchar - prev_wchar) / elapsed)
        return f"↓ {format_rate(rx_rate)}  ↑ {format_rate(tx_rate)}"

    def poll(self):
        self.refresh_status()
        self.root.after(POLL_INTERVAL_MS, self.poll)

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
            self.root.after(50, self.do_stop, tunnel)
        else:
            self.tree.set(tunnel_id, "status", "● Connecting ...")
            self.tree.item(tunnel_id, tags=("pending",))
            self.set_message(f"Establishing SSH connection for \"{tunnel['name']}\" ...", COLOR_PENDING)
            self.root.after(50, self.do_start, tunnel)

    def do_start(self, tunnel):
        ok, msg = core.start_tunnel(tunnel)
        if ok:
            self.set_message(f"{tunnel['name']}: {msg}", COLOR_OK if core.port_is_listening(tunnel["local_port"]) else COLOR_PENDING)
        else:
            self.set_message(f"{tunnel['name']} failed: {msg}", COLOR_ERROR)
        self.finish_action(tunnel["id"])

    def do_stop(self, tunnel):
        ok, msg = core.stop_tunnel(tunnel["id"])
        self.set_message(f"{tunnel['name']}: {msg}", COLOR_OK if ok else COLOR_ERROR)
        self.finish_action(tunnel["id"])

    def finish_action(self, tunnel_id):
        self.busy_ids.discard(tunnel_id)
        self.refresh_status()


def main():
    root = tk.Tk()
    TunnelApp(root)
    root.mainloop()
