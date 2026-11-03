"""One-command bootstrap. Uses only the Python standard library before setup."""

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
import uuid
import venv
import zipfile
from pathlib import Path

# Windows terminals and CI runners can default to a non-Cyrillic encoding.
for console in (sys.stdout, sys.stderr):
    if hasattr(console, "reconfigure"):
        console.reconfigure(encoding="utf-8", errors="replace")
os.environ["PYTHONUTF8"] = "1"

ROOT = Path(__file__).resolve().parent


def install_voice_model():
    destination = ROOT / ".local/models/vosk-model-small-ru-0.22"
    if (destination / ".ready").is_file() and (destination / "am/final.mdl").is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = destination.parent / "russian-model.zip"
    print("Загружаем русскую модель голоса с официального сайта Vosk…", flush=True)
    try:
        with (
            urllib.request.urlopen(
                "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip", timeout=90
            ) as response,
            archive.open("wb") as output,
        ):
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > 80 * 1024 * 1024:
                    raise RuntimeError("Файл модели превышает допустимый размер.")
                output.write(chunk)
        with zipfile.ZipFile(archive) as stream:
            if sum(item.file_size for item in stream.infolist()) > 256 * 1024 * 1024:
                raise RuntimeError("Некорректный архив модели.")
            for item in stream.infolist():
                target = (destination.parent / item.filename).resolve()
                if not target.is_relative_to(destination) or item.filename.startswith("/"):
                    raise RuntimeError("Небезопасный путь в архиве модели.")
            if stream.testzip() is not None:
                raise RuntimeError("Архив модели повреждён. Повторите запуск.")
            stream.extractall(destination.parent)
        (destination / ".ready").write_text("ok", encoding="ascii")
    finally:
        archive.unlink(missing_ok=True)


def install_git_hook():
    if not (ROOT / ".git").is_dir():
        return
    hook = ROOT / ".git/hooks/pre-commit"
    if hook.exists():
        return
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        """#!/bin/sh
if [ -x .venv/Scripts/python.exe ]; then
  exec .venv/Scripts/python.exe scripts/check_secrets.py --staged
elif [ -x .venv/bin/python ]; then
  exec .venv/bin/python scripts/check_secrets.py --staged
else
  exec python scripts/check_secrets.py --staged
fi
""",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)


def bootstrap():
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        raise RuntimeError("Нужен Python 3.11, 3.12 или 3.13.")
    envdir = ROOT / ".venv"
    python = envdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    extras = "[voice]" if "--voice" in sys.argv else ""
    fingerprint = hashlib.sha256(
        (str(ROOT) + str(sys.version_info[:2]) + extras).encode() + (ROOT / "pyproject.toml").read_bytes()
    ).hexdigest()
    state = ROOT / ".local/environment.json"
    install_git_hook()
    current = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {}
    if current.get("root") != str(ROOT) and envdir.exists():
        # Virtual environments contain absolute paths. Preserve the previous one
        # instead of reusing it after a move or deleting user files recursively.
        if envdir.is_symlink() or not envdir.resolve().is_relative_to(ROOT):
            raise RuntimeError("Папка .venv должна находиться внутри проекта без внешней ссылки.")
        backup = ROOT / (".local/previous-venv-" + uuid.uuid4().hex[:8])
        backup.parent.mkdir(exist_ok=True)
        envdir.rename(backup)
    if not python.is_file():
        print("Создаём отдельное окружение M-100…", flush=True)
        venv.EnvBuilder(with_pip=True).create(envdir)
    if current.get("fingerprint") != fingerprint:
        print("Устанавливаем зависимости проекта…", flush=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-e", str(ROOT) + extras],
            check=True,
            cwd=ROOT,
        )
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps({"root": str(ROOT), "fingerprint": fingerprint}), encoding="utf-8")
    environment = dict(os.environ, PYTHONUTF8="1")
    # The marker avoids recursion without relying on a machine-specific path.
    environment["M100_BOOTSTRAPPED"] = "1"
    return subprocess.call([str(python), str(ROOT / "start.py"), *sys.argv[1:]], cwd=ROOT, env=environment)


if __name__ == "__main__":
    try:
        if os.getenv("M100_BOOTSTRAPPED") != "1":
            raise SystemExit(bootstrap())
        if "--voice" in sys.argv:
            install_voice_model()
        from stroybot.local import main

        main()
    except KeyboardInterrupt:
        print("\nЗапуск остановлен.")
    except (OSError, RuntimeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print("Не удалось подготовить M-100: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
