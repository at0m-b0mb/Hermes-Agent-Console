"""Paths, settings and the local secret vault.

Everything Hermes owns lives under ~/.hermes so uninstalling is `rm -rf ~/.hermes`.
API keys are encrypted at rest with a keystream derived from a 0600 machine
secret. This protects keys from casual disclosure (backups, sync folders, other
users); it is not protection against an attacker who already has your login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DB_PATH = HOME / "hermes.db"
SECRET_PATH = HOME / "secret.key"
WORKSPACE = HOME / "workspace"
LOGS = HOME / "logs"
WEB_DIR = Path(__file__).parent / "web"

DEFAULT_PORT = 4317


def ensure_dirs() -> None:
    for d in (HOME, WORKSPACE, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    HOME.chmod(0o700)


def _secret() -> bytes:
    ensure_dirs()
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
        SECRET_PATH.chmod(0o600)
    return SECRET_PATH.read_bytes()


def _keystream(nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 in counter mode. Stdlib-only stream cipher."""
    key = _secret()
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    raw = plaintext.encode()
    nonce = secrets.token_bytes(16)
    cipher = bytes(a ^ b for a, b in zip(raw, _keystream(nonce, len(raw))))
    tag = hmac.new(_secret(), nonce + cipher, hashlib.sha256).digest()[:16]
    return base64.b64encode(nonce + tag + cipher).decode()


def decrypt(blob: str) -> str:
    if not blob:
        return ""
    try:
        data = base64.b64decode(blob)
        nonce, tag, cipher = data[:16], data[16:32], data[32:]
        expect = hmac.new(_secret(), nonce + cipher, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expect):
            return ""
        return bytes(a ^ b for a, b in zip(cipher, _keystream(nonce, len(cipher)))).decode()
    except Exception:
        return ""


def mask(value: str) -> str:
    """Never show a full key back to the browser."""
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"


DEFAULTS = {
    "brand.name": "HERMES",
    "brand.engine": "OpenClaw",
    "default.provider": "ollama",
    "default.model": "qwen2.5:latest",
    "judge.provider": "",
    "judge.model": "",
    "custom.base_url": "",
    "safety.shell_timeout": "60",
    "safety.max_steps": "18",
    "safety.max_cost_per_run": "1.00",
    "safety.max_cost_per_day": "10.00",
    "verify.enabled": "1",
    "workforce.enabled": "1",
    "email.address": "",
    "email.imap_host": "",
    "email.imap_port": "993",
    "email.smtp_host": "",
    "email.smtp_port": "465",
    "email.allowed_recipients": "",
    "server.allowed_hosts": "",
    "server.trusted_proxy": "",
}


def bootstrap_env() -> dict:
    """API keys already exported in the shell are picked up as a fallback."""
    return {
        "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
        "openai": os.environ.get("OPENAI_API_KEY", ""),
        "groq": os.environ.get("GROQ_API_KEY", ""),
        "gemini": os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
    }
