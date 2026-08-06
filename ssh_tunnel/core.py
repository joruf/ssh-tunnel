"""
Shared logic for SSH tunnels (local port forwarding).
Used by run.py (CLI and GUI). Tunnel definitions and the app's display name
live in tunnels.json in the project root. An older single-tunnel .env is
migrated into it automatically the first time this runs.
"""

import json
import os
import signal
import socket
import subprocess
import time
import uuid

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(APP_ROOT, "assets")
ICON_FILE = os.path.join(ASSETS_DIR, "icon.png")
CONFIG_FILE = os.path.join(APP_ROOT, "tunnels.json")
LEGACY_ENV_FILE = os.path.join(APP_ROOT, ".env")

DEFAULT_APP_NAME = "SSH Tunnel"

RUN_DIR = os.path.expanduser("~/.cache/ssh-tunnel")
APP_LOCK_FILE = os.path.join(RUN_DIR, "app.pid")

# /proc lets a PID be cross-checked against the process it is supposed to name.
HAVE_PROC = os.path.isdir("/proc/self")

TUNNEL_FIELDS = ["name", "ssh_host", "ssh_user", "local_port", "remote_host", "remote_port"]

# How long start_tunnel() waits for ssh to either forward the port or give up.
# Generous on purpose: a hostname that does not resolve can keep ssh busy for
# some 15 seconds, and stopping to wait before that verdict is in means
# reporting "started" for a tunnel that is about to die - the failure then goes
# unreported and the entry just falls back to disconnected on its own.
START_TIMEOUT_SECONDS = 30
START_POLL_SECONDS = 0.25


def load_env(path):
    """Loads KEY=VALUE pairs from a .env file into a dict, ignoring blank/comment lines."""
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def new_tunnel_id():
    return uuid.uuid4().hex[:8]


def _migrate_legacy_env():
    """
    One-time migration from the old single-tunnel .env format to tunnels.json.

    @return dict|None The migrated config, or None if there was no .env to migrate.
    """
    env = load_env(LEGACY_ENV_FILE)
    if not env:
        return None

    name = env.get("APP_NAME", DEFAULT_APP_NAME)
    config = {
        "app_name": name,
        "tunnels": [
            {
                "id": new_tunnel_id(),
                "name": name,
                "ssh_host": env["SSH_HOST"],
                "ssh_user": env["SSH_USER"],
                "local_port": int(env["LOCAL_PORT"]),
                "remote_host": env["REMOTE_HOST"],
                "remote_port": int(env["REMOTE_PORT"]),
            }
        ],
    }
    save_config(config)

    migrated_path = LEGACY_ENV_FILE + ".migrated"
    if not os.path.exists(migrated_path):
        os.rename(LEGACY_ENV_FILE, migrated_path)
    return config


def load_config():
    """Loads tunnels.json, migrating a legacy .env on first run if present."""
    if os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            config = json.load(f)
        config.setdefault("app_name", DEFAULT_APP_NAME)
        config.setdefault("tunnels", [])
        return config

    migrated = _migrate_legacy_env()
    if migrated is not None:
        return migrated

    return {"app_name": DEFAULT_APP_NAME, "tunnels": []}


def save_config(config):
    """Writes the config dict to tunnels.json."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


CONFIG = load_config()


def find_tunnel(identifier):
    """
    Looks up a tunnel by id or by display name (case-insensitive).

    @param identifier str Tunnel id or name.
    @return dict|None The matching tunnel, or None.
    """
    for tunnel in CONFIG["tunnels"]:
        if tunnel["id"] == identifier or tunnel["name"].lower() == identifier.lower():
            return tunnel
    return None


def add_tunnel(data):
    """
    Adds a new tunnel entry and persists the config.

    @param data dict Values for TUNNEL_FIELDS.
    @return dict The stored tunnel, including its generated id.
    """
    tunnel = {"id": new_tunnel_id(), **{key: data[key] for key in TUNNEL_FIELDS}}
    CONFIG["tunnels"].append(tunnel)
    save_config(CONFIG)
    return tunnel


def update_tunnel(tunnel_id, data):
    """Updates an existing tunnel's fields (keeping its id) and persists the config."""
    tunnel = find_tunnel(tunnel_id)
    if tunnel is None:
        return None
    for key in TUNNEL_FIELDS:
        tunnel[key] = data[key]
    save_config(CONFIG)
    return tunnel


