import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote


TERMINAL = {"completed", "failed", "cancelled", "interrupted", "rolled_back"}
ACTIVE = {"pending", "queued", "preparing", "starting", "running", "waiting"}
LABELS = {"starting": "Starting", "running": "Working", "background": "Turn done, still working",
          "completed": "Turn done", "failed": "Failed", "stopped": "Stopped", "unknown": "No data",
          "waiting_for_input": "Waiting for your reply", "waiting_for_approval": "Waiting for your approval"}


# A sentence ends at . ! or ? before whitespace, or glued straight to a capital
# (ACP providers stream one growing message: "…the ADR.This time…").
SENTENCE_END = re.compile(r"(?<=[.!?])(?:\s+|(?=[A-ZÀ-Þ¿¡]))")
EXCERPT = 200


MOMENTS = ("handoff", "update", "check-in", "wrap-up")
PHASES = ("handoff", "update", "wrap-up")
# Deprecated Spanish values accepted from callers built before 0.3.7.
MOMENT_ALIASES = {"encargo": "handoff", "aviso": "update", "seguimiento": "check-in", "cierre": "wrap-up"}
HEADINGS = {"handoff": "Handoff", "update": "Update", "check-in": "Check-in", "wrap-up": "Wrap-up"}


def canonical_moment(value, allowed=MOMENTS):
    value = MOMENT_ALIASES.get(value, value)
    if value not in allowed:
        raise ValueError("must be one of " + ", ".join(allowed) + ".")
    return value


def excerpt(text, finished):
    """Head of a finished reply, or the last sentence of one still being written."""
    text = " ".join(str(text or "").split())
    if not finished and text:
        text = SENTENCE_END.split(text)[-1]
    if len(text) <= EXCERPT:
        return text or None
    if finished:
        head = text[:EXCERPT - 1]
        if text[EXCERPT - 1] != " " and " " in head:
            head = head.rsplit(" ", 1)[0]
        return head.rstrip(" ,;:") + "…"
    tail = text[1 - EXCERPT:]
    if text[-EXCERPT] != " " and " " in tail:
        tail = tail.split(" ", 1)[1]
    return "…" + tail.lstrip()


def ask(request, items):
    """What a pending request asks. The text lives in the turn item that names the
    request; an approval's command sits in the command_execution sharing its nativeId."""
    item = next((item for item in items if item.get("requestId") == request.get("id")), {})
    text = None
    if item.get("type") == "user_input_request":
        questions = item.get("questions") or []
        if questions and questions[0].get("question"):
            text = questions[0]["question"] + (f" (+{len(questions) - 1} more)" if len(questions) > 1 else "")
    elif item.get("type") == "approval_request":
        native = (item.get("nativeItemRef") or {}).get("nativeId")
        command = next((other.get("input") for other in items if native and other.get("type") == "command_execution"
                        and (other.get("nativeItemRef") or {}).get("nativeId") == native), None)
        prompt = item.get("prompt")
        text = command + (" · " + prompt if prompt and prompt != command else "") if command else prompt or item.get("title")
    data = {"kind": item.get("requestKind") or request.get("kind")}
    text = excerpt(text, True)
    if text:
        data["text"] = text
    return data


def observe(operation, projection):
    data = {"threadId": operation["thread_id"], "status": "unknown", "settled": False,
            "lastAssistantMessage": None, "latestAssistantText": None, "lastError": None, "round": operation.get("round", 1),
            "operationState": operation["state"], "runId": None, "deliveryConfirmed": False}
    if projection.get("thread", {}).get("id") != operation["thread_id"]:
        raise ValueError("The server returned a different thread; its result was not used.")
    runs = projection.get("runs", [])
    run = next((run for run in runs if run.get("userMessageId") == operation["message_id"]), None)
    if run is None:
        return data
    run = max((candidate for candidate in runs if candidate.get("ordinal", 0) >= run.get("ordinal", 0)),
              key=lambda candidate: candidate.get("ordinal", 0))
    data.update(runId=run["id"], deliveryConfirmed=True)
    run_id = run["id"]
    messages = [message for message in projection.get("messages", []) if message.get("role") == "assistant" and message.get("runId") == run_id]
    data["latestAssistantText"] = "\n".join(message.get("text", "") for message in messages) or None
    started = run.get("workStartedAt") or run.get("startedAt") or run.get("requestedAt")
    data.update({key: value for key, value in (("startedAt", started), ("completedAt", run.get("completedAt"))) if value})
    nodes = {node["id"] for node in projection.get("nodes", []) if node.get("runId") == run_id}
    pending = [request for request in projection.get("runtimeRequests", [])
               if request.get("nodeId") in nodes and request.get("status") == "pending"]
    status = run.get("status")
    live_children = any(child.get("runId") == run_id and child.get("status") in ACTIVE
                        for child in projection.get("subagents", []))
    if pending:
        data.update(status="waiting_for_input" if pending[0].get("kind") in {"user_input", "auth_refresh"} else "waiting_for_approval", settled=True,
                    ask=ask(pending[0], projection.get("turnItems", [])))
    elif status == "completed" and live_children:
        data["status"] = "background"
    else:
        data["status"] = {"queued": "starting", "preparing": "starting", "running": "running", "waiting": "running", "completed": "completed",
                          "failed": "failed", "cancelled": "stopped", "interrupted": "stopped", "rolled_back": "stopped"}.get(status, "unknown")
        if status == "waiting":
            data["notice"] = "T3 is closing the turn; the text so far does not confirm it finished."
        data["settled"] = status in TERMINAL
        if data["settled"]:
            data["lastAssistantMessage"] = data["latestAssistantText"]
            if status == "failed":
                data["lastError"] = "Reason unavailable"
    if messages:
        newest = sorted(messages, key=lambda message: message.get("createdAt") or "")[-1]
        text = excerpt(newest.get("text"), data["status"] in {"completed", "failed", "stopped"})
        if text:
            data["excerpt"] = text
    return data


