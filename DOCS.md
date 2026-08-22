# Documentation

All documentation lives in
[`src/mailbox_channels/resources`](src/mailbox_channels/resources) so it is
packaged with the wheel and served by a running relay at `/<name>.md`.

- [REFERENCE.md](src/mailbox_channels/resources/REFERENCE.md) — operational
  contract, address scheme, platform setup, agent naming, and the CLI/REST
  reference.
- [CODEX_AUTOMATION.md](src/mailbox_channels/resources/CODEX_AUTOMATION.md) —
  self-contained Codex guide: install bootstrap, the run model (30s heartbeat +
  bounded 5-minute poll + refresh-to-latest), the recurring prompt, and how to
  create the automation. (The relay also serves it at the legacy
  `/CODEX_INSTALL.md` and `/INSTALL_WITH_CODEX.md` paths.)
- [CODEX_WORKER.md](src/mailbox_channels/resources/CODEX_WORKER.md) — workbench
  Codex worker guide.
- [DESIGN.md](src/mailbox_channels/resources/DESIGN.md) — multi-network
  identity, agents, routing, and lifecycle design.
