# Cursor Plugin + MCP Apps for t3-lab-grok

Architecture for a personal plugin that delegates work to a local T3 thread and, on hosts that support it, draws one inline card. Markdown digests stay the contract. The card is progressive enhancement.

**Status (0.3.0):** Phase A and Phase B are the repository root. The installable Cursor Plugin is `.cursor-plugin/plugin.json` plus `mcp.json` (`cwd` `${CURSOR_PLUGIN_ROOT}`, env `T3_LAB_OWNER=${T3_LAB_OWNER}`). Generate your own `T3_LAB_OWNER` UUID in Plugins → Configure; it is not hardcoded in `mcp.json`. `show_delegation` is the only tool with `_meta.ui.resourceUri` (`ui://t3-lab/delegation-card`, HTML in `ui/delegation-card.html`). The iframe polls `delegation_status`. Phase C (webhook wake) stays parked. The earlier `stubs/` sketches were removed when the working server was packaged. Sections below remain the contract; where they mention `stubs/`, read the repo root instead.

## 1. Starting point

The working plugin is a local bundle the owner already runs:

| Piece | Role today |
| --- | --- |
| Stdio MCP server (t3-lab fork) | `spawn_thread`, `await_thread`, `delegation_status`, and the rest of that server’s tools |
| `skills/t3-delegate` | Tells the parent agent when to delegate and how to read the result |
| `T3_LAB_OWNER` | Owner identity the server already expects. Used from Grok Bot. Value stays off git |
| Result | A Markdown digest pasted into the parent chat |

Two limits drive the redesign:

- The parent chat has no plugin-owned card. Cloud Agent cards are first-party host UI. Cursor’s plugin and MCP docs never expose that card format to a plugin.
- An `[app ↗](t3code://…)` link can be pasted into Safari and opened. The same link, clicked inside Grok Bot chat, often does nothing. Treat that as a custom-scheme sanitizer in the chat surface. Cursor’s deeplink docs do not describe chat URL filtering, so the design does not depend on `t3code://` being clickable.

The live `t3-lab-grok` MCP connection was down while this note was written. Tool names above are the owner’s stated surface, not a fresh `tools/list`.

## 2. Agent Plugin vs Cursor Plugin

