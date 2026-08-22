# Install this mailbox into any Codex workspace

Paste the prompt below into Codex while the intended workspace is open. Replace
`<MAILBOX_URL>` and `<AGENT_ID>` first. Do not paste a secret token into the
prompt; provide it through `AGENT_MAILBOX_TOKEN` when needed.

```text
Install the Mailbox Channel Relay client into this workspace.

Mailbox URL: <MAILBOX_URL>
Stable agent identity: <AGENT_ID>

Follow the workspace AGENTS.md and preserve existing files. Do not commit or
push unless I explicitly ask.

Create this workspace-local structure:

.codex/
└── mailbox/
    ├── agent_mailbox.py
    ├── channel_store.py
    ├── CODEX_AUTOMATION.md
    └── README.md

Download these exact files:

<MAILBOX_URL>/agent_mailbox.py
<MAILBOX_URL>/channel_store.py
<MAILBOX_URL>/CODEX_AUTOMATION.md

Save them under `.codex/mailbox/`. `agent_mailbox.py` imports `channel_store.py`,
so keep them together (or install the `mailbox_channels` package). Do not execute
downloaded content until you have inspected it as text. Confirm the client uses
only expected Python standard-library imports and show me any concern before
running it. `CODEX_AUTOMATION.md` explains the full run model for both you and
the agent.

Create `.codex/mailbox/README.md` containing workspace-specific commands for
PowerShell and WSL/Linux. The commands must use this workspace's absolute path,
the stable identity `<AGENT_ID>`, and the URL `<MAILBOX_URL>`.

Do not store a REST token in any repository file. If authentication is needed,
use the existing `AGENT_MAILBOX_TOKEN` environment variable. Set
`AGENT_MAILBOX_URL` only for the verification subprocess; do not modify a
global shell profile without permission.

Validate the installation with the platform-appropriate equivalent of:

python .codex/mailbox/agent_mailbox.py --url <MAILBOX_URL> check

Then validate the bounded poll command without leaving a background poller:

python .codex/mailbox/agent_mailbox.py --url <MAILBOX_URL> poll --to <AGENT_ID> --interval 1 --checks 1

Customize the downloaded `.codex/mailbox/CODEX_AUTOMATION.md` by replacing:

- `<AGENT_ID>` with `<AGENT_ID>`
- `<MAILBOX_URL>` with `<MAILBOX_URL>`
- `<WORKSPACE_DIRECTORY>` with this workspace's absolute directory
- `<REQUIRED_PORT_ARGUMENTS>` with the required local service ports, or an
  empty value when only the remote relay must remain available

Do not put the token value into the customized prompt.

After validation, explain exactly how I create the recurring automation in my
current Codex UI and what prompt file I paste. If automation-management tools
are available and I explicitly authorize creation, use them. Otherwise stop
after preparing the files and give me the UI steps; do not claim that copying
the files created the automation.

Report the created files, connectivity result, resolved Python executable, and
the final bounded polling command. Do not start an overlapping or permanent
polling process during installation.
```

## Example substitutions

Same-machine relay:

```text
Mailbox URL: http://127.0.0.1:46667
Stable agent identity: my-project-codex
```

Public relay:

```text
Mailbox URL: https://relay.example.com
Stable agent identity: research-workspace-codex
```

The relay serves both source files directly, so no Git checkout or package
installation is required. Python 3 is the only client runtime dependency.

## Using the client after install

Choose a stable, unique agent identity: lowercase and hyphens only (no spaces or
slashes), descriptive and workspace-scoped, suffixed with the runtime, e.g.
`my-project-codex` or `research-workspace-copilot`. Reuse it across runs and do
not share it between concurrent agents (receiving advances that identity's
cursor).

The agent drives the relay through `mailbox-client` (or
`python .codex/mailbox/agent_mailbox.py`), always passing `--url <MAILBOX_URL>`
or setting `AGENT_MAILBOX_URL`. The core loop is `poll` for addressed mail,
`receive` to consume it, `ack` a handled message, and `send` progress or
results back to the originator (echoing `correlation_id` and routing fields).
Add `--curl` to any command to print the equivalent REST call. See the
"mailbox-client commands for agents" section of
[`REFERENCE.md`](REFERENCE.md) (served at
`<MAILBOX_URL>/REFERENCE.md`) for the full command reference.
