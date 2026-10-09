# Changelog

## 0.3.8

Onboarding fixes from a stranger-style setup run (fresh clone, isolated config, remote tailnet target).

- **`setup.py pair`** accepts a pairing link for any address (T3 often shows a LAN `http://` link) and uses only its `#token`, sent to `--origin`, with a warning on stderr. Previously such a link failed with the misleading "Non-loopback servers require HTTPS." `--label` is documented (it names the client in T3 Settings → Connections).
- **`scripts/doctor-targets.py`** honors `T3_LAB_V2_CONFIG_DIR` for its default `--config`.
- **Tool input errors name the field:** "missing required field modelSelection in (arguments)", "unexpected field provider in (arguments) (allowed: …)". Values are still never echoed.
- `spawn_thread` and friends describe `target` as the target id from `list_targets`, not an environment UUID.
- Scratch project threads (T3 "No project", root `~/.t3/scratch`) run in a per-thread dated subfolder created by T3 even with `workspace: "shared"`; this is T3 behavior, not a plugin bug. Digests now label it `scratch folder …` instead of `worktree …` (only when the bound root is `…/.t3/scratch`; other subfolders keep `worktree …`).
- **Setup skill:** example JSON for every step-5 call, card ↔ `read_thread` status mapping (Turn done = `completed`/`settled`), `T3_LAB_V2_STATE_DIR`, "No project" and `mcp:` project ids explained, and a stdio verification snippet that needs no MCP registration.

## 0.3.7

English UI, stranger setup, cleanup. Not marketplace-listed yet.

