---
name: t3-lab-setup
description: >-
  Use when installing, repairing, or re-checking the t3-lab-grok plugin on this
  host (a Grok Bot box, a Cursor machine, or any stdio MCP host): reach a T3 Code
  Nightly origin, pair once with a one-time code, list projects, register the
  MCP server, and verify with a tiny test spawn. Re-running it is doctor + repair.
---

# Set up t3-lab-grok on this host

Goal: this host can launch and follow T3 Code Nightly threads through the `t3-lab-grok` MCP server.
Every step has a check. Do not move on until the check passes. If a step is already done, its check passes and you skip it — this skill is idempotent, so re-running it is a doctor + repair pass.

## Stop rules (read first)

- **Secrets never go through chat.** Never ask the user to paste a pairing code or a Tailscale auth key into the conversation, and never put one in a tool argument, a file in the repo, a log, or a command line. Ask for them through the host's secure secret input (Grok Bot: a secret/environment variable) as `T3_PAIRING_CODE` and `TS_AUTHKEY`, and pass them only through stdin or the environment. Never echo them.
- **A pairing code is consumed even when pairing fails.** On any pairing failure, do not retry with the same code; ask the user for a new one (`t3 auth pairing create`, or T3 Settings → Connections) through the secret input.
- **Ask only what you cannot infer:** tailnet vs public HTTPS origin, the origin URL if unknown, and a default project or runtime mode if they want one. Everything else you can check yourself.
- If a check fails twice for the same reason, stop and report the exact failing check.

## 1. Prerequisites

```sh
uv --version            # install: curl -LsSf https://astral.sh/uv/install.sh | sh
if [ -d <ABS_PLUGIN_DIR>/.git ]; then git -C <ABS_PLUGIN_DIR> pull --ff-only; else git clone https://github.com/Ranvier-Technologies/t3-lab-grok.git <ABS_PLUGIN_DIR>; fi
cd <ABS_PLUGIN_DIR> && uv run --frozen python -c "import mcp, httpx; print('ok')"
```

Pick an absolute `<ABS_PLUGIN_DIR>` (Grok Bot box: `/workspace/plugins/t3-lab-grok`; Cursor: `~/.cursor/plugins/local/t3-lab-grok`, a real directory, not a symlink).

**Check:** the last command prints `ok`.

## 2. Reachability

T3 Nightly must be reachable from **this host** at an HTTPS origin (or `http://127.0.0.1:<port>` if Nightly runs on this same host). Ask the user which one applies (Grok Bot: a question widget):

- **Public HTTPS origin** (reverse proxy, tunnel such as Cloudflare Tunnel): get the origin URL, nothing to install.
- **Tailnet** (Nightly served over Tailscale HTTPS, e.g. `tailscale serve`):
  1. Install: `curl -fsSL https://tailscale.com/install.sh | sh`.
  2. Without systemd (containers, many bot boxes): `sudo tailscaled --state=/var/lib/tailscale/tailscaled.state >/tmp/tailscaled.log 2>&1 &`. This needs a TUN device (`/dev/net/tun`) and root. Do **not** use `--tun=userspace-networking`: the plugin's HTTP clients ignore proxy settings (`ALL_PROXY`), so a curl check could pass while pairing and MCP calls fail. If the host has no TUN device, use the public HTTPS origin path instead.
  3. Ask the user for an auth key through the secret input as `TS_AUTHKEY` (a tagged, reusable-off key is best). Never put the key itself on a command line, where other processes can read it; hand it over through a private file (`--auth-key=file:<path>`, supported by current Tailscale, e.g. 1.104):
     ```sh
     key=$(sudo mktemp) && sudo chmod 600 "$key"
     printf '%s' "$TS_AUTHKEY" | sudo tee "$key" >/dev/null
     sudo tailscale up --auth-key="file:$key" --advertise-tags=<tag> --hostname=<name>
     sudo rm -f "$key"; unset TS_AUTHKEY
     ```
     `unset` clears only the current shell. If the key is reusable and saved as a host env var, a later reconnect reuses it.
  4. If MagicDNS does not resolve the Nightly hostname here, add its tailnet IP from `tailscale status` to `/etc/hosts` (`<ip> <host>.<tailnet>.ts.net`).

**Check:**

```sh
curl -fsS <ORIGIN>/.well-known/t3/environment
```

returns JSON with `"orchestrationProtocolVersion": 2`. Protocol 1 or an error means stop: upgrade Nightly or fix the network first.

After a host update or restart, `tailscaled` may not be running. The error `T3 transport could not read public metadata` means this host cannot reach `<ORIGIN>`; on a tailnet origin, usually `tailscaled` is down or this host is off the tailnet: redo step 2 (otherwise check the origin and network).

## 3. Owner, pairing, and binding

Run from `<ABS_PLUGIN_DIR>`. Config and credentials live in `~/.config/t3-lab/v2` (override with `T3_LAB_V2_CONFIG_DIR`); the chat journal lives in `~/.local/state/t3-lab-v2` (override with `T3_LAB_V2_STATE_DIR`). Private files only. Export both overrides in every shell and in the MCP server env if you use them.

```sh
uv run --frozen python setup.py init                       # owner UUID file (mode 600); safe to re-run
uv run --frozen python setup.py inspect --origin <ORIGIN>  # read environmentId, serverVersion, protocol
```

Ask the user for a fresh one-time pairing code through the secret input as `T3_PAIRING_CODE`, then pair without a terminal prompt:

```sh
printf '%s' "$T3_PAIRING_CODE" | uv run --frozen python setup.py pair \
  --target <TARGET_ID> --origin <ORIGIN> --kind remote \
  --environment-id <ENVIRONMENT_ID_FROM_INSPECT> \
  --label "<host> t3-lab-grok" --accept-read-operate --token-stdin
unset T3_PAIRING_CODE
```

