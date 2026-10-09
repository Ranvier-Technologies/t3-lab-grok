# t3-lab-grok

**Parent chat → T3 Nightly orchestration.** A Cursor plugin / MCP facade that keeps your conversation in Cursor Agent, Grok Bot, or Codex while launching durable work into official [T3 Code](https://t3.gg) Nightly environments through the protocol-2 client APIs.

This is a **protocol facade** (external parent → T3). It is **not** an official T3 Lab product, and it is **not** a fork of `pingdotgg/t3code`. Logo attribution for `assets/t3-code.png` lives in `assets/T3-LICENSE.txt` (MIT, T3 Tools Inc.). The plugin code is MIT — see root `LICENSE`.

Status: prepared for public/community use; **not published yet** to the Cursor Marketplace. Install from a local path for now.

```
┌─────────────────────┐     MCP stdio      ┌──────────────────────┐
│ Parent chat         │ ─────────────────► │ This plugin          │
│ (Cursor / Grok Bot  │   spawn / await    │ (t3-lab-grok)        │
│  / Codex)           │ ◄───────────────── │ journal + tools      │
└─────────────────────┘                    └──────────┬───────────┘
                                                      │ protocol-2
                                                      ▼
                                           ┌──────────────────────┐
                                           │ T3 Nightly client    │
                                           │ (paired environments)│
                                           └──────────────────────┘
```

## Prerequisites

- **T3 Code Nightly with orchestration protocol 2.** Tested with `v0.0.46-nightly.20261004` and later (verified against `20261004.2648` and `20261009.2873`). The plugin needs `/.well-known/t3/environment` reporting `orchestrationProtocolVersion: 2`, the `/oauth/token` environment-bootstrap token exchange, `/api/orchestration/shell`, `/api/orchestration/threads/<id>`, `/api/auth/websocket-ticket`, and the `/ws?orchestrationProtocol=2` socket. Servers that report protocol 1 are refused before any pairing or operation is sent.
- **Python ≥ 3.12** and **[uv](https://github.com/astral-sh/uv)** on `PATH`.
- A host that loads a local Cursor Plugin or a stdio MCP server (Cursor, Grok Bot, Codex, …).
- A way for that host to reach Nightly: `http://127.0.0.1:<port>` on the same machine, or an **HTTPS** origin elsewhere (Tailscale HTTPS, a reverse proxy, or a tunnel). Plain `http://` is refused off loopback.

## Quick start

1. Install the code (Cursor local plugin or any checkout, below).
2. `uv run --frozen python setup.py init` — creates your private owner UUID.
3. `inspect` → `pair` → `projects` → `bind` (below).
4. Register or reload the MCP server, then `list_targets` / `list_providers`.

A step-by-step, check-by-check version that an agent can follow on its own is in [`skills/t3-lab-setup/SKILL.md`](skills/t3-lab-setup/SKILL.md).

## Install in Cursor (local plugin)

Cursor loads local plugins only from a real directory under `~/.cursor/plugins/local` (symlinks whose target is outside that folder are skipped):

```sh
mkdir -p ~/.cursor/plugins/local
git clone https://github.com/Ranvier-Technologies/t3-lab-grok.git ~/.cursor/plugins/local/t3-lab-grok
```

Reload Cursor (**Developer: Reload Window**). The plugin's `mcp.json` starts the server with `uv run --frozen python server.py` from `${CURSOR_PLUGIN_ROOT}`.

## Set up with your Grok Bot

Your Grok Bot runs the MCP server on its own box, so the box must reach your Nightly:

- **Tailnet:** the box joins your tailnet (`tailscale up --auth-key=file:<private file>`; `tailscaled` needs a TUN device, because the plugin does not use proxy settings, so userspace networking is not enough) and uses Nightly's `https://<host>.<tailnet>.ts.net:<port>` origin.
- **Public HTTPS:** Nightly sits behind a reverse proxy or tunnel with a real certificate; the box just uses that origin.

Then ask your bot to follow `skills/t3-lab-setup`. In short:

1. Clone to an absolute path on the box, e.g. `/workspace/plugins/t3-lab-grok`.
2. Run `init`, `inspect`, `pair --token-stdin`, `projects`, `bind`, doctor (next section). Give the bot the pairing code through its secret input (for example as `T3_PAIRING_CODE`), never in chat.
3. Register a stdio MCP server:
   - command `uv`
   - args `run --frozen --directory /workspace/plugins/t3-lab-grok python server.py`
   - env `PYTHONDONTWRITEBYTECODE=1`; `T3_LAB_OWNER` is optional (the `init` owner file is used when it is unset)
4. Verify with `list_targets`, `list_providers`, and one tiny `approval-required` test spawn.

## Owner UUID (`T3_LAB_OWNER`)

Journal entries are partitioned by an owner UUID, so one install cannot read another's operations. Resolution order (fail closed):

1. `_meta.threadId` when it is a valid UUID (Codex hosts)
2. optional tool argument `owner` (UUID)
3. env `T3_LAB_OWNER` (UUID) — in Cursor, set it in **Plugins → Configure**; `mcp.json` passes `${T3_LAB_OWNER}`
4. the private owner file `~/.config/t3-lab/v2/owner` written by `setup.py init`

You never need to invent a UUID: `init` generates one, stores it with mode 600, and prints it. Re-running it prints the same value. Do not reuse someone else's owner UUID.

## Pair and bind (outside the chat)

Keep pairing codes and bearer tokens out of chat, tool arguments, package files, and logs.

```sh
cd <plugin dir>
uv run --frozen python setup.py init
uv run --frozen python setup.py inspect --origin https://t3.example.com
```

Create a one-time pairing code on the Nightly host (`t3 auth pairing create`, or **Settings → Connections**). Pair interactively (hidden prompt):

```sh
uv run --frozen python setup.py pair \
  --target remote-dev --kind remote \
  --origin https://t3.example.com \
  --environment-id ENVIRONMENT_ID_FROM_INSPECT \
  --accept-read-operate
```

or headless, with the code piped from an environment variable:

```sh
printf '%s' "$T3_PAIRING_CODE" | uv run --frozen python setup.py pair \
  --target remote-dev --kind remote --origin https://t3.example.com \
  --environment-id ENVIRONMENT_ID_FROM_INSPECT --accept-read-operate --token-stdin
```

`pair` refuses to wait on a hidden prompt when there is no terminal; use `--token-stdin` there. `--trust-inspected-environment` replaces `--environment-id` with whatever the origin reports at that moment. The input may be the bare code or a full pairing link (`…/pair#token=…`). The link T3 shows can name a LAN or `http://` address that differs from your `--origin`; `pair` then warns and uses only the link's token, sent to `--origin`. Pass `--label "<host> t3-lab-grok"` so the client is recognizable (and revocable) in T3 Settings → Connections; the default label is `Cursor plugin`. **A code is consumed even if pairing fails**; ask for a new one instead of retrying.

Then bind the existing project the target should work in:

```sh
uv run --frozen python setup.py projects --target remote-dev
uv run --frozen python setup.py bind --target remote-dev \
  --project-id PROJECT_ID --project-root /absolute/path/on/that/host
uv run --frozen python setup.py status --probe
```

For Nightly on the same machine use `--kind local` with a loopback origin (`http://127.0.0.1:3773`) and optionally `--web-origin http://localhost:3773`; local launches pass the chat's own checkout instead of a bound root.

Files under `~/.config/t3-lab/v2` (override with `T3_LAB_V2_CONFIG_DIR`; `scripts/doctor-targets.py` honors it too): `targets.json`, `owner`, and `credentials/<environment>.json`, all mode 600 in mode-700 directories. Credentials are refused if the file is readable by others, or if its directory is writable by others. Stored credentials last about 30 days; re-pair on expiry. The journal lives in `~/.local/state/t3-lab-v2/chats` (`T3_LAB_V2_STATE_DIR`).

## Runtime ceiling and remote targets

- New targets get an `approval-required` ceiling. `bind --runtime-ceiling auto-accept-edits|auto|full-access` raises it on purpose. Tool calls may narrow the ceiling, never raise it.
- With `approval-required`, approvals and questions are answered in the T3 app; the plugin cannot approve.
- **Remote targets are shared-only:** they run in the bound project root with `workspace: "shared"`. A local checkout or a `worktree` request is refused for remote targets. Exception in practice: T3's scratch project (title **No project**, root `~/.t3/scratch`) gives every thread its own dated subfolder under that root; `read_thread` reports it as `scratch folder …`, not a git worktree (the text digest shows only project and target names). Project ids prefixed `mcp:` are projects created by an MCP client; they bind like any other.

## Doctor (reachability)

Configured ≠ reachable. Before the first spawn from a host that is not on the same network as the target:

```sh
python3 scripts/doctor-targets.py
python3 scripts/doctor-targets.py --require-remote remote-dev
```

Transport-agnostic: DNS + TCP to each target origin (VPN, LAN, or tunnel all look the same once the hostname resolves). Loopback origins are **this host**, not another machine.

## First spawn

1. `list_targets` → pick a reachable target
2. `list_providers` for that environment
3. `spawn_thread` with a stable `requestKey`, a complete brief, and `runtimeMode` ≤ ceiling
4. After success, `show_delegation` once with `phase` `handoff`; paste the Markdown `content` into chat as the trail
5. `await_thread` / `read_thread` to recover; use the same `requestKey` + args after a cut

Phases and moments are `handoff`, `update`, `check-in` (moment only), and `wrap-up`. The Spanish values used before 0.3.7 (`encargo`, `aviso`, `seguimiento`, `cierre`) are still accepted as deprecated aliases.

Skills: `skills/t3-delegate` (daily use), `skills/t3-lab-setup` (install and repair). Contract notes: [docs/cursor-plugin-mcp-apps.md](docs/cursor-plugin-mcp-apps.md).

## Ownership and recovery

Use a stable `requestKey` per logical launch or continuation. The plugin records key, arguments, and IDs before dispatch; the same key with changed args is refused. Unconfirmed outcomes are **unknown**, not failed — recover with `read_thread` / `list_threads` or by repeating the exact key. Another owner UUID cannot read those journal entries. Local journals do not sync between computers automatically.

## MCP Apps card (optional)

On hosts that render MCP Apps, `show_delegation` draws `ui://t3-lab/delegation-card`, which long-polls `delegation_status` (returns within ~2 s of a state change, re-waits every 25 s; adaptive polling if the wait fails). Text-only hosts still get the Markdown digest: per thread a bold `#N · status · title` line, a `provider · model · short id` line, and the `t3code://threads/<env>/<thread>` deep link alone in a fenced code block so it can be copied on a phone (chat clients such as the Grok Bot iOS app do not open `t3code://` links on tap; pasting into Safari does). The digest itself never adds hostnames, IPs, origins, or checkout paths; the thread title is shown as given, so keep titles free of private details. There is no automatic wake of an idle parent chat.

## Troubleshooting

- **"Connection closed" / `can't open file '…/server.py'`** when registering the server by hand (a user-level `mcp.json` or a bot's MCP settings): some hosts ignore `cwd` and start the process in your home directory. Use `uv run --frozen --directory <absolute plugin dir> python server.py`, and an absolute path to `uv` if the host's `PATH` lacks it (find it with `command -v uv`). The plugin-installed server (`${CURSOR_PLUGIN_ROOT}`) is not affected.
- **`needs_pairing_or_connection_check`** in `status`: the credential is missing, expired, or has loose permissions (`chmod 700` the directories, `chmod 600` the files). Re-pair with a new code if expired.
- **DNS failure in doctor:** the host is not on your tailnet/VPN, or MagicDNS is not active there; fix name resolution (or add an `/etc/hosts` entry) and re-run doctor.
- **Protocol 1** from `inspect`: upgrade Nightly.

## Verify

```sh
uv run --frozen python -m unittest discover -s tests -v
```

## License and trademark

- Plugin source: MIT (`LICENSE`), author Bastian.
- T3 logo asset: MIT attribution to T3 Tools Inc. (`assets/T3-LICENSE.txt`).
- Not an official T3 Lab product; not a fork of `pingdotgg/t3code`.

Marketplace listing remains an explicit owner decision; this release does not add `.cursor-plugin/marketplace.json`.
