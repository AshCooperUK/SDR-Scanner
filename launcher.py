"""Install and control the SDR Scanner user service and desktop launcher."""

import argparse
import json
import os
import pwd
import shlex
import subprocess
import sys
from contextlib import closing
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import monotonic, sleep


ROOT = Path(__file__).resolve().parent
PYTHON = "/usr/bin/python3"
UNIT = "local-sdr-scanner.service"
PORT = 8766
URL = f"http://127.0.0.1:{PORT}/"
RUNNING = {"active", "activating", "reloading"}


def desktop_value(value):
    return (value.replace("\\", "\\\\").replace("\n", "\\n")
            .replace("\r", "\\r").replace("\t", "\\t"))


def desktop_arg(value):
    # The desktop-entry string parser unescapes before the Exec parser.
    escaped = "".join("\\" + char if char in '\"\x60$\\' else char for char in value)
    return '"' + desktop_value(escaped.replace("%", "%%")) + '"'


def desktop_session():
    if any(os.environ.get(name) for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return False
    if os.environ.get("XDG_SESSION_TYPE") == "tty":
        return False
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def systemd_arg(value):
    # ExecStart expands environment variables and systemd specifiers after unquoting.
    escaped = desktop_value(value).replace('"', '\\"').replace("$", "$$").replace("%", "%%")
    return f'"{escaped}"'


def xdg_home(variable, fallback):
    configured = Path(os.environ.get(variable, ""))
    return configured if configured.is_absolute() else Path.home() / fallback


def service_path():
    return xdg_home("XDG_CONFIG_HOME", ".config") / "systemd" / "user" / UNIT


def write_atomic(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(contents)
            output.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def install_desktop():
    entry = xdg_home("XDG_DATA_HOME", ".local/share") / "applications/sdr-scanner.desktop"
    launch = f"{PYTHON} {desktop_arg(str(ROOT / 'launcher.py'))}"
    icon = desktop_value(str(ROOT / "web/ashcooperuk-avatar.png"))
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SDR Scanner\n"
        "Comment=Listen to radio with a USB RTL-SDR receiver\n"
        f"Exec={launch} open\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;\n"
        "Actions=Stop;\n"
        "\n"
        "[Desktop Action Stop]\n"
        "Name=Stop SDR Scanner\n"
        f"Exec={launch} stop\n"
    )
    write_atomic(entry, contents)
    print(f"Installed {entry}")


def install(desktop=None):
    if desktop is None:
        desktop = desktop_session()
    if desktop:
        install_desktop()
    transient = unit_is_transient()
    unit = service_path()
    contents = (
        "[Unit]\n"
        "Description=SDR Scanner\n"
        "\n"
        "[Service]\n"
        "Type=exec\n"
        f"ExecStart={PYTHON} {systemd_arg(str(ROOT / 'app.py'))}\n"
        "Restart=on-failure\n"
        "RestartSec=2s\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    write_atomic(unit, contents)
    if transient:
        require_success(command("systemctl", "--user", "stop", UNIT))
    require_success(command("systemctl", "--user", "daemon-reload"))
    require_success(command("systemctl", "--user", "enable", "--now", UNIT))
    print(f"Installed and started {unit}")
    enable_boot_start()
    print("SDR Scanner is enabled at boot.")


def command(*args):
    return subprocess.run(args, capture_output=True, text=True)


def require_success(result):
    if result.returncode:
        message = result.stderr.strip() or f"{result.args[0]} exited with status {result.returncode}"
        raise RuntimeError(message)


def unit_state():
    result = command("systemctl", "--user", "is-active", UNIT)
    if result.returncode not in (0, 3, 4):
        require_success(result)
    return result.stdout.strip()


def unit_is_transient():
    result = command("systemctl", "--user", "show", "--property=Transient", "--value", UNIT)
    if result.returncode and unit_state() in RUNNING:
        require_success(result)
    return result.stdout.strip() == "yes"


def enable_boot_start():
    user = pwd.getpwuid(os.getuid()).pw_name
    try:
        result = command("loginctl", "show-user", user, "--property=Linger", "--value")
        if result.returncode == 0 and result.stdout.strip() == "yes":
            return
        require_success(command("loginctl", "--no-ask-password", "enable-linger", user))
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"The service is enabled for your user session. Run "
            f"`sudo loginctl enable-linger {shlex.quote(user)}` to start it before login. "
            f"{error}"
        ) from error


def panel_ready():
    try:
        with closing(HTTPConnection("127.0.0.1", PORT, timeout=0.5)) as connection:
            connection.request("GET", "/api/state")
            with connection.getresponse() as response:
                if response.status == 200:
                    state = json.loads(response.read(65536))
                    return isinstance(state, dict) and {"display", "receiver", "power"} <= state.keys()
    except (OSError, HTTPException, ValueError):
        pass
    return False


def open_panel():
    if not panel_ready() and unit_state() not in RUNNING:
        if service_path().is_file():
            started = command("systemctl", "--user", "start", UNIT)
        else:
            # systemd expands dollars in ExecStart arguments, even without a shell.
            script = str(ROOT / "app.py").replace("$", "$$")
            started = command(
                "systemd-run", "--user", "--quiet", "--collect", f"--unit={UNIT}",
                "--property=Type=exec", "--", PYTHON, script,
            )
        if started.returncode and unit_state() not in RUNNING:
            require_success(started)
    deadline = monotonic() + 10
    while not panel_ready():
        if monotonic() >= deadline:
            raise RuntimeError(f"Scanner did not start. Check journalctl --user -u {UNIT}.")
        sleep(0.1)
    if desktop_session():
        require_success(command("xdg-open", URL))
    else:
        print(URL)


def stop():
    state = unit_state()
    if state in RUNNING or state == "deactivating":
        require_success(command("systemctl", "--user", "stop", UNIT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    installation = actions.add_parser("install", help="Install the service and enable it at boot")
    desktop = installation.add_mutually_exclusive_group()
    desktop.add_argument("--headless", dest="desktop", action="store_false",
                         help="Skip the desktop menu icon")
    desktop.add_argument("--desktop", dest="desktop", action="store_true",
                         help="Install the desktop menu icon even from a headless session")
    installation.set_defaults(desktop=None)
    actions.add_parser("open", help="Start the service and open or print the panel URL")
    actions.add_parser("stop", help="Stop the service for the current boot")
    args = parser.parse_args()
    try:
        if args.action == "install":
            install(args.desktop)
        elif args.action == "open":
            open_panel()
        else:
            stop()
    except (OSError, RuntimeError) as error:
        print(f"SDR Scanner: {error}", file=sys.stderr)
        if args.action != "install" and desktop_session():
            try:
                command("notify-send", "--", "SDR Scanner", str(error))
            except FileNotFoundError:
                pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