def remove_tunnel(tunnel_id):
    """Stops the tunnel if running, removes it from the config and persists."""
    stop_tunnel(tunnel_id)
    CONFIG["tunnels"] = [t for t in CONFIG["tunnels"] if t["id"] != tunnel_id]
    save_config(CONFIG)


def tunnel_paths(tunnel_id):
    return (
        os.path.join(RUN_DIR, f"{tunnel_id}.pid"),
        os.path.join(RUN_DIR, f"{tunnel_id}.log"),
    )


def read_pid(pid_file):
    """Returns the stored PID if present and readable, otherwise None."""
    if not os.path.isfile(pid_file):
        return None
    try:
        with open(pid_file) as f:
            return int(f.read().strip())
    except (ValueError, OSError):
        return None


def is_running(pid):
    """Checks whether a process with the given PID is alive."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def forward_spec(tunnel):
    """The ssh -L argument that identifies this tunnel's process."""
    return f"{tunnel['local_port']}:{tunnel['remote_host']}:{tunnel['remote_port']}"


def read_cmdline(pid):
    """
    Reads a process's argument vector from /proc.

    @param pid int Process id.
    @return list[str]|None The arguments, or None when they cannot be read
        (process already gone, or no /proc on this platform).
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return [arg for arg in raw.decode(errors="replace").split("\0") if arg] or None


def process_is_tunnel(pid, tunnel):
    """
    Confirms a PID really is this tunnel's ssh process, not just some live PID.

    PIDs get reused, and a pid file can outlive the process it names: an ssh
    that only dies after start_tunnel() stopped waiting for it (a slow DNS
    failure, say) leaves one behind. Trusting such a PID would show the tunnel
    as connected - and disconnecting it would send SIGTERM to whatever
    unrelated process has meanwhile inherited that number.

    @param pid int|None PID from the tunnel's pid file.
    @param tunnel dict The tunnel it is supposed to belong to.
    @return bool True when that process exists and is this tunnel's forward.
    """
    if not is_running(pid):
        return False
    if not HAVE_PROC:
        # Nothing to cross-check against here, so liveness has to do.
        return True
    cmdline = read_cmdline(pid)
    if cmdline is None:
        return False
    return os.path.basename(cmdline[0]).endswith("ssh") and forward_spec(tunnel) in cmdline


def is_tunnel_running(tunnel_id):
    """
    Whether this tunnel's ssh process is up.

    @param tunnel_id str Tunnel id (or name).
    @return bool True when the tunnel is running.
    """
    pid_file, _ = tunnel_paths(tunnel_id)
    pid = read_pid(pid_file)
    tunnel = find_tunnel(tunnel_id)
    if tunnel is None:
        # Not a configured tunnel (e.g. one being removed): liveness is all
        # there is to go on.
        return is_running(pid)
    return process_is_tunnel(pid, tunnel)


def running_app_pid():
    """
    Returns the PID of an already-running GUI instance, or None.

    @return int|None PID of the other instance, if one is alive.
    """
    pid = read_pid(APP_LOCK_FILE)
    return pid if is_running(pid) else None


def acquire_app_lock():
    """Writes this process's PID to the app lock file. Call only after confirming
    no other instance is running (see running_app_pid())."""
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(APP_LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def release_app_lock():
    """Removes the app lock file, but only if it still points at this process."""
    if read_pid(APP_LOCK_FILE) == os.getpid():
        try:
            os.remove(APP_LOCK_FILE)
        except OSError:
            pass


def port_is_listening(local_port):
    """Confirms the local forwarded port actually accepts connections."""
    try:
        with socket.create_connection(("127.0.0.1", local_port), timeout=1):
            return True
    except OSError:
        return False


def read_io_counters(pid):
    """
    Reads cumulative bytes read/written by a process from /proc/<pid>/io.
    Used to derive a throughput rate by sampling this twice over time.

    @param pid int Process id.
    @return tuple[int, int]|None (bytes_read, bytes_written), or None if unavailable
    (process gone, or /proc/[pid]/io unsupported on this platform).
    """
    try:
        with open(f"/proc/{pid}/io") as f:
            values = {}
            for line in f:
                key, _, value = line.partition(":")
                values[key.strip()] = int(value.strip())
    except (OSError, ValueError):
        return None
    return values.get("rchar", 0), values.get("wchar", 0)


def tail_log(tunnel_id, lines=5):
    _, log_file = tunnel_paths(tunnel_id)
    if not os.path.isfile(log_file):
        return ""
    with open(log_file, errors="replace") as f:
        content = f.readlines()
    return "".join(content[-lines:]).strip()


def start_tunnel(tunnel):
    """Starts the given tunnel. Returns (success: bool, message: str)."""
    pid_file, log_file = tunnel_paths(tunnel["id"])
    pid = read_pid(pid_file)
    if process_is_tunnel(pid, tunnel):
        return True, f"Already running (PID {pid})."

    os.makedirs(RUN_DIR, exist_ok=True)
    # Clear the log so tail_log() reflects only this attempt.
    open(log_file, "w").close()
    log = open(log_file, "a")
    forward = f"{tunnel['local_port']}:{tunnel['remote_host']}:{tunnel['remote_port']}"
    proc = subprocess.Popen(
        ["ssh", "-N", "-L", forward, f"{tunnel['ssh_user']}@{tunnel['ssh_host']}"],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    with open(pid_file, "w") as f:
        f.write(str(proc.pid))

    # Wait for SSH to either establish the tunnel or fail (auth error, unknown
    # host, ...). Uses proc.poll() rather than is_running(): for our own child,
    # is_running()'s os.kill(pid, 0) still succeeds while the process is a
    # not-yet-reaped zombie, which would misreport a failure as still starting.
    for _ in range(int(START_TIMEOUT_SECONDS / START_POLL_SECONDS)):
        time.sleep(START_POLL_SECONDS)
        if proc.poll() is not None:
            break
        if port_is_listening(tunnel["local_port"]):
            return True, f"Connected (PID {proc.pid})."

    if proc.poll() is None:
        # Process alive but port not confirmed yet - treat as started, GUI will keep polling.
        return True, f"Started (PID {proc.pid}), waiting for connection ..."

    if os.path.isfile(pid_file):
        os.remove(pid_file)
    return False, tail_log(tunnel["id"]) or "SSH process exited immediately (see log)."


def stop_tunnel(tunnel_id):
    """Stops the given tunnel. Returns (success: bool, message: str)."""
    pid_file, _ = tunnel_paths(tunnel_id)
    pid = read_pid(pid_file)
    # is_tunnel_running() rather than is_running(): a pid file left behind by a
    # dead tunnel must never get an unrelated process killed (process_is_tunnel).
    if not is_tunnel_running(tunnel_id):
        if os.path.isfile(pid_file):
            os.remove(pid_file)
        return True, "Was not active."

    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        if not is_running(pid):
            break
        time.sleep(0.2)

    if is_running(pid):
        os.kill(pid, signal.SIGKILL)

    if os.path.isfile(pid_file):
        os.remove(pid_file)
    return True, f"Disconnected (PID {pid})."


def test_connection(host, user, remote_host, remote_port, timeout=5):
    """
    Checks SSH reachability/auth for (user, host) and, over that same SSH
    connection, whether remote_host:remote_port is reachable from the server.
    Does not touch the actual tunnel (PID/log files untouched).

    @param host str SSH server to connect to.
    @param user str SSH user.
    @param remote_host str Host to probe from the SSH server's point of view.
    @param remote_port int Port to probe on remote_host.
    @param timeout int Seconds to wait for the SSH connection.
    @return tuple[bool, str] (success, human-readable message).
    """
    remote_check = f"timeout 5 bash -c '</dev/tcp/{remote_host}/{remote_port}' && echo TARGET_OK || echo TARGET_UNREACHABLE"
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", f"ConnectTimeout={timeout}",
                "-o", "StrictHostKeyChecking=accept-new",
                f"{user}@{host}",
                remote_check,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        return False, "Connection timed out."
    except OSError as e:
        return False, str(e)

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "SSH connection failed.").strip()
        return False, f"SSH connection failed: {error}"

    if "TARGET_OK" in result.stdout:
        return True, f"SSH OK, {remote_host}:{remote_port} is reachable from {host}."
    return False, f"SSH OK, but {remote_host}:{remote_port} is not reachable from {host}."
