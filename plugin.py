"""Mailbox Chat workbench plugin entrypoint.

Mailbox Chat is a nested project (its own ``.git``, its own package under
``src/mailbox_chat``) vendored into ``workbench/plugins``. It runs a durable
mailbox relay that bridges chat networks -- IRC, Discord, Slack, Matrix,
Telegram, WhatsApp, Mattermost, and more -- through adapters. Each connected
channel or client gets its own mailbox, so one IRC channel and one Discord
channel (say) can exchange messages through a shared mailbox: this is what
lets otherwise-incompatible chat networks talk to each other.

The workbench plugin loader imports this file directly (not as part of a
package) and calls ``create_router(manifest)``. This plugin is
**standalone-only** -- there is no in-process "embedded" mode, unlike
``ws_collab``/``emullm`` -- because the relay is a separate installable
project with its own dependencies (``requirements.txt``) and its own
``src`` layout. ``create_router`` ensures the relay process is running
(spawning it if needed) and returns an empty router; the workbench reaches
it through ``web_proxy`` (see ``plugin.json``), same as the other
standalone plugins.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# .../workbench/plugins/mailbox_chat
_HERE = Path(__file__).resolve().parent
# The package lives under src/ (pyproject.toml: package-dir = {"" = "src"}).
_SRC = _HERE / "src"
# .../workbench/plugins/mailbox_chat -> plugins -> workbench -> repo root.
_REPO_ROOT = _HERE.parents[2]

DEFAULT_HOST = os.environ.get("MAILBOX_RELAY_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("MAILBOX_RELAY_PORT", "46667"))

# Windows process-creation flags: detach from the parent console and start a
# new process group so the relay survives the workbench process exiting.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _probe_host(host: str) -> str:
    """A connectable address for a bind host (wildcards map to loopback)."""

    if host in ("0.0.0.0", "::", ""):
        return "127.0.0.1"
    return host


def is_listening(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """Return True when a TCP connection to ``host:port`` succeeds."""

    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def launch(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    wait: bool = True,
    timeout: float = 20.0,
) -> subprocess.Popen | None:
    """Spawn the Mailbox Chat relay as a detached background process.

    Idempotent: returns ``None`` immediately if ``host:port`` is already
    serving. Otherwise starts ``python -m mailbox_chat.server`` detached, with
    stdout/stderr redirected to a log file beside the plugin. When ``wait`` is
    true, blocks until the port accepts connections (or raises on failure).
    """

    if is_listening(host, port):
        return None

    env = os.environ.copy()
    # The child must import ``mailbox_chat`` regardless of its cwd.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(_SRC), existing) if p)
    # Anchor the mailbox store to the shared repo-root mailbox/ directory, so
    # this relay agrees with the workbench's embedded mailbox API
    # (mailbox_api_lib._default_mailbox_root) and the STT listener, regardless
    # of the process cwd.
    env.setdefault("AGENT_MAILBOX_DIR", str(_REPO_ROOT / "mailbox"))

    log_dir = Path(env.get("MAILBOX_CHAT_STATE_DIR") or (_HERE / "collab_state"))
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "standalone.log"
    log = open(log_path, "ab", buffering=0)  # noqa: SIM115 - handed to the child

    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-m", "mailbox_chat.server", "--host", host, "--port", str(port)],
        cwd=str(_HERE),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        close_fds=True,
        **kwargs,
    )

    if not wait:
        return proc

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_listening(host, port):
            return proc
        if proc.poll() is not None:
            raise RuntimeError(
                f"mailbox_chat relay exited (code {proc.returncode}) during "
                f"startup; see {log_path}"
            )
        time.sleep(0.25)
    raise TimeoutError(
        f"mailbox_chat relay did not start listening on {host}:{port} within "
        f"{timeout:.0f}s; see {log_path}"
    )


def create_router(manifest: dict[str, Any] | None = None):
    """Ensure the relay is running, then contribute no in-process routes.

    The workbench serves ``/mailbox_chat`` by proxying to the standalone
    relay process (see the ``web_proxy`` entry in ``plugin.json``), so this
    router intentionally contributes no in-process routes.
    """

    launch(DEFAULT_HOST, DEFAULT_PORT)
    try:
        from fastapi import APIRouter
    except Exception:  # pragma: no cover - FastAPI always present in practice
        return None
    return APIRouter()


__all__ = ["create_router", "launch", "is_listening"]
