#!/usr/bin/env python3
"""
Main entry point for the SSH tunnel toggle (local port forwarding).

Without an argument, starts the GUI. With a CLI command, run.py behaves like
a terminal tool, e.g. for scripts or sessions without a display. Tunnels are
defined in tunnels.json in the same directory (see tunnels.json.example);
each tunnel can be referenced by its "name" (case-insensitive) or its "id".

Usage:
    run.py                          # start the GUI
    run.py --tray                   # start hidden, only in the system tray (autostart)
    run.py list                     # list all defined tunnels with status
    run.py status [name]            # show status of one tunnel, or all
    run.py start|stop|toggle <name> # act on one tunnel by name
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ssh_tunnel import core

CLI_ACTIONS = {"start", "stop", "toggle"}
TRAY_FLAG = "--tray"


def describe(tunnel):
    running = core.is_tunnel_running(tunnel["id"])
    if running:
        state = "ACTIVE" if core.port_is_listening(tunnel["local_port"]) else "STARTED (connection not yet confirmed)"
    else:
        state = "INACTIVE"
    return (
        f"{tunnel['name']}: {state} - localhost:{tunnel['local_port']} "
        f"-> {tunnel['ssh_host']} -> {tunnel['remote_host']}:{tunnel['remote_port']}"
    )


def find_tunnel_or_exit(name):
    tunnel = core.find_tunnel(name)
    if tunnel is None:
        print(f"No tunnel named \"{name}\". Run 'run.py list' to see defined tunnels.")
        sys.exit(1)
    return tunnel


def run_list():
    if not core.CONFIG["tunnels"]:
        print("No tunnels defined yet. Use the GUI's \"Add\" button to create one.")
        return
    for tunnel in core.CONFIG["tunnels"]:
        print(describe(tunnel))


def run_status(name):
    if name is None:
        run_list()
        return
    print(describe(find_tunnel_or_exit(name)))


def run_cli(action, name):
    if name is None:
        print(f"Usage: run.py {action} <name>")
        sys.exit(1)
    tunnel = find_tunnel_or_exit(name)

    if action == "start":
        ok, msg = core.start_tunnel(tunnel)
        print(msg)
        sys.exit(0 if ok else 1)
    elif action == "stop":
        ok, msg = core.stop_tunnel(tunnel["id"])
        print(msg)
    elif action == "toggle":
        if core.is_tunnel_running(tunnel["id"]):
            ok, msg = core.stop_tunnel(tunnel["id"])
        else:
            ok, msg = core.start_tunnel(tunnel)
        print(msg)
        sys.exit(0 if ok else 1)


def run_gui(start_hidden=False):
    """
    Starts the GUI, or hands over to an already running instance.

    @param start_hidden bool True to start without showing the window (tray only),
        as used by the autostart entry.
    @return None
    """
    import signal
    import tkinter as tk

    from ssh_tunnel.gui import TunnelApp

    other_pid = core.running_app_pid()
    if other_pid is not None:
        if start_hidden:
            # Autostart run: an instance is already there, so don't pop its window open.
            print(f"SSH Tunnel is already running (PID {other_pid}); nothing to do.")
            return
        os.kill(other_pid, signal.SIGUSR1)
        print(f"SSH Tunnel is already running (PID {other_pid}); bringing that window to front.")
        return

    core.acquire_app_lock()
    try:
        root = tk.Tk()
        app = TunnelApp(root, start_hidden=start_hidden)
        # request_show() only appends to a lock-free queue: safe to call from a
        # signal handler, which may interrupt the main thread anywhere.
        signal.signal(signal.SIGUSR1, lambda *_args: app.request_show())
        root.mainloop()
    finally:
        core.release_app_lock()


def main():
    args = sys.argv[1:]
    start_hidden = TRAY_FLAG in args
    if start_hidden:
        args = [arg for arg in args if arg != TRAY_FLAG]
        if args:
            print(f"{TRAY_FLAG} applies to the GUI and cannot be combined with a CLI command.")
            sys.exit(1)

    action = args[0] if args else None
    name = args[1] if len(args) > 1 else None

    if action is None:
        run_gui(start_hidden)
    elif action == "list":
        run_list()
    elif action == "status":
        run_status(name)
    elif action in CLI_ACTIONS:
        run_cli(action, name)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
