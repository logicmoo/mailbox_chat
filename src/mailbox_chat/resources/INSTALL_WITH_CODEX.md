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
    ├── AUTOMATION_PROMPT.md
    └── README.md

Download these exact files:

<MAILBOX_URL>/agent_mailbox.py
<MAILBOX_URL>/AUTOMATION_PROMPT.md

Save them under `.codex/mailbox/`. Do not execute downloaded content until you
have inspected it as text. Confirm `agent_mailbox.py` uses only expected Python
standard-library imports and show me any concern before running it.

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

python .codex/mailbox/agent_mailbox.py --url <MAILBOX_URL> poll <AGENT_ID> --interval 1 --checks 1

Customize the downloaded `.codex/mailbox/AUTOMATION_PROMPT.md` by replacing:

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
