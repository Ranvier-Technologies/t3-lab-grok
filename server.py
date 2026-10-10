import asyncio
import json
import os
from pathlib import Path

import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from domain import MOMENT_ALIASES, MOMENTS, PHASES, canonical_moment, delegation_card, resolve_owner
from engine import Engine


TEXT = {"type": "string", "minLength": 1}
KEY = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
       "description": "Stable key for this exact logical operation in this chat. Keep it and all arguments unchanged when recovering. Use a new key for a new task or round."}
THREAD = {"type": "string", "pattern": "^[A-Fa-f0-9-]{36}$"}
OWNER = {"type": "string", "pattern": "^[A-Fa-f0-9-]{36}$",
         "description": "Optional chat-owner UUID when the host does not supply _meta.threadId (Grok Bot / Cursor Agent). Prefer the T3_LAB_OWNER environment variable (Cursor Plugin variable ${T3_LAB_OWNER})."}
TARGET = {"type": "string", "description": "Target id from list_targets (its `id`, e.g. mac), not an environment UUID or a provider. Unknown destinations never fall back."}
CHECKOUT = {"type": "string", "description": "This parent chat's actual absolute Git checkout. Needed for local launches in a Git project; omit for remote targets and for scratch. The plugin cache is not your checkout."}
ALIAS_NOTE = " Spanish values from before 0.3.7 (encargo, aviso, seguimiento, cierre) are accepted as deprecated aliases."
MOMENT = {"enum": [*MOMENTS, *MOMENT_ALIASES], "description": "handoff after launch, update only for meaningful attention, check-in when the user asks, wrap-up after synthesis." + ALIAS_NOTE}
PHASE = {"enum": [*PHASES, *(alias for alias, value in MOMENT_ALIASES.items() if value in PHASES)], "description": "handoff after spawn_thread; wrap-up only if no card rendered." + ALIAS_NOTE}
MODEL = {"type": "object", "properties": {"instanceId": TEXT, "model": TEXT}, "required": ["instanceId", "model"], "additionalProperties": False}
SCHEMAS = {
    "list_targets": ({}, []),
    "list_providers": ({"target": TARGET, "owner": OWNER}, []),
    "list_threads": ({"owner": OWNER}, []),
    "list_projects": ({"target": TARGET, "owner": OWNER}, []),
    "add_project": ({"target": TARGET, "requestKey": KEY, "workspaceRoot": {"type": "string", "minLength": 1, "maxLength": 4096, "description": "Absolute path of an existing folder on the target machine."},
                     "title": {"type": "string", "maxLength": 200, "description": "Defaults to the folder name."}, "owner": OWNER}, ["requestKey", "workspaceRoot"]),
    "spawn_thread": ({"target": TARGET, "projectId": {**TEXT, "description": "Project id from list_projects."},
                      "projectTitle": {**TEXT, "description": "Exact project title (case-insensitive), instead of projectId."}, "checkout": CHECKOUT, "requestKey": KEY, "workspace": {"enum": ["shared", "worktree"]},
                      "prompt": {"type": "string", "minLength": 1, "maxLength": 100000}, "title": {"type": "string", "maxLength": 200}, "modelSelection": MODEL,
                      "runtimeMode": {"enum": ["approval-required", "auto-accept-edits", "auto", "full-access"]}, "owner": OWNER}, ["requestKey", "workspace", "prompt", "modelSelection"]),
    "send_to_thread": ({"threadId": THREAD, "requestKey": KEY, "message": {"type": "string", "minLength": 1, "maxLength": 100000}, "owner": OWNER}, ["threadId", "requestKey", "message"]),
    "read_thread": ({"threadId": THREAD, "maxChars": {"type": "integer", "minimum": 100, "maximum": 20000}, "offset": {"type": "integer", "minimum": 0, "description": "Character offset into the reply, to page past maxChars (default 0 = head)."}, "owner": OWNER}, ["threadId"]),
    "await_thread": ({"threadId": THREAD, "maxChars": {"type": "integer", "minimum": 100, "maximum": 20000}, "offset": {"type": "integer", "minimum": 0, "description": "Character offset into the reply, to page past maxChars (default 0 = head)."}, 
                      "timeoutSeconds": {"type": "integer", "minimum": 1, "maximum": 45}, "owner": OWNER}, ["threadId"]),
    "delegation_status": ({"threadIds": {"type": "array", "items": THREAD, "minItems": 1, "maxItems": 10, "uniqueItems": True},
                           "moment": MOMENT, "owner": OWNER,
                           "waitSeconds": {"type": "integer", "minimum": 1, "maximum": 45,
                                           "description": "Card long-poll: hold up to this many seconds until a row's signature differs from `since`."},
                           "since": {"type": "string", "maxLength": 4000, "description": "The `signature` from the previous result or card."}}, ["threadIds", "moment"]),
    "show_delegation": ({"threadId": THREAD, "phase": PHASE, "owner": OWNER}, ["threadId", "phase"]),
}
CARD_URI = "ui://t3-lab/delegation-card"
CARD_MIME = "text/html;profile=mcp-app"
CARD_PATH = Path(__file__).resolve().parent / "ui" / "delegation-card.html"
DESCRIPTIONS = {
    "list_targets": "List configured official Nightly environments without connecting. Configured is not proof of availability. Pairing uses the local setup CLI, never tokens in tool arguments.",
    "list_providers": "Read the selected environment's current providers and exact models. Call before launch; never silently substitute a requested provider/model.",
    "list_projects": "List every project in the target environment's live T3 snapshot: id, title, workspaceRoot, and whether it is the target's default. Any of them can be passed to spawn_thread.",
    "add_project": "Register an existing folder on the target machine as a T3 project (projects.mutate project.create; the folder is never created). Idempotent: an already-registered root returns that project with created=false, and a retry with the same requestKey resends the same command. Returns the project.",
    "list_threads": "List only operations this parent chat owns, including pending/unconfirmed requests. Recover by recorded requestKey, not by title/date or another chat's history.",
    "spawn_thread": "Launch one ordinary T3 thread on the selected official environment. It belongs to this parent chat's local journal, not a fabricated T3 parent. Choose the project with projectId or projectTitle (see list_projects). Otherwise a local checkout's Git project, the default set by setup.py bind, or T3's scratch \"No project\" is used; the response names the resolved project (resolvedBy) and warns on a fallback. The response also carries the handoff digest to paste. Pass the actual local checkout for local targets; remote targets run in the project root with workspace shared. runtimeMode overrides the target default. shared sees uncommitted work; worktree starts at the current local HEAD commit. requestKey and exact payload persist before sending, so recovery uses the same command/thread/message IDs. An unconfirmed outcome is unknown, not failure; do not launch a replacement.",
    "send_to_thread": "Continue an owned settled thread on the same provider/model. Use a new stable requestKey for this round. Questions/approvals must be answered in T3; this tool cannot approve. An unconfirmed send hides the old answer until its exact message is observed. Recover with the same requestKey/arguments, never a new message ID.",
    "read_thread": "Read the latest round of a thread this chat owns, with current state and response. Long replies return the head with truncated and totalChars; page with offset. No previous-round reply is returned for an unconfirmed send. Not found/unreadable data is unknown; never switch environments to resolve it.",
    "await_thread": "Wait at most 45 seconds for the owned thread to settle or need the user. Timeout does not cancel or fail it. Await again on the same ID; background children mean work remains. No automatic idle-chat wake is promised.",
    "delegation_status": "Read only this delegation's owned IDs (any mix of targets) and return a compact Markdown digest, one section per environment. Paste digest verbatim as normal assistant text, not a code block; write your synthesis separately. handoff after launch, update only for meaningful attention, check-in when asked, wrap-up after synthesis. changed=false means write nothing. Links are server-identity-specific; never build them yourself. The delegation card long-polls this (moment check-in, waitSeconds, since); do not poll it yourself while that card is visible and do not pass waitSeconds from chat.",
    "show_delegation": "Render one delegation card for an owned thread. Call once after spawn_thread with phase handoff. Always surface the tool content (Markdown digest) in the chat as the visible trail — even when an MCP App iframe mounts in the tool-result panel; tell the user to expand show_delegation there (like tldraw). The iframe polls delegation_status and updates in place; do not call this for update. Call again with phase wrap-up only if no card rendered. Do not invent links.",
}


