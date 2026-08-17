from mailbox_channels import cmd_client_page

class FakeStdout:
    def __init__(self): self.lines = iter(["help text\n", ""])
    def readline(self): return next(self.lines)
    def close(self): pass

class FakeProcess:
    def __init__(self, argv, **options):
        self.argv, self.options, self.stdout, self.returncode = argv, options, FakeStdout(), None
    def poll(self): return self.returncode
    def wait(self): self.returncode = 0; return 0
    def terminate(self): self.returncode = -15

def test_cmd_client_has_no_command_timeout_or_shell(monkeypatch):
    captured = {}
    def fake_popen(argv, **options):
        captured.update(argv=argv, options=options); return FakeProcess(argv, **options)
    monkeypatch.setattr(cmd_client_page.subprocess, "Popen", fake_popen)
    session = cmd_client_page.start_mailbox_client(
        'follow --to "worker one"', relay_url="http://127.0.0.1:46667", token="secret")
    assert captured["argv"][-3:] == ["follow", "--to", "worker one"]
    assert captured["options"]["env"]["AGENT_MAILBOX_TOKEN"] == "secret"
    assert "timeout" not in captured["options"] and "shell" not in captured["options"]
    assert session.session_id

def test_cmd_client_page_retains_arguments_and_controls_session():
    process = FakeProcess([])
    session = cmd_client_page.CommandSession("abc123", ["python", "-m", "module", "status"], process)
    page = cmd_client_page.render_cmd_client_page('send -- "<hello>"', session).decode()
    assert 'value="send -- &quot;&lt;hello&gt;&quot;"' in page
    assert "/cmd-client/output?session=" in page and "/cmd-client/stop?session=" in page
    assert "abc123" in page

def test_cmd_client_rejects_unclosed_quotes():
    try: cmd_client_page._client_argv('send "unfinished', "http://127.0.0.1:46667")
    except ValueError as error: assert "No closing quotation" in str(error)
    else: raise AssertionError("expected malformed arguments to be rejected")
