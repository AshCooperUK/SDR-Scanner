"""Install and control the SDR Scanner applications-menu launcher."""

import argparse
import json
import os
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


def install():
    data_home = Path(os.environ.get("XDG_DATA_HOME", ""))
    if not data_home.is_absolute():
        data_home = Path.home() / ".local/share"
    entry = data_home / "applications/sdr-scanner.desktop"
    entry.parent.mkdir(parents=True, exist_ok=True)
    command = f"{PYTHON} {desktop_arg(str(ROOT / 'launcher.py'))}"
    icon = desktop_value(str(ROOT / "web/panel.png"))
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=SDR Scanner\n"
        "Comment=Listen to radio with a USB RTL-SDR receiver\n"
        f"Exec={command} open\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;\n"
        "Actions=Stop;\n"
        "\n"
        "[Desktop Action Stop]\n"
        "Name=Stop SDR Scanner\n"
        f"Exec={command} stop\n"
    )
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=entry.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(contents)
            output.flush()
            temporary.replace(entry)
        finally:
            temporary.unlink(missing_ok=True)
    print(f"Installed {entry}")


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
    require_success(command("xdg-open", URL))


def stop():
    state = unit_state()
    if state in RUNNING or state == "deactivating":
        require_success(command("systemctl", "--user", "stop", UNIT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "open", "stop"))
    action = parser.parse_args().action
    try:
        {"install": install, "open": open_panel, "stop": stop}[action]()
    except (OSError, RuntimeError) as error:
        print(f"SDR Scanner: {error}", file=sys.stderr)
        if action != "install":
            try:
                command("notify-send", "--", "SDR Scanner", str(error))
            except FileNotFoundError:
                pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
