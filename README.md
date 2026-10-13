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
3. `inspect` → `pair` → `projects` (below). `bind` is optional: it only sets a default project (without one, unnamed spawns go to T3's scratch **No project**).
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
2. Run `init`, `inspect`, `pair --token-stdin`, `projects`, doctor (next section). Give the bot the pairing code through its secret input (for example as `T3_PAIRING_CODE`), never in chat.
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

## Pair (outside the chat)

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

One pairing per machine covers every project on it. List them, and optionally pick a default project and default runtime mode:

```sh
uv run --frozen python setup.py projects --target remote-dev
uv run --frozen python setup.py bind --target remote-dev --project-id PROJECT_ID   # optional
uv run --frozen python setup.py status --probe
```

Configs from 0.3.x/0.4.0 keep working. Their old binding is no longer a spawn default; set one explicitly with `bind --project-id` if you want it.

For Nightly on the same machine use `--kind local` with a loopback origin (`http://127.0.0.1:3773`) and optionally `--web-origin http://localhost:3773`; local launches pass the chat's own checkout.

Files under `~/.config/t3-lab/v2` (override with `T3_LAB_V2_CONFIG_DIR`; `scripts/doctor-targets.py` honors it too): `targets.json`, `owner`, and `credentials/<environment>.json`, all mode 600 in mode-700 directories. Credentials are refused if the file is readable by others, or if its directory is writable by others. Stored credentials last about 30 days; re-pair on expiry. The journal lives in `~/.local/state/t3-lab-v2/chats` (`T3_LAB_V2_STATE_DIR`).

## Tools (11)

| Tool | Does |
|---|---|
| `list_targets` | Configured environments (no network). |
| `list_providers` | Live providers and models of a target. |
| `list_projects` | Every project in the target's environment: id, title, workspaceRoot, default flag. |
| `add_project` | Register an existing folder on the target as a T3 project (idempotent; returns the existing project if the root is already registered). |
| `list_threads` | Operations this chat owns. |
| `spawn_thread` | Launch a thread in any project (`projectId` or `projectTitle`); returns the resolved project and the handoff digest. |
| `send_to_thread` | Message an owned thread: `mode` `steer` (default) or `queue`. |
| `read_thread` / `await_thread` | Current state and reply (head first; `truncated`, `totalChars`, page with `offset`). |
| `delegation_status` | Markdown digest for owned threads, across targets (one section per environment). |
| `show_delegation` | MCP Apps card plus digest. |

Project choice for `spawn_thread`: `projectId` or `projectTitle` (case-insensitive) first; otherwise, for a local target, the project matching the checkout's Git repository; otherwise the default set with `setup.py bind --project-id`; otherwise T3's scratch **No project** (each thread gets its own folder). The response carries `project: {id, title, resolvedBy}` (`id` is null for scratch; `resolvedBy` is `projectId`, `projectTitle`, `git`, `default` or `scratch`), a `warning` when it fell back to a default or scratch, and `digest`: the same handoff Markdown `delegation_status` would return, ready to paste.

## Runtime mode and remote targets

- New `spawn_thread` calls default to `runtimeMode: "full-access"`, regardless of the target's saved `runtime_mode`. Pass `approval-required`, `auto-accept-edits`, or `auto` explicitly to restrict a launch. Existing threads and target config are not changed.
- With `approval-required`, approvals and questions are answered in the T3 app; the plugin cannot approve.
- **Remote targets are shared-only:** they run in the chosen project's root with `workspace: "shared"`. A local checkout or a `worktree` request is refused for remote targets. Exception in practice: T3's scratch project (title **No project**, root `~/.t3/scratch`) gives every thread its own dated subfolder under that root; `read_thread` reports it as `scratch folder …`, not a git worktree (the text digest shows only project and target names). Project ids prefixed `mcp:` are projects created by an MCP client; they work like any other.

## Messaging (`send_to_thread`)

| Mode | Idle thread | Running thread |
|---|---|---|
| `steer` (default) | Starts a new turn | Steers the current work |
| `queue` (explicit) | Starts a new turn | Waits until the current turn finishes |

- The old `continue` mode is gone; sending to an idle thread covers it.
- Steering depends on the provider integration: T3 picks native steering or interrupt-and-restart. If it cannot steer, the send fails with an error; it is never silently queued.
- Steer is refused while the turn is still preparing or an approval/question is pending. Resolve those in T3 first.
- On servers advertising `serverResolvedCommandContext`, steer also sends `deliveryIntent: "steer"` so T3 resolves the active run under its dispatch lock.
- Results carry `messageId`. `accepted` means T3 stored it, not that the provider consumed it. `read_thread` reports `queued`, `queuePosition`, `queueHeld`, and `queuedMessages`; a held queue is resumed in T3.
- One `requestKey` per message; retry an uncertain send with the identical key, mode, and message.

See [docs/messaging.md](docs/messaging.md) for the pinned upstream contract.

## Doctor (reachability)

Configured ≠ reachable. Before the first spawn from a host that is not on the same network as the target:

```sh
python3 scripts/doctor-targets.py
python3 scripts/doctor-targets.py --require-remote remote-dev
```

Transport-agnostic: DNS + TCP to each target origin (VPN, LAN, or tunnel all look the same once the hostname resolves). Loopback origins are **this host**, not another machine.

## First spawn

1. `list_targets` → pick a reachable target
2. `list_providers` and `list_projects` for that environment (`add_project` if the folder is not a project yet)
3. `spawn_thread` with a stable `requestKey`, the project, a complete brief, and optionally `runtimeMode` (default `full-access`)
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