def git_value(checkout, *arguments):
    directory = Path(checkout)
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("checkout must be an existing absolute directory. No call was sent.")
    env = {key: value for key, value in os.environ.items() if key not in {"GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"}}
    try:
        return subprocess.run(["git", "-C", str(directory), *arguments], check=True, capture_output=True, text=True, timeout=5, env=env).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise ValueError("The checkout is not a Git repository or Git could not inspect it. No call was sent.") from None


def common_dir(checkout):
    return str(Path(git_value(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())


def resolve_project(projects, checkout, project_id=None):
    identity = common_dir(checkout)
    matched = []
    for project in projects:
        try:
            if common_dir(project["workspaceRoot"]) == identity:
                matched.append(project)
        except (ValueError, KeyError):
            continue
    if project_id is not None:
        matched = [project for project in matched if project["id"] == project_id]
    if len(matched) > 1:
        choices = ", ".join(sorted(project["id"] for project in matched))
        raise ValueError(f"Multiple T3 projects match this Git repository. Pass an explicit projectId from [{choices}]; no project or thread was created.")
    if len(matched) != 1:
        raise ValueError("No unique configured T3 project matches this checkout. Choose a project explicitly or create it in T3; no project or thread was created.")
    return matched[0]


def owner_from_meta(meta):
    """Codex path: host _meta.threadId only (fail closed if missing/invalid)."""
    try:
        return str(uuid.UUID(meta["threadId"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("This plugin needs the Codex host's threadId metadata. No conversation was inferred from a process, session or tool argument; no mutation was sent.") from None


def config_dir(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("T3_LAB_V2_CONFIG_DIR") or Path.home() / ".config/t3-lab/v2")


def read_owner_file(root):
    """Owner UUID written by `setup.py init`; ignored unless private and not a symlink."""
    file = Path(root) / "owner"
    try:
        if file.is_symlink() or not file.is_file() or file.stat().st_mode & 0o077:
            return None
        return str(uuid.UUID(file.read_text().strip()))
    except (OSError, ValueError):
        return None


def resolve_owner(meta=None, arguments=None, environ=None, owner_root=None):
    """Fail-closed chat owner for parent hosts.

    Order: valid _meta.threadId (Codex) → tool arg owner → env T3_LAB_OWNER →
    the private owner file written by `setup.py init` in the config dir.
    Grok Bot / Cursor Agent do not inject threadId. Cursor sets T3_LAB_OWNER
    from Plugins → Configure via ${T3_LAB_OWNER} in mcp.json.
    """
    meta = meta or {}
    arguments = arguments or {}
    environ = os.environ if environ is None else environ
    try:
        return owner_from_meta(meta)
    except ValueError:
        pass
    for value in (arguments.get("owner"), environ.get("T3_LAB_OWNER")):
        if value is None:
            continue
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            continue
    owner = read_owner_file(owner_root if owner_root is not None else config_dir(environ))
    if owner:
        return owner
    raise ValueError(
        "This plugin needs a chat owner UUID. Codex supplies _meta.threadId; "
        "Grok Bot / Cursor Agent need env T3_LAB_OWNER, an owner tool argument, "
        "or the owner file from `setup.py init`. "
        "No conversation was inferred from a process or session; no mutation was sent."
    )


def plain(value):
    return re.sub(r"([\\`*_\[\]<>#|~])", r"\\\1", " ".join(str(value).split()))


def inline_code(value):
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    fence = "`" * (max((len(run) for run in re.findall("`+", text)), default=0) + 1)
    return fence + " " + text + " " + fence


CARD_STATUS = {"starting": "queued", "unknown": "queued", "running": "running", "background": "running",
               "waiting_for_input": "waiting", "waiting_for_approval": "waiting", "completed": "done",
               "stopped": "stopped", "failed": "error"}


def thread_urls(row):
    """App URI for display, plus an http(s) page only when a data row already has a web origin."""
    thread = quote(str(row.get("threadId") or ""), safe="")
    environment = quote(str(row.get("environment") or ""), safe="")
    app = f"t3code://threads/{environment}/{thread}" if thread and environment else None
    origin = str(row.get("webOrigin") or "").strip().rstrip("/")
    if not origin or any(mark in origin for mark in " \t\r\n@\\"):
        return app, None, None
    if origin.startswith("https://"):
        scheme = "https"
    elif origin.startswith("http://"):
        scheme = "http"
    else:
        return app, None, None
    if "://" in origin[len(scheme) + 3:]:
        return app, None, None
    return app, f"{origin}/{environment}/{thread}", scheme


def link_line(row):
    """Bare t3code:// deep link in its own fenced block, so a phone can copy it.

    Chat clients (Grok Bot iOS) do not open t3code:// when tapped; copying into Safari does.
    No web origin here: hostnames and tailnet origins never go into the text digest.
    """
    app, _web, _scheme = thread_urls(row)
    return f"```\n{app}\n```" if app else ""


def row_signature(row):
    """What the card waits on: status, the pending ask, the error, and the round."""
    ask = row.get("ask") if isinstance(row.get("ask"), dict) else {}
    fields = [row.get("status"), ask.get("kind"), ask.get("text"), row.get("lastError"), row.get("round")]
    # Fixed-length and unambiguous, so it always fits the `since` schema whatever the error text.
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, default=str).encode()).hexdigest()[:32]



def delegation_card(row, phase, digest_text):
    """Card model for the iframe. No transcript, owner id, or invented URL."""
    try:
        phase = canonical_moment(phase, PHASES)
    except ValueError as error:
        raise ValueError("phase " + str(error)) from None
    text = str(digest_text or "")
    if len(text) > 800:
        text = text[:800].rstrip() + "…"
    app, web, scheme = thread_urls(row)
    card = {"phase": phase, "threadId": row["threadId"], "title": row.get("title") or "Task",
            "status": CARD_STATUS.get(row.get("status"), "queued"), "digest": text}
    if scheme == "https" and web:
        card["httpsUrl"] = web
    elif scheme == "http" and web:
        card["webUrl"] = web
    if app:
        card["appUrl"] = app
    card.update({key: row[key] for key in ("driver", "model", "startedAt", "completedAt", "excerpt") if isinstance(row.get(key), str) and row[key]})
    if isinstance(row.get("ask"), dict):
        card["ask"] = row["ask"]
    card["signature"] = row_signature(row)
    return card


def digest(rows, moment, observed_at):
    try:
        moment = canonical_moment(moment)
    except ValueError as error:
        raise ValueError("moment " + str(error)) from None
    heading = HEADINGS[moment]
    def where(row):
        """Project and target name only: no checkout paths, hostnames, or origins."""
        return " · ".join(plain(part) for part in (row.get("project"), row.get("target")) if part)
    locations = {where(row) for row in rows}
    location = where(rows[0]) if len(locations) == 1 else "several projects or targets"
    observed = "seen " + datetime.fromtimestamp(observed_at).strftime('%H:%M') if observed_at is not None else "no observation yet"
    # No "expand the card" hint: the server cannot tell whether the host renders MCP Apps cards,
    # and the card works on its own.
    lines = [" · ".join(bit for bit in (f"**{heading}**", location, observed) if bit)]
    attention = [f"#{row['n']} {LABELS[row['status']].lower()}" for row in rows if row["status"] in {"failed", "waiting_for_input", "waiting_for_approval"}]
    if attention:
        lines.append("Needs your attention: " + " · ".join(attention))
    for row in rows:
        lines.append(f"**#{row['n']} · {LABELS[row['status']]} · {plain(row['title'])}" + (f" · round {row['round']}" if row["round"] > 1 else "") + "**")
        thread = quote(row["threadId"], safe="")
        detail = [plain(row['provider']), inline_code(row['model']), thread[:8]]
        if len(locations) > 1:
            detail.append(where(row))
        lines.append(" · ".join(bit for bit in detail if bit))
        if row["status"] == "failed":
            lines.append(inline_code(row.get("lastError")))
        if row.get("notice"):
            lines.append(inline_code(row["notice"]))
        link = link_line(row)
        if link:
            lines.append(link)
    if moment == "wrap-up" and any(row["status"] in {"waiting_for_input", "waiting_for_approval"} for row in rows):
        lines.append("When you reply in T3, tell me “continue” and I’ll pick it up.")
    out = ""
    for line in lines:
        if out:
            # Markdown hard break between prose lines; plain newlines around fenced blocks.
            out += "\n" if line.startswith("```") or out.endswith("```") else "  \n"
        out += line
    return out
