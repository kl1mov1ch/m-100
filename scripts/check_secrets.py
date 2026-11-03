"""Scan publishable files without ever printing matching secret values."""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {".local", ".venv", ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
PATTERNS = {
    "Telegram token": re.compile(rb"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "Groq key": re.compile(rb"\bgsk_[A-Za-z0-9]{35,}\b"),
    "OpenAI key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    "private key": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "Bitrix webhook secret": re.compile(
        rb"https://[a-zA-Z0-9.-]+\.bitrix24\.[a-z]+/rest/[0-9]+/[A-Za-z0-9_-]{12,}/"
    ),
    "personal Windows path": re.compile(rb"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+"),
}


def private_literals():
    values = []
    profile = ROOT / ".local/account.env"
    if profile.exists():
        for line in profile.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key in {
                "TELEGRAM_BOT_TOKEN",
                "LLM_API_KEY",
                "ALLOWED_USER_IDS",
                "ADMIN_USER_IDS",
                "GOOGLE_SPREADSHEET_ID",
                "BITRIX_WEBHOOK_URL",
                "BITRIX_PORTAL_HOST",
                "MINIAPP_URL",
                "TELEGRAM_BOT_USERNAME",
            }:
                values.extend(v.encode() for v in value.strip().split(",") if len(v.strip()) >= 8)
    folder = ROOT / ".local/secrets"
    if folder.exists():
        import json

        for path in folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            values.extend(
                str(data[k]).encode()
                for k in ("client_email", "project_id", "private_key_id", "client_id")
                if data.get(k)
            )
    return values


def files(staged=False):
    args = ["git", "ls-files", "-z", "--cached"]
    if not staged:
        args += ["--others", "--exclude-standard"]
    try:
        result = subprocess.run(args, cwd=ROOT, check=True, capture_output=True)
        return sorted({ROOT / os.fsdecode(name) for name in result.stdout.split(b"\0") if name})
    except (OSError, subprocess.CalledProcessError):
        return [
            p
            for p in ROOT.rglob("*")
            if p.is_file()
            and not any(x in PRIVATE or x.endswith(".egg-info") for x in p.relative_to(ROOT).parts)
        ]


def scan(staged=False):
    issues = []
    literals = private_literals()
    for path in files(staged):
        rel = path.relative_to(ROOT)
        if not staged and not path.is_file():
            continue
        if any(part in {".local", "secrets", "credentials"} for part in rel.parts) or (
            path.name.startswith(".env") and path.name != ".env.example"
        ):
            issues.append((str(rel), "private file"))
            continue
        if staged:
            try:
                data = subprocess.run(
                    ["git", "show", ":" + rel.as_posix()], cwd=ROOT, check=True, capture_output=True
                ).stdout
            except subprocess.CalledProcessError:
                issues.append((str(rel), "index could not be checked"))
                continue
        else:
            data = path.read_bytes()
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                issues.append((str(rel), label))
        if any(value and value in data for value in literals):
            issues.append((str(rel), "private profile data"))
    return issues


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check files intended for GitHub")
    parser.add_argument("--staged", action="store_true")
    args = parser.parse_args()
    issues = scan(args.staged)
    if issues:
        for path, label in issues:
            print(f"{path}: {label}", file=sys.stderr)
        raise SystemExit(1)
    print("Publication check passed: no detected keys or private profile data.")
