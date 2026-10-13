import asyncio
import json
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

from client import AuthenticationRequired, Client, Credentials, MutationUnknown, load_targets
from domain import canonical_moment, row_signature, digest, git_value, message_dispatch_mode, observe, resolve_project
from journal import Journal


def is_scratch_root(root):
    """True only for T3's scratch project root (<home>/.t3/scratch) on any user or OS."""
    parts = [part for part in (root or "").replace("\\", "/").rstrip("/").split("/") if part]
    return parts[-2:] == [".t3", "scratch"]


def place_label(checkout, root, principal):
    """Describe where a thread runs. T3 gives each thread of the scratch project (root ~/.t3/scratch, shown as
    "No project") its own dated subfolder, which is a plain folder under the root, not a git worktree."""
    if principal:
        return "main checkout"
    if is_scratch_root(root) and checkout.startswith(root.rstrip("/\\") + "/"):
        return "scratch folder " + "/".join(checkout.split("/")[-2:])
    return "worktree " + "/".join(checkout.split("/")[-2:])


class Engine:
    def __init__(self, config_root, state_root):
        self.config_root, self.state_root = Path(config_root), Path(state_root)
        self.credentials = Credentials(config_root)
        self.avisos = {}
        self.drivers = {}
        self.wait_interval = 2.0

    def target(self, target_id=None, environment=None):
        configured = load_targets(self.config_root)
        selected = [target for target in configured if target.id == target_id] if target_id else [target for target in configured if target.kind == "local"]
        if len(selected) != 1:
            raise ValueError("Choose a configured target explicitly, or pair one with setup.py. No other destination was used.")
        target = selected[0]
        if environment is not None and target.environment_id != environment:
            raise ValueError("The recorded target now points to a different environment. Restore its configuration; no other destination was used.")
        return target

    @staticmethod
    def mode(requested):
        """New plugin launches default to full access; explicit overrides are retained."""
        mode = requested or "full-access"
        if mode not in {"approval-required", "auto-accept-edits", "auto", "full-access"}:
            raise ValueError("Unknown runtime mode. No call was sent.")
        return mode

    @staticmethod
    def project_row(project, target):
        return {"id": project.get("id"), "title": project.get("title"), "workspaceRoot": project.get("workspaceRoot"),
                "default": project.get("id") == target.default_project_id}

    async def choose_project(self, client, projects, target, project_id=None, title=None, checkout=None):
        """Return (project, resolvedBy). Order: explicit projectId/projectTitle, a local checkout's Git match, the
        default set with `setup.py bind --project-id`, then T3's scratch "No project". Never "the only project"."""
        choices = "; ".join(f"{project.get('title')} ({project.get('id')})" for project in projects) or "none"
        if project_id is not None or title is not None:
            matched = [project for project in projects
                       if (project_id is None or project.get("id") == project_id)
                       and (title is None or (project.get("title") or "").casefold() == title.strip().casefold())]
            if len(matched) == 1:
                return matched[0], "projectId" if project_id is not None else "projectTitle"
            problem = "Several projects match that title" if matched else "No project in this environment matches that id/title"
            raise ValueError(f"{problem}. Pass a projectId from: {choices}. No thread was launched.")
        if checkout:
            try:
                return resolve_project(projects, checkout), "git"
            except ValueError:
                pass
        if target.default_project_id:
            default = next((project for project in projects if project.get("id") == target.default_project_id), None)
            if default is not None:
                return default, "scratch" if is_scratch_root(default.get("workspaceRoot")) else "default"
        scratch = next((project for project in projects if is_scratch_root(project.get("workspaceRoot"))), None)
        if scratch is None:
            try:
                created = await client.rpc("projects.ensureScratch", {})
            except MutationUnknown:
                raise MutationUnknown("T3 did not confirm its scratch project, so no thread was launched. Check list_projects for "
                                      "\"No project\", then retry with projectId, or retry the same requestKey.") from None
            scratch_id = (created or {}).get("projectId")
            scratch = next((project for project in (await client.shell()).get("projects", []) if project.get("id") == scratch_id),
                           {"id": scratch_id, "title": "No project", "workspaceRoot": None})
            if not scratch.get("id"):
                raise ValueError("T3 did not provide its scratch project. Pass projectId or projectTitle; no thread was launched.")
        return scratch, "scratch"

    async def submit(self, journal, operation):
        target = self.target(operation["target"], operation["environment"])
        client = Client(target, self.credentials)
        state = operation["state"]
        authentication_required = False
        if state != "accepted":
            self.mode(operation["intent"].get("runtimeMode"))
            journal.mark(operation["request_key"], "unknown")
            try:
                method = "orchestration.launchThread" if operation["kind"] == "launch" else "orchestration.dispatchCommand"
                receipt = await client.rpc(method, operation["payload"])
                if operation["kind"] == "launch" and receipt.get("threadId") != operation["thread_id"]:
                    raise MutationUnknown("T3 returned a different thread. Reconcile the original recorded operation.")
                journal.mark(operation["request_key"], "accepted", receipt)
                state = "accepted"
            except AuthenticationRequired:
                state = "unknown"
                authentication_required = True
            except (ValueError, OSError, TimeoutError):
                state = "unknown"
        return {"threadId": operation["thread_id"], "requestKey": operation["request_key"], "commandId": operation["command_id"],
                "messageId": operation["message_id"],
                "operationState": state, "target": target.id, "environmentId": target.environment_id,
                "instruction": "Pair this environment again with setup.py, then reconcile this recorded thread or retry the exact requestKey and arguments. Do not launch a replacement." if authentication_required else
                    "Await this recorded thread." if state == "accepted" else "The outcome is unconfirmed. Read this recorded thread or retry the exact requestKey and arguments; do not launch a replacement."}

    async def read(self, journal, thread_id):
        operation = journal.latest(thread_id)
        launch = journal.launch(thread_id)
        target = self.target(operation["target"], operation["environment"])
        client = Client(target, self.credentials)
        data = {"threadId": thread_id, "status": "unknown", "settled": False, "lastAssistantMessage": None, "latestAssistantText": None,
                "lastError": None, "round": operation["round"], "operationState": operation["state"], "observedAt": None}
        project = {"workspaceRoot": "", "title": "Unconfirmed project"}
        thread = {}
        pending_runs = True
        try:
            projection = await client.projection(thread_id)
            thread = projection["thread"]
            if thread.get("projectId") != launch["intent"]["projectId"]:
                raise ValueError("The server thread now belongs to a different project. Its result was not used.")
            data.update(observe(operation, projection), observedAt=time.time())
            pending_runs = any(run.get("status") in {"queued", "preparing", "starting", "running", "waiting"}
                               for run in projection.get("runs", []))
            queued = sorted((run for run in projection.get("runs", []) if run.get("status") == "queued"),
                            key=lambda run: run.get("queuePosition") or run.get("ordinal", 0))
            data["queuedMessages"] = [{"runId": run["id"], "messageId": run.get("userMessageId"),
                                       "position": run.get("queuePosition"), "held": run.get("queueHeld", False)} for run in queued]
            if pending_runs and data["status"] == "completed":
                data.update(status="background", settled=False, lastAssistantMessage=None,
                            notice="This message's turn finished, but other active or queued work remains in T3.")
            if data["deliveryConfirmed"] and operation["state"] != "accepted":
                journal.mark(operation["request_key"], "accepted", {"runId": data["runId"], "confirmedByProjection": True})
                data["operationState"] = "accepted"
        except AuthenticationRequired:
            data.update(notice="This environment needs pairing again with setup.py; the record is kept.")
        except (ValueError, OSError, TimeoutError):
            pass
        if data["observedAt"] is not None:
            try:
                shell = await client.shell()
                project = next((project for project in shell.get("projects", []) if project["id"] == launch["intent"]["projectId"]), project)
                summary = next((entry for entry in shell.get("threads", []) if entry["id"] == thread_id), {})
                if summary.get("pendingBackgroundTasks") and data["status"] == "completed":
                    data.update(status="background", settled=False, lastAssistantMessage=None)
                if data["status"] == "failed" and summary.get("lastError"):
                    data["lastError"] = summary["lastError"]
            except (ValueError, OSError, TimeoutError):
                data["notice"] = "Reply observed; background work could not be confirmed."
        selection = thread.get("modelSelection", launch["intent"]["modelSelection"])
        key = (target.environment_id, selection["instanceId"])
        if data["observedAt"] is not None and key not in self.drivers:
            try:
                found = {(target.environment_id, provider["instanceId"]): provider["driver"] for provider in await client.providers()}
                # Cache a confirmed absence too, so status reads don't repeat getConfig. Errors stay uncached (retryable).
                found.setdefault(key, None)
                self.drivers.update(found)
            except (ValueError, OSError, TimeoutError):
                pass
        checkout = thread.get("worktreePath") or launch["intent"]["workspaceStrategy"].get("worktreePath") or project["workspaceRoot"]
        principal = checkout == project["workspaceRoot"] or (target.kind == "local" and checkout and project["workspaceRoot"] and Path(checkout).resolve() == Path(project["workspaceRoot"]).resolve())
        data.update(title=launch["intent"]["title"], provider=selection["instanceId"], driver=self.drivers.get(key), model=selection["model"],
                    project=Path(project["workspaceRoot"]).name or project["title"], place=place_label(checkout, project["workspaceRoot"], principal),
                    environment=target.environment_id, webOrigin=target.web_origin, target=target.id, checkout=checkout,
                    runtimeMode=thread.get("runtimeMode", launch["intent"]["runtimeMode"]))
        return self.credentials.redact(data)

    async def add_project(self, arguments):
        root = arguments["workspaceRoot"].strip()
        if not root or not (root.startswith("/") or (len(root) > 2 and root[1] == ":" and root[2] in "\\/")):
            raise ValueError("workspaceRoot must be an absolute path on the target machine. Nothing was sent.")
        target = self.target(arguments.get("target"))
        client = Client(target, self.credentials)
        def same(project):
            return (project.get("workspaceRoot") or "").rstrip("/\\") == root.rstrip("/\\")
        existing = next((project for project in (await client.shell()).get("projects", []) if same(project)), None)
        if existing is not None:
            return {"created": False, "target": target.id, "project": self.project_row(existing, target)}
        windows = not root.startswith("/")
        title = (arguments.get("title") or "").strip() or (PureWindowsPath(root) if windows else PurePosixPath(root)).name or root
        # Identifiers derive from the environment, requestKey and the exact intent: a retry with the same arguments
        # resends the identical command (T3 dedupes it), and changed arguments never reuse those identifiers.
        seed = json.dumps([target.environment_id, arguments["requestKey"], root, title])
        payload = {"type": "project.create", "commandId": str(uuid.uuid5(uuid.NAMESPACE_URL, "t3-lab-command:" + seed)),
                   "projectId": str(uuid.uuid5(uuid.NAMESPACE_URL, "t3-lab-project:" + seed)),
                   "title": title, "workspaceRoot": root, "createWorkspaceRootIfMissing": False}
        try:
            project = await client.rpc("projects.mutate", payload)
        except MutationUnknown:
            found = next((project for project in (await client.shell()).get("projects", [])
                          if project.get("id") == payload["projectId"] or same(project)), None)
            if found is None:
                raise MutationUnknown("T3 did not confirm the project (it may have rejected the folder, e.g. missing or already registered, "
                                      "or the reply was lost). The outcome is unknown: check list_projects or retry the exact requestKey and arguments.") from None
            project = found
        return {"created": project.get("id") == payload["projectId"], "target": target.id, "project": self.project_row(project, target)}

    async def spawned(self, journal, operation, result, owner):
        """Add the resolved project, a fallback warning, and the one-thread handoff digest to a spawn result."""
        project = dict((operation.get("request") or {}).get("_project") or {"id": operation["intent"]["projectId"], "title": None, "resolvedBy": None})
        if project.get("resolvedBy") == "scratch":
            project["id"] = None
        result["project"] = project
        if project.get("resolvedBy") in {"default", "scratch"}:
            result["warning"] = ("No projectId/projectTitle was given, so the thread runs in T3's scratch \"No project\" folder. "
                                 "Pass projectId or projectTitle (see list_projects) to run in a project.") if project["resolvedBy"] == "scratch" else (
                                 f"No projectId/projectTitle was given, so the target's default project ({project.get('title')}) was used.")
        if result.get("operationState") == "accepted":
            try:
                status = await self.call("delegation_status", {"threadIds": [result["threadId"]], "moment": "handoff"}, owner)
                result["digest"] = status["digest"]
            except Exception:  # the digest is a convenience; the launch result stands without it
                pass
        return result

    async def call(self, name, arguments, owner):
        if name == "list_targets":
            return {"targets": [{"id": target.id, "label": target.label, "origin": target.origin, "environmentId": target.environment_id,
                    "kind": target.kind, "connection": "configured" if self.credentials.file(target.environment_id).exists() else "needs_pairing"}
                    for target in load_targets(self.config_root)]}
        journal = Journal(self.state_root, owner)
        if name == "list_providers":
            client = Client(self.target(arguments.get("target")), self.credentials)
            return {"providers": await client.providers()}
        if name == "list_threads":
            return {"threads": [{"threadId": row["thread_id"], "requestKey": row["request_key"], "target": row["target"], "operationState": row["state"]} for row in journal.threads()]}
        if name == "list_projects":
            target = self.target(arguments.get("target"))
            shell = await Client(target, self.credentials).shell()
            return {"target": target.id, "projects": [self.project_row(project, target) for project in shell.get("projects", [])]}
        if name == "add_project":
            return await self.add_project(arguments)
        if name in {"spawn_thread", "send_to_thread"}:
            key = arguments["requestKey"]
            previous = journal.find(key)
            if previous:
                if {key: value for key, value in previous["request"].items() if not key.startswith("_")} != arguments or previous["kind"] != ("launch" if name == "spawn_thread" else "send"):
                    raise ValueError("This requestKey records different arguments. Recover the original or use a new key for a new operation; nothing was sent.")
                result = await self.submit(journal, previous)
                return await self.spawned(journal, previous, result, owner) if name == "spawn_thread" else result
        if name == "spawn_thread":
            target = self.target(arguments.get("target"))
            client = Client(target, self.credentials)
            shell = await client.shell()
            if target.kind == "local":
                checkout = arguments.get("checkout")
                if checkout and (not Path(checkout).is_absolute() or not Path(checkout).is_dir()):
                    raise ValueError("checkout must be an existing absolute directory on this machine. No thread was launched.")
                projects = shell.get("projects", [])
                project, resolved = await self.choose_project(client, projects, target, arguments.get("projectId"), arguments.get("projectTitle"), checkout)
                if resolved == "scratch":
                    # Scratch gives each thread its own folder; the chat's checkout is not used.
                    strategy = {"type": "root"}
                else:
                    if not checkout:
                        raise ValueError("Pass this chat's actual absolute checkout for a local project; the plugin cache is not its working directory.")
                    # The checkout must belong to the chosen project (same Git repository), however it was chosen.
                    resolve_project(projects, checkout, project["id"])
                    strategy = {"type": "existing_worktree", "worktreePath": str(Path(checkout).resolve())} if arguments["workspace"] == "shared" else {
                        "type": "worktree", "baseRef": git_value(checkout, "rev-parse", "HEAD"), "startFromOrigin": False}
            else:
                if arguments.get("checkout") is not None or arguments["workspace"] != "shared":
                    raise ValueError("Remote targets run in the project's root with workspace shared. A local checkout or a remote worktree is not accepted.")
                project, resolved = await self.choose_project(client, shell.get("projects", []), target, arguments.get("projectId"), arguments.get("projectTitle"))
                strategy = {"type": "root"}
            model = arguments["modelSelection"]
            provider = next((provider for provider in await client.providers() if provider["instanceId"] == model["instanceId"]), None)
            if not provider or not provider["ready"] or model["model"] not in provider["models"]:
                raise ValueError("The requested provider/model is not available. No substitute or thread was launched.")
            mode = self.mode(arguments.get("runtimeMode"))
            intent = {"projectId": project["id"], "title": arguments.get("title") or "Task from the parent chat", "modelSelection": model,
                      "runtimeMode": mode, "interactionMode": "default", "workspaceStrategy": strategy, "creationSource": "mcp",
                      "initialMessage": {"text": arguments["prompt"], "attachments": []}}
            record = {**arguments, "_project": {"id": project.get("id"), "title": project.get("title"), "resolvedBy": resolved}}
            operation = journal.prepare(arguments["requestKey"], "launch", target.id, target.environment_id, intent, request=record)
            return await self.spawned(journal, operation, await self.submit(journal, operation), owner)
        if name == "send_to_thread":
            operation = journal.latest(arguments["threadId"])
            target = self.target(operation["target"], operation["environment"])
            client = Client(target, self.credentials)
            projection = await client.projection(arguments["threadId"])
            if projection["thread"].get("projectId") != journal.launch(arguments["threadId"])["intent"]["projectId"]:
                raise ValueError("The thread now belongs to a different project. No message was sent.")
            dispatch = message_dispatch_mode(arguments.get("mode", "steer"), projection)
            intent = {"text": arguments["message"], "attachments": [], "createdBy": "user", "creationSource": "mcp", "dispatchMode": dispatch}
            metadata = getattr(client, "metadata", None) or {}
            if arguments.get("mode", "steer") == "steer" and metadata.get("capabilities", {}).get("serverResolvedCommandContext") is True:
                intent["deliveryIntent"] = "steer"
            followup = journal.prepare(arguments["requestKey"], "send", operation["target"], operation["environment"], intent, arguments["threadId"], request=arguments)
            return await self.submit(journal, followup)
        if name in {"read_thread", "await_thread"}:
            journal.latest(arguments["threadId"])
            deadline = time.monotonic() + (arguments.get("timeoutSeconds", 45) if name == "await_thread" else 0)
            while True:
                try:
                    async with asyncio.timeout(max(0.001, deadline - time.monotonic()) if name == "await_thread" else 45):
                        data = await self.read(journal, arguments["threadId"])
                except TimeoutError:
                    return {"threadId": arguments["threadId"], "status": "unknown", "settled": False, "lastAssistantMessage": None,
                            "lastError": None, "timedOut": True, "instruction": "The wait budget ended, not the child. Await the same ID again."}
                if data["settled"] or data.get("queueHeld") or time.monotonic() >= deadline:
                    # Keep the head; callers page with offset. truncated/totalChars describe the main reply.
                    size, offset = arguments.get("maxChars", 4000), arguments.get("offset", 0)
                    main = "lastAssistantMessage" if data.get("lastAssistantMessage") else "latestAssistantText"
                    for key in ("lastAssistantMessage", "latestAssistantText"):
                        if data.get(key):
                            whole = data[key]
                            data[key] = whole[offset:offset + size]
                            if key == main:
                                data.update(totalChars=len(whole), offset=offset, truncated=offset > 0 or offset + size < len(whole))
                    return data
                await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
        if name == "delegation_status":
            moment = canonical_moment(arguments["moment"])
            selected = arguments["threadIds"]
            environments = {thread: journal.latest(thread)["environment"] for thread in selected}
            launch_order = {thread: journal.launch(thread)["seq"] for thread in selected}
            ordered = sorted(selected, key=launch_order.get)
            rows = await asyncio.gather(*(self.read(journal, thread) for thread in ordered))
            waited = None
            if arguments.get("waitSeconds"):
                # Long-poll for the card: hold until any row's signature differs from `since`,
                # or the wait budget ends. Never blocks the event loop.
                since = arguments.get("since")
                deadline = time.monotonic() + arguments["waitSeconds"]
                started = time.monotonic()
                def changed(current):
                    return since is None or ";".join(row_signature(row) for row in current) != since
                while not changed(rows) and time.monotonic() < deadline:
                    await asyncio.sleep(min(self.wait_interval, max(0, deadline - time.monotonic())))
                    rows = await asyncio.gather(*(self.read(journal, thread) for thread in ordered))
                waited = {"seconds": round(time.monotonic() - started, 2), "changed": changed(rows), "timedOut": not changed(rows) and time.monotonic() >= deadline}
            for index, row in enumerate(rows, 1):
                row["n"] = index
            observation = min((row["observedAt"] for row in rows if row["observedAt"] is not None), default=None)
            for row in rows:
                row["signature"] = row_signature(row)
            # One digest section per environment, in launch order of each environment's first thread.
            sections = {}
            for row in rows:
                sections.setdefault(environments[row["threadId"]], []).append(row)
            text = "\n\n".join(digest(group, moment, min((row["observedAt"] for row in group if row["observedAt"] is not None), default=None))
                                for group in sections.values())
            content = {"rows": rows, "observedAt": observation, "digest": text,
                       "signature": ";".join(row["signature"] for row in rows)}
            if waited is not None:
                content["wait"] = waited
            if moment == "update":
                identity = (owner, tuple(sorted(selected)))
                signature = json.dumps([(row["threadId"], row_signature(row)) for row in rows])
                content["changed"] = self.avisos.get(identity) != signature
                if content["changed"]:
                    self.avisos[identity] = signature
                else:
                    content["digest"] = "No changes since the last update. Do not write anything to the user."
            return content
        raise ValueError("Unknown plugin tool.")