- **English everywhere.** `phase`/`moment` are now `handoff`, `update`, `check-in`, `wrap-up` in tool schemas, digests, the card, skills, and docs. The pre-0.3.7 Spanish values (`encargo`, `aviso`, `seguimiento`, `cierre`) remain accepted as deprecated aliases and are normalized server-side and in the card.
- **`setup.py init`** creates a private owner UUID file (`~/.config/t3-lab/v2/owner`, mode 600) and prints it; re-running is a no-op. `resolve_owner` falls back to it after `_meta.threadId`, the `owner` argument, and `T3_LAB_OWNER`, so `T3_LAB_OWNER` is no longer a required plugin variable.
- **`setup.py projects --target X`** lists `id`, `title`, and `workspaceRoot` from the paired environment so `bind` no longer needs IDs from elsewhere.
- **`setup.py pair`** fails fast without a terminal unless `--token-stdin` is used, and accepts `--trust-inspected-environment` instead of `--environment-id`.
- **Credentials:** refused when the credentials or config directory is a symlink or writable by group/others; AppleDouble `._*` files are ignored.
- **New skill `skills/t3-lab-setup`:** check-by-check onboarding (prerequisites, tailnet or public HTTPS reachability, init/inspect/pair/projects/bind/doctor, MCP registration, test spawn) with secret-handling stop rules. Referenced from the README and `plugin.json` (`skills`).
- **README:** minimum Nightly (protocol 2, tested with `v0.0.46-nightly.20261004`+), "Set up with your Grok Bot", owner resolution, remote shared-only and ceiling notes, troubleshooting (folded in from the removed `docs/apps-mcp-cwd-fix.md`).
- **Card updates in ~2 s instead of up to 20 s.** `delegation_status` gains `waitSeconds` (1–45) and `since`: a server-side long-poll that re-reads every ~2 s and returns as soon as a row's `signature` (status, pending ask, error, round) changes, or at the timeout with fresh data. Rows, results, and `show_delegation` cards carry `signature`. The card waits 25 s per call and re-waits immediately, falls back to adaptive polling (3 s for the first minute, then 10 s, then 30 s) when the wait errors or is unsupported, and ticks the elapsed pill locally every second.
- Concurrency proven by test: a quick `list_targets` returns in ~0.2 s while a 5 s blocked `read_thread` is in flight on the same session.
- Driver lookup caches a confirmed missing provider instance, so status reads stop repeating `server.getConfig`; lookup errors stay retryable (CodeRabbit, PR #1).
- MCP Apps design note: the `ui/open-link` paragraph now matches the card (it sends `appUrl` when `openLinks` is available).
- CodeRabbit (PR #2): the owner-file fallback uses the server's config root; `signature` is a fixed-length hash; `setup.py init` refuses symlinked config paths (root-owned system links excepted) and opens the owner file with `O_NOFOLLOW`; the setup skill passes the Tailscale key via `--auth-key=file:` instead of argv and requires a TUN device (no `ALL_PROXY`/userspace advice, since the client ignores proxies); the TLS test only silences the expected reset.
- Removed `ui/delegation-card.opus.html` (duplicate of the served card) and its dev-only preview.
- `plugin.json` gains `homepage`, `repository`, `license`; `httpx`, `jsonschema`, and `certifi` are pinned directly in `pyproject.toml` (same versions as before; `uv.lock` refreshed).
- Tests: shipped files may only contain fixture UUIDs and no personal hosts/paths/tokens; English-only check outside alias handling; setup CLI, owner-file, and credential-directory tests; the TLS socket test no longer prints an expected handshake traceback.
- Version **0.3.7** in `plugin.json`, `pyproject.toml`, `uv.lock`, and the server.

## 0.3.6

Opus delegation card becomes the served card. Not marketplace-listed yet.

- `ui/delegation-card.html` (served as `ui://t3-lab/delegation-card`) is now the Opus monochrome card. It reads optional `driver`, `model`, `startedAt`, `completedAt`, `excerpt` and `ask` fields from `structuredContent` and shows the provider logo when the driver is known.
- Waiting and Stopped get their own pills; Waiting says what T3 is asking. Elapsed time sits in the Working pill.
- When the host can't or won't open links, the card shows Copy plus a hint.
- `t3code://threads/<env>/<thread>` is the primary link in the digest and the card: the digest leads with `[open in T3 ↗](t3code://…)` plus a copyable code span, and an https origin is demoted to `[web (needs paired browser) ↗]` (unpaired browsers land on T3's `/pair` page). The card's Open button sends the app link first and falls back to https only when there is no app link.
- Skill and MCP Apps design note follow the t3code-first link order.
- Tests follow the new card's link handling (`links()` table instead of `openTarget`, `#hint` instead of `#open-hint`).
- The owner-UUID guard test checks a sha256 hash instead of carrying the UUID literal.
- `plugin.json` / `pyproject.toml` / `uv.lock` / server version **0.3.6**.

## 0.3.4

Public-prep hygiene. Not marketplace-listed yet.

- Removed production owner UUID from docs, skill, and package tests; instruct users to generate their own `T3_LAB_OWNER`.
- Stranger-first README: placeholders instead of owner-specific host/path examples; clarify protocol facade (not official T3 Lab / not a `pingdotgg/t3code` fork).
- Root MIT `LICENSE`; keep `assets/T3-LICENSE.txt` for the logo.
- `plugin.json` / `pyproject.toml` / server version **0.3.4**; longDescription no longer says “Local private use”.
- Neutral pair default label: “Cursor plugin” (was “ChatGPT plugin”).
- Ignore AppleDouble `._*`, `.venv`, and local prove notes in `.gitignore`.

## 0.3.3

Clearer Open fallback + MCP cwd fix for hand-registered servers. Not marketplace-listed.

- Card: when only `t3code://` exists, Open labels itself as an app link, shows a hint to Copy, and on `ui/open-link` reject keeps the app URL + Copy visible.
- Hand-registered (user-level) MCP entries: some hosts ignore `cwd` and look for `~/server.py` (Connection closed). Register with an absolute `uv` path plus `uv run --directory <plugin>` so startup does not rely on cwd/PATH.

## 0.3.2

Chat trail + Open with `t3code://`. Not marketplace-listed.

- Digests prepend a stable trail line: open the `show_delegation` tool-result panel for the interactive card (same place as tldraw).
- `skills/t3-delegate` and the `show_delegation` tool description always require pasting the Markdown `content` into chat, even when the iframe mounts.
- Card Open still prefers http(s) via `ui/open-link`. When only `appUrl` (`t3code://`) exists, Open tries that scheme; on host reject, the URL stays visible and Copy still works.

## 0.3.0

MCP Apps delegation card. Phase C (webhook wake) stays parked. Not marketplace-listed.

- `show_delegation` is the only render tool. `_meta.ui.resourceUri` is `ui://t3-lab/delegation-card`. `spawn_thread`, `await_thread`, `delegation_status`, and `list_*` stay data tools with no UI metadata.
- `ui/delegation-card.html` is the sandboxed resource (`text/html;profile=mcp-app`, no CSP override). It handshakes with `ui/initialize`, paints the handoff phase, polls `delegation_status` in place, and stops at wrap-up. Open prefers an https web origin through `ui/open-link`. `t3code://` is copyable text.
- Every tool result still includes the Markdown digest in `content`. Digests link http(s) web origins and print `t3code://` as a code span.
- `skills/t3-delegate` calls `show_delegation` once after a successful spawn, and again at wrap-up only if no card rendered.

## 0.2.0

Cursor Plugin packaging. No MCP Apps in this release.

- One manifest: `.cursor-plugin/plugin.json`. The root Agent Plugin `plugin.json` is gone.
- `variables.T3_LAB_OWNER` is the journal-partition UUID. Set it in Plugins → Configure. Generate your own UUID; do not commit a real owner value. `mcp.json` passes `"${T3_LAB_OWNER}"` and does not hardcode the UUID.
- Stdio server cwd is `${CURSOR_PLUGIN_ROOT}`. Command remains `uv run --frozen python server.py`.
- Existing Python tools and `skills/t3-delegate` stay. `show_delegation` and `ui://t3-lab/delegation-card` are still Phase B ([docs/cursor-plugin-mcp-apps.md](docs/cursor-plugin-mcp-apps.md)).
- Marketplace submission stays an explicit owner decision.

## 0.1.1

Agent Plugin (`plugin.json` at the repo root) with `T3_LAB_OWNER` written directly into `mcp.json` and cwd `${PLUGIN_ROOT}`.
