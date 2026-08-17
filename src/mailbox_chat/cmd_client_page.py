"""Live browser console for the bundled mailbox command client."""
from __future__ import annotations

import html
import json
import os
import shlex
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field

MAX_ARGUMENT_LENGTH = 16_384
MAX_SESSIONS = 50

@dataclass
class CommandSession:
    session_id: str
    argv: list[str]
    process: subprocess.Popen[str]
    output: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            output = "".join(self.output)
        returncode = self.process.poll()
        return {"session": self.session_id, "running": returncode is None,
                "returncode": returncode, "output": output}

_sessions: dict[str, CommandSession] = {}
_sessions_lock = threading.Lock()

def _client_argv(arguments: str, relay_url: str) -> list[str]:
    if len(arguments) > MAX_ARGUMENT_LENGTH:
        raise ValueError(f"arguments exceed {MAX_ARGUMENT_LENGTH} characters")
    supplied = shlex.split(arguments, posix=True)
    # mailbox-client.cmd is only a platform launcher for this exact module.
    # Calling it directly avoids cmd.exe rewriting metacharacters in arguments.
    return [sys.executable, "-m", "mailbox_channels.agent_mailbox", "--url", relay_url, *supplied]

def _collect_output(session: CommandSession) -> None:
    assert session.process.stdout is not None
    for chunk in iter(session.process.stdout.readline, ""):
        with session.lock:
            session.output.append(chunk)
    session.process.stdout.close()
    session.process.wait()

def start_mailbox_client(arguments: str, *, relay_url: str, token: str = "") -> CommandSession:
    """Start the same entrypoint as mailbox-client.cmd, with no command timeout."""
    argv = _client_argv(arguments, relay_url)
    environment = os.environ.copy()
    if token:
        environment["AGENT_MAILBOX_TOKEN"] = token
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                               errors="replace", env=environment, bufsize=1)
    session = CommandSession(uuid.uuid4().hex, argv, process)
    with _sessions_lock:
        if len(_sessions) >= MAX_SESSIONS:
            completed = [key for key, value in _sessions.items() if value.process.poll() is not None]
            for key in completed[:max(1, len(_sessions) - MAX_SESSIONS + 1)]:
                _sessions.pop(key, None)
        if len(_sessions) >= MAX_SESSIONS:
            process.terminate()
            raise RuntimeError("too many active command sessions")
        _sessions[session.session_id] = session
    threading.Thread(target=_collect_output, args=(session,), daemon=True,
                     name=f"mailbox-client-{session.session_id[:8]}").start()
    return session

def command_status(session_id: str) -> dict[str, object] | None:
    with _sessions_lock:
        session = _sessions.get(session_id)
    return session.snapshot() if session else None

def stop_mailbox_client(session_id: str) -> bool:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if not session or session.process.poll() is not None:
        return False
    session.process.terminate()
    return True

def render_cmd_client_page(arguments: str = "", session: CommandSession | None = None,
                           error: str = "") -> bytes:
    escaped_arguments = html.escape(arguments, quote=True)
    command = ""
    session_id = ""
    if session:
        command = "$ mailbox-client " + " ".join(shlex.quote(item) for item in session.argv[3:]) + "\n"
        session_id = session.session_id
    elif error:
        command = f"error: {error}"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Mailbox command client</title>
<style>:root{{color-scheme:dark;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}}body{{max-width:1100px;margin:0 auto;padding:24px;background:#071116;color:#d7e8ea}}h1{{color:#39d6c5;font-size:1.35rem}}form{{display:grid;grid-template-columns:1fr auto;gap:8px}}input,button{{box-sizing:border-box;border:1px solid #26515a;border-radius:4px;background:#0c1d23;color:inherit;padding:10px 12px;font:inherit}}button{{color:#39d6c5;cursor:pointer}}.toolbar{{display:flex;justify-content:space-between;align-items:center;margin-top:12px}}pre{{min-height:16rem;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid #183b43;background:#041015;padding:16px}}code{{color:#8ce9df}}</style></head><body>
<h1>Mailbox command client</h1><p>Runs the complete bundled <code>mailbox-client.cmd</code> command surface against this relay.</p>
<form method="get" action="/cmd-client/"><input name="args" value="{escaped_arguments}" autofocus aria-label="mailbox-client arguments" placeholder="-h or status or follow --to agent-name"><button type="submit">Run</button></form>
<div class="toolbar"><span id="state">{'running' if session else 'ready'}</span><button id="stop" type="button" {'disabled' if not session else ''}>Stop</button></div>
<pre id="output" aria-live="polite">{html.escape(command)}</pre><script>
const session={json.dumps(session_id)},output=document.querySelector('#output'),state=document.querySelector('#state'),stop=document.querySelector('#stop');
async function refresh(){{if(!session)return;const response=await fetch('/cmd-client/output?session='+encodeURIComponent(session));const data=await response.json();output.textContent={json.dumps(command)}+data.output;state.textContent=data.running?'running':'exit: '+data.returncode;stop.disabled=!data.running;if(data.running)setTimeout(refresh,300)}}
stop.addEventConnector('click',async()=>{{await fetch('/cmd-client/stop?session='+encodeURIComponent(session),{{method:'POST'}});refresh()}});refresh();
</script></body></html>""".encode("utf-8")