Cursor loads two bundle formats ([Plugins](https://cursor.com/docs/plugins), [Plugins reference](https://cursor.com/docs/reference/plugins)):

| | Agent Plugin | Cursor Plugin |
| --- | --- | --- |
| Manifest | `plugin.json` at the plugin root | `.cursor-plugin/plugin.json` |
| Documented contents | Skills and MCP servers | Skills, MCP servers, rules, agents, commands, hooks, and variables |
| MCP file | Root `mcp.json`, transport declared (`"type": "stdio"`) | Root `mcp.json`. Cursor infers stdio from `command` and HTTP from `url` |
| Root variable | Standard uses `${PLUGIN_ROOT}` | Cursor expands `${CURSOR_PLUGIN_ROOT}` (and `${CLAUDE_PLUGIN_ROOT}`) in `command`, `args`, `env`, and `cwd` |

Cursor’s own words on the variable: a plugin that follows the Agent Plugins specification loads in Cursor, and Cursor does not expand the standard’s `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` in `mcp.json`. Use `${CURSOR_PLUGIN_ROOT}`. The reference still shows `${PLUGIN_ROOT}` inside the Agent Plugin *example*, because that example is the open-standard file. Copying that example into a bundle Cursor will run leaves the path unexpanded.

A Cursor Plugin manifest only requires `name` (lowercase kebab-case, start and end alphanumeric). Useful optional fields for this plugin are `description`, `version`, `keywords`, `skills`, `mcpServers`, and `variables`. `displayName` is not a documented field. `variables` is a JSON Schema of names only. Secret values are set in the dashboard under Plugins → Configure and are substituted into `${VAR}` placeholders. The plugin must not contain the value.

Default discovery, when the manifest does not override a component ([reference](https://cursor.com/docs/reference/plugins)):

| Component | Default path |
| --- | --- |
| Skills | `skills/<name>/SKILL.md` |
| MCP | `mcp.json` |
| Rules, agents, commands, hooks | `rules/`, `agents/`, `commands/`, `hooks/hooks.json` |

Setting `mcpServers` in the manifest **replaces** `mcp.json` discovery. Leave it unset and keep `mcp.json` at the plugin root.

### What to ship

Ship a **Cursor Plugin**, not a root Agent Plugin manifest.

The bundle needs one Cursor-only feature the Agent Plugin format does not list: a declared variable for `T3_LAB_OWNER`, so the owner identity is configured in the dashboard and never written into the repo. Skills and the stdio server are legal in both formats; the variable is why the manifest moves under `.cursor-plugin/`.

Local install path from the plugins doc: `~/.cursor/plugins/local/t3-lab-grok`, then restart or run Developer: Reload Window. Enterprise teams default **Allow Local Plugin Imports** to off. A team account can refuse the folder until an admin enables it.

Plugin-managed MCP config is read-only in the dashboard. Edit the source bundle, not the installed copy.

### When to dual-ship

Dual-ship means two manifests for two host families. It does not mean two manifests in one directory.

The install instructions say to include **either** a root `plugin.json` **or** `.cursor-plugin/plugin.json`. Cursor detects the format from that location. One folder that contains both is not a documented layout. A marketplace plugin with the same `name` takes precedence over the local copy, so a later publish under `t3-lab-grok` would shadow the laptop bundle. This tree is prepared for community use but is not marketplace-listed yet, and it does not add `.cursor-plugin/marketplace.json`.

Add a separate Agent Plugin tree only if a non-Cursor host that speaks the [Agent Plugins spec](https://cursor.com/docs/plugins) must load the same skills and server without Cursor variables. Until that host exists, one Cursor Plugin is the package. The sketch of the other manifest lives in `stubs/agent-plugin/` so the two files are not in the same tree.

Keep the current local stdio entrypoint. Phase A repackages it. It does not rewrite t3-lab.

## 3. Where the process runs

Stdio is local: Cursor starts the process ([MCP](https://cursor.com/docs/mcp)). That matches a plugin whose server must sit next to the T3 app on the owner’s machine (the New task dialog runs the task on a named computer, a Mac mini in the screenshot).

Cloud agents also accept stdio, but that process runs **inside the cloud agent VM**, and the agent can see its env. HTTP is the documented recommendation there, because tool calls are proxied and credentials stay off the VM. SSE and `mcp-remote` are not supported for cloud agents ([Cloud Agent capabilities](https://cursor.com/docs/cloud-agent/capabilities)).

Grok Bot is a third place. Its computer is a persistent cloud machine. Plugins in the Grok Bot docs are account connectors: OAuth tokens stay on Cursor’s connector backend, and the bot invokes tools without receiving those tokens ([Work](https://cursor.com/docs/grok-bot/work), [Teams](https://cursor.com/docs/grok-bot/teams)). Local execution on the desktop is a separate policy for shell and files, per-command approval by default ([Security](https://cursor.com/docs/grok-bot/security)). The docs do not say that policy spawns a stdio MCP server on the laptop.

Consequence for parent chats:

| Parent | What can see the stdio server |
| --- | --- |
| Cursor IDE / local agent on the machine that runs T3 | The Cursor Plugin, once the folder is imported |
| Cloud Agent | Only if the server is reachable as HTTP, or if a stdio build actually runs inside that VM. A binary that talks to a T3 app on the Mac mini will not, by itself, run in the VM |
| Grok Bot | Documented path is a connector the team MCP policy allows. A `~/.cursor/plugins/local` folder is not that path. Whether Grok Bot also loads local plugins is unverified; see §8 |

Phase A is still worth doing. It is the package IDE chats and any future host that *does* spawn the plugin will load. It does not, by itself, put the server on the Grok Bot cloud computer.

`${CURSOR_PLUGIN_ROOT}` must appear in the stdio `command` or `args`. A bare `node server/dist/index.js` is resolved against the host’s cwd, not the plugin folder.

`T3_LAB_OWNER` in the stub is the Cursor variable `${T3_LAB_OWNER}`. The MCP page also documents `${env:NAME}` interpolation in `env` values. If a local-only install has no Plugins → Configure field, point `env` at `${env:T3_LAB_OWNER}` and keep exporting the variable the way the current server already expects. Either form is a name in git. Neither form is a value.

## 4. File tree to build in phase A and B

Phase A fills the manifest, skill, and `mcp.json`. Phase B adds the resource and the render tool. The server implementation stays in the existing fork; this tree only shows where the new files sit once that fork is wrapped.

```text
t3-lab-grok/
├── .cursor-plugin/
│   └── plugin.json              # Cursor Plugin manifest. No root plugin.json
├── mcp.json                     # stdio server. Discovered by default
├── skills/
│   └── t3-delegate/
│       └── SKILL.md             # when to call data tools vs show_delegation
├── server/                      # existing t3-lab fork, packaged so the command path is stable
│   └── …                        # unchanged data tools; plus show_delegation in phase B
└── ui/
    └── delegation-card.html     # served as ui://t3-lab/delegation-card
```

As of 0.3.0 that tree is the repository root. The Python server is `server.py` (not a `server/` package). There is no root `plugin.json` and no `stubs/` directory. `ui/delegation-card.html` is the resource, and `show_delegation` is registered in `server.py`.

## 5. MCP Apps: resource and tools

Cursor supports the MCP Apps extension. Tools can return interactive UI in chat. If the host cannot render it, the same tool still works through its normal response ([MCP](https://cursor.com/docs/mcp), [MCP help](https://cursor.com/help/customization/mcp)). The changelog entry that introduced it describes charts, diagrams, and whiteboards inside agent chat ([changelog 2-6](https://cursor.com/changelog/2-6)). Cursor does not restate the wire protocol. The contract below is the MCP Apps spec dated 2026-01-26, which is what a server has to speak. Cursor’s host is not documented field-by-field against that spec; §8 lists the gaps.

Spec sources:

- Overview: [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)
- Wire protocol: [ext-apps `specification/2026-01-26/apps.mdx`](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx)

### Resource

One HTML document:

| Field | Value |
| --- | --- |
| URI | `ui://t3-lab/delegation-card` |
| MIME | `text/html;profile=mcp-app` |
| Body | A single HTML5 document, scripts and CSS inlined |
| `_meta.ui.csp` | Omitted |

The URI must start with `ui://`. The MIME must be `text/html;profile=mcp-app`. Content is HTML5 in `text` or base64 `blob`. The host fetches it with `resources/read` and may preload it from the tool metadata before the tool runs.

Omitting CSP is deliberate. If `ui.csp` is absent the host must apply:

```text
default-src 'none';
script-src 'self' 'unsafe-inline';
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
media-src 'self' data:;
connect-src 'none';
```

The card must not `fetch` T3, a CDN, or `localhost`. Fresh data comes back through the host, as a tool call. The spec also says a web host wraps the view in a sandbox proxy on a different origin, with `allow-scripts` and `allow-same-origin`, and that the view cannot touch the parent DOM. External links do not escape that sandbox by themselves.

### Tool split

`_meta.ui.resourceUri` goes on **one** tool. Data tools stay ordinary MCP tools. A host that understands MCP Apps renders a tool’s result with the linked resource only when that metadata is present. Putting it on `spawn_thread` or `delegation_status` would paint a card for every status poll and would stack iframes.

| Tool | Who can call it | `resourceUri` | Job |
| --- | --- | --- | --- |
| `spawn_thread` | model (`visibility: ["model"]`) | none | Start a T3 thread. Returns ids and whatever URLs the server already returns |
| `await_thread` | model | none | Block for completion when the parent wants a single wait. The card does not call this |
| `delegation_status` | model and app (`["model", "app"]`) | none | Read status. The iframe long-polls this (`waitSeconds` + `since`). No second card |
| `show_delegation` | model and app | `ui://t3-lab/delegation-card` | The only render tool. Returns Markdown plus `structuredContent` |

Visibility defaults to `["model", "app"]` when omitted. A tool without `"model"` is hidden from the agent. A tool without `"app"` must be rejected when the iframe calls it. `spawn_thread` and `await_thread` are model-only so the card cannot start work or hold a long wait. `delegation_status` keeps `"app"` so the open card can refresh. Cross-server app calls are blocked by the spec; the card can call only this server.

`show_delegation` arguments:

```json
{
  "threadId": "string, required",
  "phase": "handoff | update | wrap-up"
}
```

The handler reads the same status the data tools already expose. It does not add a T3 endpoint. Its result has three parts, matching the spec’s best practice:

| Part | Audience | Contents |
| --- | --- | --- |
| `content[]` text | The model, and any host with no app UI | The Markdown digest the plugin already pastes |
| `structuredContent` | The iframe. Not model context | The card model below |
| `_meta` | Diagnostics | Optional timestamp. No secrets, no raw webhook body |

```ts
type DelegationCard = {
  phase: "handoff" | "update" | "wrap-up";
  threadId: string;
  title: string;
  status: "queued" | "running" | "waiting" | "done" | "stopped" | "error";
  digest: string;
  httpsUrl?: string;   // passed through only if a data tool already returned one
  appUrl?: string;     // t3code://threads/<env>/<thread>; the preferred click target (0.3.6)
};
```

Servers should check the client capability `io.modelcontextprotocol/ui` before advertising UI tools. When the capability is missing, still register `show_delegation` as a normal tool that returns `content`. Progressive enhancement is a Cursor requirement and a spec requirement: “If host does not support MCP Apps, tool behaves as standard tool (text-only fallback).”

Sketch of the metadata, and of the data tools without it: [`stubs/cursor-plugin/server/show-delegation.tool.json`](../stubs/cursor-plugin/server/show-delegation.tool.json).

### One card, then updates inside it

The spec renders **the result of the call that carries `resourceUri`**. It does not promise that a later call replaces an earlier iframe. If the model calls `show_delegation` on every poll, the chat fills with cards.

Skill rule:

1. After `spawn_thread` succeeds, call `show_delegation` once with `phase: "handoff"`.
2. Leave `update` refreshes to the iframe. It calls `delegation_status` and re-renders. The model does not poll.
3. Call `show_delegation` again with `phase: "wrap-up"` only when the host never drew the iframe, or the iframe is gone and the parent chat still needs a terminal digest. On a text-only host this second call is just Markdown, which is the desired fallback.
4. `await_thread` remains available when the parent explicitly wants a blocking wait and will quote the text result. It is not the card’s refresh loop.

The iframe listens for `ui/notifications/tool-result` on its own `show_delegation` call, then uses `tools/call` for `delegation_status` with `moment` `check-in`, `waitSeconds: 25`, and `since` set to the last `signature`. That is a server-side long-poll: the server re-reads the thread every ~2 s and returns as soon as the row signature (status, pending ask, error, round) changes, or after 25 s with fresh data (excerpt included); the card renders and waits again immediately. The MCP server handles tool calls concurrently, so a held wait never blocks other calls. If the wait call errors (older server, host timeout), the card falls back to adaptive polling — 3 s during the first minute, then 10 s, then 30 s — and gives up after 5 consecutive errors. The elapsed pill ticks locally every second. Handlers are registered before `ui/initialize` completes. `ui/delegation-card.html` implements that client. It stops at done, stopped or error and switches the visible phase to wrap-up. Waiting means T3 is asking the user something, so the card keeps waiting.

### What the card looks like

The quality bar is the attached chat card: one compact dark unit with an identity row, a short body, a quiet meta line, and a single action. It is not an X post, and it is not a dashboard.

[`stubs/cursor-plugin/ui/delegation-card.html`](../stubs/cursor-plugin/ui/delegation-card.html) shows the three phases as static HTML so the layout can be reviewed without a host. The served resource is that document, plus the small script that binds `structuredContent` and the host bridge. Keep the script inline. Request no camera, microphone, or geolocation. Request `clipboardWrite` only for the copy control, and expect the host to refuse it.

Host context may include `theme`, CSS variables, and `containerDimensions`. Use the host’s text and background variables when `ui/initialize` provides them, so the card matches the chat instead of forcing a second theme. The static sketch uses a dark palette because that is the bar in the screenshot.

## 6. Card phases

The phase is a field on `show_delegation` and on `structuredContent`. The values are `handoff`, `update`, and `wrap-up`; the pre-0.3.7 Spanish values are accepted as deprecated aliases.

| Phase | When it is true | Card | Model’s text |
| --- | --- | --- | --- |
| `handoff` | `spawn_thread` has returned and the thread is queued or running | Title, thread id, “Running”, digest of the assignment, Open control if an https URL exists | Short confirmation plus the same digest |
| `update` | The iframe’s long-poll of `delegation_status` returns non-terminal | Same card, status line and digest replaced in place. No new tool result in the transcript | Nothing. The model is not called |
| `wrap-up` | Status is done, stopped or error | Same card if it is still mounted: final digest, status “Done”, “Stopped” or “Error”, waiting stopped. A new `show_delegation` only in the fallback cases in §5 | Terminal Markdown digest |

Empty, in progress, and error are visual states inside those phases, not extra tools:

- **Empty** — `show_delegation` called without a thread id. The tool returns an error string in `content`. No iframe data to invent.
- **In progress** — `handoff` or `update` with `status: "queued"`, `"running"` or `"waiting"`. The meta line shows the last status word the server returned. There is no spinner protocol in MCP Apps; the HTML can pulse a dot.
- **Error** — `status: "error"` at `wrap-up`, or a failed `tools/call`. The body is the server’s error text. The Open control stays only when `httpsUrl` is still present.

The digest is a short excerpt. Do not put a full T3 transcript, a webhook body, or `T3_LAB_OWNER` into `structuredContent`. Grok Bot action recording stores connector tool calls ([Teams](https://cursor.com/docs/grok-bot/teams)). Assume tool payloads can be logged.

## 7. Open in T3

Three mechanisms, in the order the card should try them. The server never manufactures a URL. It only forwards a string a data tool already returned.

**0.3.6 change:** `t3code://` is now the primary link and https is secondary. Testing showed the https web origin lands on T3's `/pair` page in any browser not already paired, while `t3code://threads/<env>/<thread>` opens the right thread in T3 mobile (remote environments included). The sections below describe the current order.

### t3code:// — the primary link

The Markdown digest leads with `[open in T3 ↗](t3code://threads/<env>/<thread>)` and repeats the URI as a code span for copying, because some chat surfaces strip custom-scheme links (see §1). The card's Open button sends `appUrl` to `ui/open-link` when the host reports `openLinks`. If the host refuses, the card shows the URI with Copy and a hint to paste it into Safari or T3.

### https web origin — secondary

When a target has an `https` web origin, the digest adds `[web (needs paired browser) ↗](…)` after the app link. An `http` origin (local) is shown as `[local ↗](…)`. The card only opens the web URL when there is no `appUrl`.

Inside the iframe, a plain `<a href>` does not navigate the chat. The sandbox exists so the view cannot navigate the parent. The button sends the MCP Apps request `ui/open-link` with `{ "url": "<url>" }` after `ui/initialize` reports `hostCapabilities.openLinks`. The spec says the host should open that URL in the default browser or a new tab, and that it may deny it (`Invalid URL`, `Policy violation`, user denial). Cursor’s docs do not say that Cursor or Grok Bot set `openLinks`. The button checks the capability and, if it is absent, shows the `https` URL as selectable text. The Markdown `content` already contains the same link, which is what a text-only host displays.

Do not add a `connectDomains` entry in order to `window.open` from the iframe. `connect-src` is for fetch, not for handing a URL to the host, and the default CSP is `connect-src 'none'`.

### Host bridge — ui/open-link only

There is no documented Cursor or Grok Bot bridge that focuses a local T3 window. Documented Cursor deeplinks are prompt, command, rule, and MCP-server install. They require a person to confirm, and they never run on their own ([Deeplinks](https://cursor.com/docs/reference/deeplinks), [MCP install links](https://cursor.com/docs/mcp/install-links)). None of them opens a T3 thread.

The plugin does not invent a private method next to `ui/open-link`. When `ui/initialize` reports `hostCapabilities.openLinks`, the card sends `appUrl` (`t3code://`) through `ui/open-link`; without that capability, or if the host rejects the request, it shows the `t3code://` URI with Copy instead. The capability check fails closed.

```text
appUrl and openLinks       → "Open app" calls ui/open-link with t3code://; Copy beside it
appUrl and no openLinks    → t3code:// code line plus Copy; Markdown already links it
open-link rejected         → t3code:// code line plus Copy and a paste hint
httpsUrl only              → https takes the same slots
neither                    → no open control; digest still renders
```

## 8. Grok Bot gaps and risks

Grok Bot documents connectors, approvals, and file or link cards. It does not document MCP Apps. Design the server so a Grok Bot parent chat that only sees text still gets the digest it gets today. Treat a rendered iframe there as an experiment, not as a dependency.

### Local plugin vs connector

- “Plugins” in [Work with Grok Bot](https://cursor.com/docs/grok-bot/work) are services such as Gmail, Notion, and Slack. Install is a Connect card, then a browser login ([Connect plugins](https://cursor.com/help/grok-bot/connect-plugins)). Tokens stay on the connector backend.
- The same team MCP allowlist and blocklist apply. There is no separate Grok Bot connector list. Blocking a connector shows “Disabled by team admin.” Pushing a connector as mandatory or default-on is **not** available ([Teams](https://cursor.com/docs/grok-bot/teams)). IDE team plugins can be Default Off, Default On, or Required ([Plugins](https://cursor.com/docs/plugins)). Those modes do not transfer.
- Settings → Plugins has Marketplace and Yours, and individual tools can be toggled ([Settings](https://cursor.com/docs/grok-bot/settings)). That is the documented place to look. It is not documented as a loader for `~/.cursor/plugins/local`.
- The computers bots work on run in Cursor’s cloud ([Grok Bot](https://cursor.com/docs/grok-bot)). A stdio server that has to speak to a T3 app on a Mac mini is on the other side of that split. Desktop local execution does not close the gap; it is an approval policy for commands and files ([Security](https://cursor.com/docs/grok-bot/security)).

Risk: phase A succeeds in the IDE and the Grok Bot parent still has no `spawn_thread`. Closing that gap means a connector the bot can call (HTTP MCP that the backend proxies, per the cloud-agent recommendation) whose other end can reach the machine running T3. This document does not design that tunnel and does not invent a T3 port.

### Apps rendering

Cursor’s MCP page says apps render “in chat” and that a host which cannot render UI falls back to the normal tool response. It does not list IDE chat, the Agents Window, cloud agents, the CLI, or Grok Bot separately.

Grok Bot UI that *is* documented:

- An in-chat Connect card for authorization.
- “Generated files and links appear as cards you can preview, save, or answer with feedback” ([Work](https://cursor.com/docs/grok-bot/work)). Those are file and link cards, not an MCP Apps iframe.

Risk: `_meta.ui.resourceUri` is ignored. Mitigation is already the `content` string. Verify once, on a real Grok Bot turn, before describing the card as supported there. A failed render that still shows the digest is the success criterion for that test.

Canvases are a different feature (agent-built views beside the chat, and prebuilt plugin canvases in Customize). They are not this card.

### Deeplinks and the custom scheme

Cursor documents `cursor://anysphere.cursor-deeplink/…` for prompts, commands, rules, and MCP install. It does not document which schemes a chat message will turn into a control. The owner’s `t3code://` failure stands as an observed sanitizer until Cursor writes the rule down. The Open control in §7 is the response to that observation.

`ui/open-link` can also be refused per host. The overview says a host might disable the open-link capability. The button must survive that refusal.

### Approvals, logs, and name shadowing

- Cursor asks before MCP tools run, and Auto-review can let an allowlisted tool through ([MCP](https://cursor.com/docs/mcp)). Grok Bot Auto Review covers plugin calls ([Security](https://cursor.com/docs/grok-bot/security)). A card that polls `delegation_status` can raise a prompt on every tick if the tool is not allowlisted. The poll interval belongs to the iframe, should be slow (tens of seconds, not a tight loop), and should stop at `wrap-up`. The plugin cannot bypass the approval.
- Recorded connector calls can retain arguments and results. Digests stay short and free of owner tokens and webhook bodies.
- Publishing a marketplace plugin named `t3-lab-grok` would override the local folder. Do not publish until the owner explicitly decides to list it.
- Marketplace review expects open source and largely markdown plus skill support files, with no binaries ([marketplace security](https://cursor.com/help/security-and-privacy/marketplace-security)). A stdio binary next to T3 is a local plugin, which is another reason to keep this off the marketplace.

## 9. Phase C — webhook wake, without a T3 API

The New task screenshot is T3 receiving work, not T3 calling out.

What the dialog shows, and all this design is willing to claim:

- Schedule mode **On webhook**. The URL appears after save.
- The prompt template may name `{{body.path}}`, `{{headers.name}}`, `{{query.name}}`, `{{body}}`, or `{{request}}`. Only named parts reach the agent.
- Optional “skip requests older than” a number of minutes.
- Optional reject when the body has no valid HMAC-SHA256 signature.
- The task runs on a selected computer, in a workspace, from a base branch.

That is an inbound trigger. It can start a T3 thread when some sender POSTs to T3. It does not say T3 will POST anywhere when a thread finishes. Phase C does not invent that sender, the path, the signature header name, or the JSON shape.

Documented wakes on the Cursor side, if an outbound sender appears later:

| Mechanism | Direction | What a 2xx means |
| --- | --- | --- |
| [Grok Bot routine webhook](https://cursor.com/help/grok-bot/routines) | Caller POSTs to the routine URL with `Authorization: Bearer <key>`. The bot receives the JSON body with the routine instruction | 200 means the call was accepted and a run started. It does not mean the bot finished |
| [Cursor Automations](https://cursor.com/docs/cloud-agent/automations) | POST to a private endpoint starts a cloud agent run. An MCP server can be one of that agent’s tools | A started run, not a finished one |
| [Cloud Agent API webhooks](https://cursor.com/docs/cloud-agent/api/webhooks) | Cursor POSTs `statusChange` (`ERROR` or `FINISHED`) to a URL **you** set when creating an agent | Notification only. This does not start an agent |

A routine or automation could, once something is allowed to call it, instruct the bot to call `delegation_status` / `show_delegation` for a thread id that was in the JSON body. That instruction is ordinary prompt text. It is not a new tool. The HMAC toggle and the skip-old field stay T3’s inbound controls; this plugin does not store the signing secret.

Cloud Agent subscriptions (GitHub, Slack, Linear, timers, 180 days) are another documented wake, and they are not a T3 channel ([Capabilities](https://cursor.com/docs/cloud-agent/capabilities)).

Until T3 documents outbound delivery, phase C is a parking lot. Shipping A and B does not depend on it.

## 10. Parent-chat contract

The `t3-delegate` skill is the only agent-facing policy:

- Call the data tools for real work. Call `show_delegation` only at the moments in §5.
- Quote `content` when the turn has no iframe. Do not describe a card the host did not draw.
- Paste the digest as is: its primary link is `t3code://`, and the https web link (when present) needs a paired browser.
- Do not ask the user to paste `T3_LAB_OWNER` into the chat.
- Do not call a T3 webhook, and do not invent a status callback.

The skill sketch is [`stubs/cursor-plugin/skills/t3-delegate/SKILL.md`](../stubs/cursor-plugin/skills/t3-delegate/SKILL.md).

## 11. Sources

Cursor:

- https://cursor.com/docs/plugins
- https://cursor.com/docs/reference/plugins
- https://cursor.com/docs/mcp
- https://cursor.com/help/customization/mcp
- https://cursor.com/changelog/2-6
- https://cursor.com/docs/reference/deeplinks
- https://cursor.com/docs/mcp/install-links
- https://cursor.com/docs/cloud-agent/capabilities
- https://cursor.com/docs/cloud-agent/api/webhooks
- https://cursor.com/docs/cloud-agent/automations
- https://cursor.com/docs/grok-bot
- https://cursor.com/docs/grok-bot/work
- https://cursor.com/docs/grok-bot/teams
- https://cursor.com/docs/grok-bot/security
- https://cursor.com/docs/grok-bot/settings
- https://cursor.com/help/grok-bot/connect-plugins
- https://cursor.com/help/grok-bot/routines
- https://cursor.com/help/security-and-privacy/marketplace-security

MCP Apps:

- https://modelcontextprotocol.io/extensions/apps/overview
- https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