`--label` is the client name shown in T3 Settings → Connections (default `Cursor plugin`); choose one the user can recognize and revoke. The secret may be the bare code or the whole pairing link: if the link's address differs from `<ORIGIN>` (T3 often shows a LAN `http://` address), `pair` warns and sends only the token to `<ORIGIN>`.

Use `--kind local` only for a loopback origin on this same host. Add `--web-origin <https origin>` if the user opens T3 in a browser at that address.

```sh
uv run --frozen python setup.py projects --target <TARGET_ID>
```

One pairing per machine covers all of its projects: bots pick one per `spawn_thread` (`projectId` or `projectTitle`), see them with `list_projects`, and can register an existing folder with `add_project`. **No project** is T3's scratch project (root e.g. `~/.t3/scratch` or `~/.t3-nightly-omarchy/scratch`, depending on the install; `list_projects` shows the real path): handy for checks; each thread there gets its own dated subfolder, and it is where a spawn that names no project runs (unless `bind --project-id` set a default). Ids prefixed `mcp:` are projects created by an MCP client and work normally.

```sh
# optional: a default project for spawns that name none
uv run --frozen python setup.py bind --target <TARGET_ID> --project-id <ID>
# optional: a default runtime mode (approval-required, auto-accept-edits, auto, or full-access)
uv run --frozen python setup.py bind --target <TARGET_ID> --runtime-mode auto-accept-edits
python3 scripts/doctor-targets.py --require-remote <TARGET_ID>   # honors T3_LAB_V2_CONFIG_DIR (or pass --config)
uv run --frozen python setup.py status --probe
```

**Check:** `projects` lists the environment's projects; doctor exits 0; `status --probe` shows `"authentication": "stored"` and a provider list for the target.

## 4. Register the MCP server

Register a stdio MCP server named `t3-lab-grok` on this host:

- command: `uv`
- args: `run --frozen --directory <ABS_PLUGIN_DIR> python server.py`
- env: `PYTHONDONTWRITEBYTECODE=1`; `T3_LAB_OWNER` is optional (the `setup.py init` owner file is the fallback)

Grok Bot: use the host's add-MCP-server flow with exactly that command; use the absolute directory because some hosts ignore `cwd`. Cursor plugin install: nothing to register, the plugin's `mcp.json` does it; reload the window.

**Check:** the server lists its tools (`list_targets`, `spawn_thread`, …).

If the connector still lists old tools after an update, restart the host's MCP servers; if that fails, restart the host (Grok Bot: Update Computer), then redo step 2.

To verify without registering anything (or before the host reloads), drive it over stdio with the MCP Python SDK from the plugin venv:

```sh
cd <ABS_PLUGIN_DIR> && uv run --frozen python - <<'PY'
import asyncio, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
params = StdioServerParameters(command="uv", args=["run", "--frozen", "--directory", os.getcwd(), "python", "server.py"], env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "T3_LAB_V2_CONFIG_DIR", "T3_LAB_V2_STATE_DIR", "T3_LAB_OWNER"}})
async def main():
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        print([t.name for t in (await s.list_tools()).tools])
        print((await s.call_tool("list_targets", {})).content[0].text)
asyncio.run(main())
PY
```

## 5. Verify end to end

`target` is always the target **id** from `list_targets` (e.g. `mac`), never the environment UUID. Inputs are strict: unknown fields are rejected, and the error names the missing or unexpected field.

1. `list_targets`: call with no arguments (`{}`). Shows the target with `connection: configured`.
2. `list_providers` → `{"target": "<TARGET_ID>"}`. At least one provider with `"ready": true` and a model.
3. `list_projects` → `{"target": "<TARGET_ID>"}`. Pick a project (scratch **No project** is fine for a check).
4. `spawn_thread` (for remote targets, omit `checkout`; `workspace` must be `shared`):
   ```json
   {"target": "<TARGET_ID>", "projectId": "<id from list_projects>", "requestKey": "setup-check-<UTC timestamp>", "workspace": "shared", "runtimeMode": "approval-required",
    "modelSelection": {"instanceId": "<instanceId from list_providers>", "model": "<model>"},
    "prompt": "Reply with the word ready. Do not edit files."}
   ```
   Returns `threadId` and `operationState: "accepted"`. Use a fresh `requestKey` for every new check (re-running this check with an old key returns the earlier thread, or is refused if the arguments changed); reuse a key only to recover that same launch.
5. Follow it with `await_thread` → `{"threadId": "<id>"}`, or with the card: `delegation_status` → `{"threadIds": ["<id>"], "moment": "handoff", "waitSeconds": 25}`, then repeat with `"moment": "check-in"` and `"since": "<signature from the last result>"`.
6. Confirm with `read_thread` → `{"threadId": "<id>"}`.

Status vocabulary: the card's **Working** = `running`; **Turn done** = the turn finished, which `read_thread`/`await_thread` report as `"status": "completed"`, `"settled": true` with the reply in `lastAssistantMessage`. **Waiting for your approval** / **Waiting for your reply** = a pending approval or question; **Failed** shows `lastError`.

**Check:** the test thread settles `completed` (card: Turn done) with the expected reply. Report the target, provider, and thread link from the digest. Then follow `skills/t3-delegate` for real work.

## Keeping it healthy

- Credentials expire after about 30 days; `status` shows when re-pairing is needed. Re-pairing is step 3 with a new code.
- Optional: offer the user a quiet daily routine that runs `python3 scripts/doctor-targets.py` and `setup.py status --probe` and alerts only when pairing expires soon or a target becomes unreachable. Silence on success.