def describe_validation_error(error):
    """Name the offending field without echoing any submitted value."""
    path = ".".join(str(part) for part in error.absolute_path) or "(arguments)"
    if error.validator == "required":
        missing = [name for name in error.validator_value if name not in (error.instance or {})]
        return f"missing required field {', '.join(missing)} in {path}"
    if error.validator == "additionalProperties":
        allowed = set((error.schema or {}).get("properties", {}))
        extra = sorted(name for name in (error.instance or {}) if name not in allowed)
        return f"unexpected field {', '.join(extra)} in {path} (allowed: {', '.join(sorted(allowed))})"
    return f"{error.validator} failed for {path}"


def build_server(config_root=None, state_root=None):
    config_root = config_root or os.environ.get("T3_LAB_V2_CONFIG_DIR") or Path.home() / ".config/t3-lab/v2"
    state_root = state_root or os.environ.get("T3_LAB_V2_STATE_DIR") or Path.home() / ".local/state/t3-lab-v2/chats"
    engine = Engine(config_root, state_root)
    app = Server("t3-lab-grok", version="0.4.1")
    definitions = {name: {"type": "object", "properties": properties, "required": required, "additionalProperties": False}
                   for name, (properties, required) in SCHEMAS.items()}

    def tool_definition(name, schema):
        writing = name in {"spawn_thread", "send_to_thread", "add_project"}
        payload = {"name": name, "description": DESCRIPTIONS[name], "inputSchema": schema,
                   "annotations": {"readOnlyHint": not writing, "destructiveHint": writing, "openWorldHint": writing, "idempotentHint": True}}
        if name == "show_delegation":
            payload["_meta"] = {
                "ui": {"resourceUri": CARD_URI, "visibility": ["model", "app"]},
                "ui/resourceUri": CARD_URI,
            }
        return types.Tool.model_validate(payload)

    @app.list_tools()
    async def list_tools():
        return [tool_definition(name, schema) for name, schema in definitions.items()]

    @app.list_resources()
    async def list_resources():
        return [types.Resource.model_validate({
            "name": "delegation-card",
            "uri": CARD_URI,
            "description": "Inline delegation card for one T3 thread. Sandboxed HTML; no network.",
            "mimeType": CARD_MIME,
        })]

    @app.read_resource()
    async def read_resource(uri):
        if str(uri) != CARD_URI:
            raise ValueError("Unknown resource.")
        return [ReadResourceContents(content=CARD_PATH.read_text(encoding="utf-8"), mime_type=CARD_MIME)]

    @app.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        try:
            if name not in definitions:
                raise ValueError("Unknown plugin tool.")
            try:
                jsonschema.validate(arguments, definitions[name])
            except jsonschema.ValidationError as error:
                raise ValueError(f"Invalid tool input: {describe_validation_error(error)}. No values were echoed and no call was sent.") from None
            if engine.credentials.redact(arguments) != arguments:
                raise ValueError("Tool input contains a configured credential. It was not stored or sent.")
            try:
                meta = app.request_context.meta
            except LookupError:
                meta = None
            owner = resolve_owner(meta.model_dump(mode="json") if meta else {}, arguments, owner_root=config_root) if name != "list_targets" else None
            if name == "show_delegation":
                arguments = {**arguments, "phase": canonical_moment(arguments["phase"], PHASES)}
                payload = {"threadIds": [arguments["threadId"]], "moment": arguments["phase"]}
                if arguments.get("owner"):
                    payload["owner"] = arguments["owner"]
                data = engine.credentials.redact(await engine.call("delegation_status", payload, owner))
                row = next((item for item in data.get("rows", []) if item.get("threadId") == arguments["threadId"]), None)
                if row is None:
                    raise ValueError("That thread has no status row. No card was invented.")
                card = delegation_card(row, arguments["phase"], data.get("digest"))
                return types.CallToolResult.model_validate({
                    "content": [{"type": "text", "text": data.get("digest") or ""}],
                    "structuredContent": card,
                    "_meta": {
                        "phase": arguments["phase"],
                        "ui": {"resourceUri": CARD_URI, "visibility": ["model", "app"]},
                    },
                    "isError": False,
                })
            data = engine.credentials.redact(await engine.call(name, arguments, owner))
            text = data.get("digest") if name == "delegation_status" else json.dumps(data, ensure_ascii=False)
            return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structuredContent=data)
        except Exception as error:
            message = engine.credentials.redact(str(error))
            return types.CallToolResult(content=[types.TextContent(type="text", text=message)], isError=True)
    return app


async def main():
    app = build_server()
    async with stdio_server() as (reader, writer):
        await app.run(
            reader,
            writer,
            app.create_initialization_options(
                experimental_capabilities={
                    "io.modelcontextprotocol/ui": {"mimeTypes": [CARD_MIME]},
                }
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
