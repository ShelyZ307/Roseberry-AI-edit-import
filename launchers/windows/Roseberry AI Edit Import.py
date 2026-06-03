#!/usr/bin/env python3
"""
Stable Windows DaVinci Resolve launcher for Roseberry AI Edit Import.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path


APP_FOLDER_NAME = "roseberry_ai_edit_import"
APPDATA = Path(os.environ.get("APPDATA", str(Path.home())))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
USERPROFILE = Path(os.environ.get("USERPROFILE", str(Path.home())))
CONFIG_PATH = APPDATA / "Roseberry AI Tools" / "config.json"
DEFAULT_APP_HOME = LOCALAPPDATA / "Roseberry" / "AI Edit Import"
LOG_PATH = USERPROFILE / "Desktop" / "roseberry_ai_tools_desktop_launcher_debug.txt"


def log(message: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write("[{}] {}\n".format(datetime.now().astimezone().isoformat(timespec="seconds"), message))
    except Exception:
        pass


def show_message(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, str(message), str(title), 0)
    except Exception:
        pass


def configured_app_home() -> Path:
    env_home = os.environ.get("ROSEBERRY_AI_EDIT_IMPORT_HOME")
    if env_home:
        return Path(env_home).expanduser()
    try:
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            configured = data.get("app_home")
            if configured:
                return Path(str(configured)).expanduser()
    except Exception as exc:
        log("Could not read config: {}".format(exc))
    return DEFAULT_APP_HOME


def app_candidates(launcher_path: Path) -> list[Path]:
    launcher_dir = launcher_path.parent
    app_home = configured_app_home()
    return [
        app_home / APP_FOLDER_NAME,
        app_home,
        launcher_dir / APP_FOLDER_NAME,
        launcher_dir.parent / APP_FOLDER_NAME,
    ]


def choose_python(app_dir: Path) -> Path:
    venv_python = app_dir / "roseberry_ai_tools_app" / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return venv_python
    direct_venv_python = app_dir / ".venv" / "Scripts" / "python.exe"
    if direct_venv_python.exists():
        return direct_venv_python
    return Path(sys.executable or "python")


def main() -> None:
    LOG_PATH.write_text("", encoding="utf-8")
    launcher_path = Path(str(globals().get("__file__", ""))).expanduser().resolve() if globals().get("__file__") else CONFIG_PATH
    candidates = app_candidates(launcher_path)
    app_dir = next((candidate for candidate in candidates if (candidate / "roseberry_ai_tools_app" / "run_desktop_app.py").exists()), candidates[0])
    runner = app_dir / "roseberry_ai_tools_app" / "run_desktop_app.py"
    python_exe = choose_python(app_dir)
    command = [str(python_exe), str(runner)]

    log("Roseberry AI Edit Import Windows launcher starting.")
    log("Launcher path: {}".format(launcher_path))
    log("Config path: {}".format(CONFIG_PATH))
    log("App candidates: {}".format(" | ".join(str(value) for value in candidates)))
    log("Resolved app root: {}".format(app_dir))
    log("Runner exists: {}".format(runner.exists()))
    log("Python executable: {}".format(python_exe))
    log("Command: {}".format(" ".join(shlex.quote(part) for part in command)))

    if not runner.exists():
        raise RuntimeError("Desktop app runner not found: {}".format(runner))

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    subprocess.Popen(
        command,
        cwd=str(app_dir / "roseberry_ai_tools_app"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("Fatal launcher error: {}".format(exc))
        log(traceback.format_exc())
        show_message(
            "Roseberry AI Edit Import",
            "Could not launch Roseberry AI Edit Import.\n\n{}\n\nLog:\n{}".format(exc, LOG_PATH),
        )

