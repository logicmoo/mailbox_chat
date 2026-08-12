from mailbox_channel_relay_bridging_proxy.server import build_parser, run_relay_supervisor, runtime_paths


class FlakyRelay:
    def __init__(self) -> None:
        self.stop_requested = False
        self.status = {"enabled": False}
        self.configure_calls = 0
        self.cycle_calls = 0
        self.reset_calls = 0

    def configure(self) -> bool:
        self.configure_calls += 1
        if self.configure_calls == 1:
            raise RuntimeError("temporary configuration failure")
        self.status["enabled"] = True
        return True

    def cycle(self) -> None:
        self.cycle_calls += 1
        if self.cycle_calls == 1:
            raise ConnectionError("temporary adapter failure")
        self.stop_requested = True

    def reset_after_failure(self) -> None:
        self.reset_calls += 1


def test_supervisor_recovers_from_configuration_and_adapter_exceptions(monkeypatch) -> None:
    relay = FlakyRelay()
    delays = []
    monkeypatch.setattr("mailbox_channel_relay_bridging_proxy.server._safe_write_status", lambda _relay: None)
    run_relay_supervisor(relay, sleep=delays.append)
    assert relay.configure_calls == 3
    assert relay.cycle_calls == 2
    assert relay.reset_calls == 2
    assert delays == [1.0, 2.0, 1.0]
    assert relay.status["running"] is True


def test_server_port_flag_and_port_specific_runtime_paths(monkeypatch) -> None:
    monkeypatch.setenv("MAILBOX_RELAY_PORT", "47000")
    assert build_parser().parse_args([]).port == 47000
    assert build_parser().parse_args(["--port", "48000"]).port == 48000
    runtime_dir, pid_file, status_file = runtime_paths(48000)
    assert runtime_dir.name == "channel-relay-48000"
    assert pid_file.parent == runtime_dir
    assert status_file.parent == runtime_dir


def test_server_accepts_independent_mailbox_and_config_directories(tmp_path) -> None:
    mailbox = tmp_path / "mailbox"
    configuration = tmp_path / "configuration"
    arguments = build_parser().parse_args([
        "--mailbox-dir", str(mailbox), "--config-dir", str(configuration),
    ])
    assert arguments.mailbox_dir == mailbox
    assert arguments.config_dir == configuration
    runtime_dir, _, _ = runtime_paths(46667, mailbox)
    assert runtime_dir == mailbox / "runtime" / "channel-relay-46667"


def test_server_accepts_all_interfaces_bind_address() -> None:
    arguments = build_parser().parse_args(["--host", "0.0.0.0", "--port", "46667"])
    assert arguments.host == "0.0.0.0"
    assert arguments.port == 46667


def test_server_accepts_optional_verbose_level() -> None:
    parser = build_parser()
    assert parser.parse_args([]).verbose == 0
    assert parser.parse_args(["--verbose"]).verbose == 1
    assert parser.parse_args(["--verbose", "2"]).verbose == 2
    assert parser.parse_args(["-v", "0"]).verbose == 0
