"""AI_Civ_Web.exe: one double-click starts the whole thing for the local network.

* the database and Redis containers (``civ-pg``, ``civ-redis``) are started when they are stopped;
* the backend (FastAPI) and the built front-end (``web/dist``, served by the same process) listen on 0.0.0.0:<port>, default 8000;
* the docker service names of ``.env`` (``postgres``, ``redis``, ``proxy``) are mapped to 127.0.0.1 for THIS process only; ``.env`` is not touched;
* the console prints the addresses to open on the phone (same Wi-Fi) and opens the page on this PC.

Built by ``scripts/build_exe.ps1`` (PyInstaller). The exe is a thin launcher: it needs the project folder (``.venv``, ``src``, ``.env``) next to it.
Usage: ``AI_Civ_Web.exe [port]``. Stop: Ctrl+C or close the window.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8000
FALLBACK_ROOT = Path(r"D:\softs\AI_Civilization")
CONTAINERS = ("civ-pg", "civ-redis")


def project_root() -> Path:
    here = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    for candidate in (here, here.parent, FALLBACK_ROOT):
        if (candidate / "src" / "ai_civ").is_dir() and (candidate / ".venv").is_dir():
            return candidate
    sys.exit(f"Не нашёл проект (папку с src\\ai_civ и .venv) рядом с {here} и в {FALLBACK_ROOT}.")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z][A-Z0-9_]*)=(.*)", line)
            if m:
                values[m.group(1)] = m.group(2).split(" #")[0].strip().strip("'\"")
    return values


def localise(value: str) -> str:
    """Docker service names -> this machine (their ports are published by the containers)."""
    return value.replace("@postgres:", "@127.0.0.1:").replace("//redis:", "//127.0.0.1:")


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # no packet is sent: the OS only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def start_containers() -> None:
    for name in CONTAINERS:
        try:
            state = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            print("! Docker не отвечает: запустите Docker Desktop и повторите.", flush=True)
            return
        if state.returncode != 0:
            print(f"! Контейнера {name} нет (docker ps -a) — пропускаю.", flush=True)
        elif state.stdout.strip() != "true":
            print(f"→ запускаю контейнер {name}", flush=True)
            subprocess.run(["docker", "start", name], capture_output=True, timeout=60)


def ensure_frontend(root: Path) -> None:
    if (root / "web" / "dist" / "index.html").is_file():
        return
    print("→ web\\dist нет, собираю фронтенд (npm ci && npm run build)…", flush=True)
    web = root / "web"
    for cmd in (["npm.cmd", "ci"], ["npm.cmd", "run", "build"]):
        if subprocess.run(cmd, cwd=web).returncode != 0:
            print("! Не удалось собрать фронтенд: страница / покажет заглушку, мобильное API и /mobile-test работают.", flush=True)
            return


def open_when_up(port: int) -> None:
    for _ in range(90):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            break
        except OSError:
            time.sleep(1)
    else:
        return
    ip = lan_ip()
    print("\n" + "=" * 62, flush=True)
    print(f" Готово. На этом ПК:   http://127.0.0.1:{port}/mobile-test", flush=True)
    print(f" С телефона (Wi-Fi):   http://{ip}:{port}/mobile-test", flush=True)
    print(f" Mini App (фронт):     http://{ip}:{port}/   (в обычном браузере просит открыть из Telegram)", flush=True)
    print(" Остановить: Ctrl+C или закрыть окно.", flush=True)
    print("=" * 62 + "\n", flush=True)
    webbrowser.open(f"http://127.0.0.1:{port}/mobile-test")


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else DEFAULT_PORT
    root = project_root()
    os.chdir(root)
    dotenv = read_env(root / ".env")
    if "APP_JWT_SECRET" not in dotenv:
        print("! В .env нет APP_JWT_SECRET: мобильное API не будет подключено.", flush=True)
    env = dict(os.environ)
    for key in ("DATABASE_URL", "CELERY_BROKER_URL", "RATE_LIMIT_REDIS_URL", "REDIS_URL"):
        if key in dotenv:
            env[key] = localise(dotenv[key])
    env.update(GEMINI_PROXY="", TELEGRAM_PROXY="", WEB_HOST="0.0.0.0", WEB_PORT=str(port), PYTHONPATH=str(root / "src"), PYTHONUNBUFFERED="1")
    print(f"AI Civilization · проект: {root}", flush=True)
    start_containers()
    ensure_frontend(root)
    threading.Thread(target=open_when_up, args=(port,), daemon=True).start()
    child = subprocess.Popen([str(root / ".venv" / "Scripts" / "python.exe"), "-m", "ai_civ.web"], cwd=root, env=env)
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
        return 0


if __name__ == "__main__":
    code = main()
    if code:
        input(f"\nСервер остановился с кодом {code}. Enter — закрыть окно.")
    sys.exit(code)
