"""Local administration for the relay REST bearer token."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from pathlib import Path

from .listener_registry import config_dir


TOKEN_NAME = "MAILBOX_RELAY_TOKEN"


def register_token(configuration: Path, token: str | None = None) -> str:
    """Generate or register a token in config/.env and return it once."""
    value = token or secrets.token_urlsafe(48)
    if len(value) < 32:
        raise ValueError("relay token must contain at least 32 characters")
    configuration.mkdir(parents=True, exist_ok=True)
    env_file = configuration / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    replacement = f"{TOKEN_NAME}={value}"
    updated: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{TOKEN_NAME}="):
            if not replaced:
                updated.append(replacement)
                replaced = True
        else:
            updated.append(line)
    if not replaced:
        updated.append(replacement)
    temporary = env_file.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
    try:
        temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    temporary.replace(env_file)
    return value


def token_registered(configuration: Path) -> bool:
    env_file = configuration / ".env"
    if not env_file.exists():
        return False
    return any(
        line.strip().startswith(f"{TOKEN_NAME}=") and line.partition("=")[2].strip()
        for line in env_file.read_text(encoding="utf-8").splitlines()
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="mailbox-client token",
                                     description="Register relay REST authentication locally")
    result.add_argument("action", choices=("register", "status"),
                        help="register/rotate a token or report whether one exists")
    result.add_argument("--config-dir", type=Path, help="relay configuration directory")
    result.add_argument("--token", help="existing token; omit to securely generate one")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    configuration = (arguments.config_dir or config_dir()).expanduser().resolve()
    if arguments.action == "status":
        print("registered" if token_registered(configuration) else "not registered")
        return 0 if token_registered(configuration) else 1
    token = register_token(configuration, arguments.token)
    print("Relay token registered. Copy this value to authorized clients now; it is shown once:")
    print(token)
    print("Restart the relay, then set AGENT_MAILBOX_TOKEN to the same value on each client.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
