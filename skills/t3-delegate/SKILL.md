---
name: t3-delegate
description: >-
  Use when spawning, awaiting, or following up on a T3 Code Nightly thread from
  a parent chat (Cursor Agent, Grok Bot, or Codex): pick target and
  project, recover by threadId/requestKey, and report settlement without
  moving the parent conversation into T3.
---

# Delegate through T3

Use this plugin's MCP tools. Keep the parent conversation in the host chat.
Do not migrate servers, extract provider credentials, or invent a native T3 parent.

`Delegation = { target, provider, model, project, runtimeMode, requestKey, threadId, settlement }`

Ownership: Codex may supply `_meta.threadId`. Cursor Agent / Grok Bot use the `T3_LAB_OWNER` environment variable (Cursor Plugin variable, set in Plugins → Configure; `mcp.json` passes `${T3_LAB_OWNER}`). **Generate your own owner UUID** for that install (or run `setup.py init`, whose private owner file is the fallback); do not invent a new UUID per call. Setup lives in `skills/t3-lab-setup`.

## Host topology (this host vs remote)

The MCP stdio server runs on **this host** (the machine that loaded the plugin), not on a remote laptop by magic.

- Prefer **remote** targets (`kind=remote`) when Nightly runs elsewhere. Their `origin` is whatever pairing wrote (HTTPS off-loopback, or a tunnel URL).
- A target with `origin` on `127.0.0.1` / `localhost` means **this host's loopback**, not another machine's.
- Reachability is transport-agnostic: DNS + TCP to the configured host:port. VPN, LAN, or a tunnel all work once the hostname resolves and the port answers. If doctor says unreachable, stop and say so.
- After `setup.py bind` / re-pair on the Nightly host, re-sync `~/.config/t3-lab/v2` to this host before trusting defaults or origins.

Run `scripts/doctor-targets.py` before the first spawn in a session (or when list/spawn fails oddly). Configured ≠ reachable.

## Launch

1. Call `list_targets`, then `list_providers` for the chosen environment. Never silently substitute provider, model, or destination.
2. Pick the project: `list_projects` shows every project on the target; pass `projectId` or `projectTitle`. If the folder is not a project yet, `add_project` with its absolute path on that machine. Naming no project runs in T3's scratch **No project**; the result's `warning` says so, so name the project whenever the task belongs to one. `runtimeMode` defaults to the target's `runtime_mode`; pass one only when the task needs it.
3. Local launches in a Git project: pass this chat's actual absolute checkout. Remote launches: pass `target`, omit checkout; they run shared at the project root. Protocol 1 is unsupported.
4. Write a complete brief (paths, goal, permissions, checks, return format). The child cannot see this conversation.
5. Stable `requestKey` per logical task. `shared` sees uncommitted work in place; `worktree` starts from last commit.
6. Launch independent tasks together. Retain every `threadId` and `requestKey`. On unconfirmed outcome, read that ID or repeat the exact requestKey — never invent a replacement thread.
7. After a successful `spawn_thread`, call `show_delegation` once with that `threadId` and `phase` `handoff`. Always paste the tool `content` (Markdown digest) as normal assistant text — never a code block — even when the host draws the iframe. Add one short line that the interactive card lives in the tool-result panel (expand `show_delegation`, like tldraw). Do not poll. Do not call `show_delegation` for update; the card polls `delegation_status`. Do not construct thread links yourself. The digest's primary link is `t3code://` (it opens the thread in the T3 app); the https web link only works in a browser already paired with T3.

## Wait and intervene

`spawn_thread` already returns the handoff `digest`; paste it instead of calling `delegation_status` again. `delegation_status` accepts threads from several targets in one call (one section per environment). Long replies from `read_thread` / `await_thread` return the head with `truncated` and `totalChars`; page with `offset`.

Await every owned child until it settles (`timeoutSeconds=45`). Timeout / queued / No data is not failure — await the same ID again; no message per await.

Settlement:
- Done for synthesis: `settled=true` and `status=completed` (plus delivery when present).
- Human pause: `waiting_for_approval` or `waiting_for_input` with `settled=true` — tell the user where to act in T3; do not fabricate an approval tool. After they finish, they say "continue".
- `background` means the turn ended but children still run — keep awaiting.

Use `moment=update` only for a new failure, question, approval, connection issue, or plan change. If `changed=false`, write nothing. Use `check-in` only when the user asks for status.

Pairing uses `setup.py` outside the chat. Never put codes/tokens in tool arguments.

## Continue and close

Follow-up on a settled thread: `send_to_thread` with the same `threadId`, a new stable `requestKey`, and a complete brief. After confirmation: "same thread, no new one created". Await the new round. Unconfirmed send must not reuse the old answer.

When all settle: synthesize from artifacts first, quote each child result once. Call `show_delegation` with `phase` `wrap-up` only if no card rendered; otherwise the iframe moves to wrap-up on its own. Whenever you called `show_delegation`, paste its `content` digest as the chat trail (text-only hosts included). `delegation_status` with `moment=update` still means write nothing when `changed=false`.

## Do not

- Treat loopback targets on this host as a different machine.
- Count `waiting_for_approval` as task completion.
- Submit this plugin to a marketplace without an explicit owner ask.
