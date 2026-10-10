import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import AuthenticationRequired, MutationUnknown, Target, save_target
from engine import Engine
from journal import Journal
from test_core import ENV, OWNER, OTHER, repo


class FakeClient:
    projects = []
    calls = []
    projections = {}
    lose_before_commit = False
    lose_after_commit = False
    provider_calls = 0
    provider_error = None

    def __init__(self, target, credentials):
        self.target = target

    async def public_metadata(self):
        return {"environmentId": self.target.environment_id, "orchestrationProtocolVersion": 2}

    async def shell(self):
        return {"projects": self.projects, "threads": [{"id": thread, "pendingBackgroundTasks": []} for thread in self.projections]}

    async def providers(self):
        FakeClient.provider_calls += 1
        if self.provider_error:
            raise self.provider_error
        return [{"instanceId": "claudeAgent", "models": ["test-model"], "ready": True, "displayName": "Claude", "driver": "claudeAgent"}]

    async def projection(self, thread_id):
        self.calls.append(("projection", thread_id))
        return copy.deepcopy(self.projections[thread_id])

    async def rpc(self, name, payload):
        self.calls.append((name, copy.deepcopy(payload)))
        if self.lose_before_commit:
            raise MutationUnknown("Synthetic connection loss")
        thread_id = payload["threadId"]
        if name == "orchestration.launchThread":
            self.projections[thread_id] = {"thread": {"id": thread_id, "projectId": payload["projectId"], "runtimeMode": payload["runtimeMode"], "modelSelection": payload["modelSelection"]}, "runs": [], "messages": []}
            message = payload["initialMessage"]["messageId"]
            text = "First reply"
        else:
            message = payload["messageId"]
            text = "Second reply"
        projection = self.projections[thread_id]
        ordinal = len(projection["runs"]) + 1
        run = {"id": "r" + str(ordinal), "ordinal": ordinal, "userMessageId": message, "status": "completed"}
        projection["runs"].append(run)
        projection["messages"].append({"role": "assistant", "runId": run["id"], "text": text})
        if self.lose_after_commit:
            raise MutationUnknown("Synthetic lost acknowledgment")
        return {"threadId": thread_id, "sequence": ordinal}


class Operations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = repo(self.root / "repo")
        self.config = self.root / "config"
        self.state = self.root / "state"
        save_target(self.config, Target("mac", "Mac", "http://127.0.0.1:3773", ENV, runtime_mode="approval-required"))
        FakeClient.projects = [{"id": "p1", "workspaceRoot": str(self.repo), "title": "Repo"}]
        FakeClient.calls, FakeClient.projections = [], {}
        FakeClient.lose_before_commit = FakeClient.lose_after_commit = False
        FakeClient.provider_calls, FakeClient.provider_error = 0, None
        self.enterContext(patch("engine.Client", FakeClient))
        self.arguments = {"requestKey": "review", "checkout": str(self.repo), "workspace": "shared", "prompt": "Read only", "title": "Review", "modelSelection": {"instanceId": "claudeAgent", "model": "test-model"}}
        self.engine = Engine(self.config, self.state)

    async def launch(self, owner=OWNER, **changed):
        return await self.engine.call("spawn_thread", {**self.arguments, **changed}, owner)

    async def test_repeated_launch_returns_saved_thread_without_redispatch(self):
        first = await self.launch()
        second = await Engine(self.config, self.state).call("spawn_thread", self.arguments, OWNER)
        self.assertEqual(first["threadId"], second["threadId"])
        self.assertEqual(len([call for call in FakeClient.calls if call[0] == "orchestration.launchThread"]), 1)

    async def test_two_conversations_never_use_each_others_registry(self):
        first = await self.launch()
        second = await self.launch(owner=OTHER)
        self.assertNotEqual(first["threadId"], second["threadId"])
        before = list(FakeClient.calls)
        with self.assertRaises(ValueError):
            await self.engine.call("read_thread", {"threadId": first["threadId"]}, OTHER)
        self.assertEqual(FakeClient.calls, before)

    async def test_a_lost_reply_is_reconciled_by_the_exact_message_run(self):
        FakeClient.lose_after_commit = True
        result = await self.launch()
        self.assertEqual(result["operationState"], "unknown")
        FakeClient.lose_after_commit = False
        observed = await self.engine.call("read_thread", {"threadId": result["threadId"]}, OWNER)
        self.assertEqual(observed["lastAssistantMessage"], "First reply")
        self.assertEqual(Journal(self.state, OWNER).latest(result["threadId"])["state"], "accepted")
        await self.launch()
        self.assertEqual(len([call for call in FakeClient.calls if call[0] == "orchestration.launchThread"]), 1)

    async def test_failed_send_hides_old_answer_and_reuses_ids_on_retry(self):
        launch = await self.launch()
        FakeClient.lose_before_commit = True
        args = {"threadId": launch["threadId"], "requestKey": "round-two", "message": "Second round"}
        unknown = await self.engine.call("send_to_thread", args, OWNER)
        self.assertEqual(unknown["operationState"], "unknown")
        read = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
        self.assertIsNone(read["lastAssistantMessage"])
        self.assertEqual(read["status"], "unknown")
        FakeClient.lose_before_commit = False
        sent = await self.engine.call("send_to_thread", args, OWNER)
        self.assertEqual(sent["operationState"], "accepted")
        calls = [call[1] for call in FakeClient.calls if call[0] == "orchestration.dispatchCommand"]
        self.assertEqual(calls[0], calls[1])
        read = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
        self.assertEqual(read["lastAssistantMessage"], "Second reply")

    async def test_validation_rejects_foreign_checkout_and_unknown_model_before_mutation(self):
        other = repo(self.root / "other")
        for changes in ({"checkout": str(other)}, {"modelSelection": {"instanceId": "claudeAgent", "model": "not-a-model"}}):
            with self.assertRaises(ValueError):
                await self.launch(**changes)
        self.assertFalse(any(call[0] == "orchestration.launchThread" for call in FakeClient.calls))
        self.assertEqual(Journal(self.state, OWNER).threads(), [])

    async def test_failed_storage_sends_no_operation(self):
        blocked = self.root / "blocked"
        blocked.write_text("not a directory")
        with self.assertRaises(OSError):
            await Engine(self.config, blocked).call("spawn_thread", self.arguments, OWNER)
        self.assertFalse(any(call[0] == "orchestration.launchThread" for call in FakeClient.calls))

    async def test_discovery_never_creates_projects(self):
        FakeClient.projects = []
        await self.engine.call("list_providers", {}, OWNER)
        self.assertEqual(FakeClient.calls, [])
        with self.assertRaises(ValueError):
            await self.launch()
        self.assertEqual(FakeClient.calls, [])

    async def test_await_budget_includes_a_slow_projection_read(self):
        launch = await self.launch()
        original = FakeClient.projection
        async def slow(client, thread_id):
            await asyncio.sleep(2)
            return await original(client, thread_id)
        with patch.object(FakeClient, "projection", slow):
            async with asyncio.timeout(1.4):
                observed = await self.engine.call("await_thread", {"threadId": launch["threadId"], "timeoutSeconds": 1}, OWNER)
        self.assertFalse(observed["settled"])
        self.assertTrue(observed["timedOut"])

    async def test_runtime_mode_is_a_default_that_a_call_may_override(self):
        await self.launch()
        await self.launch(requestKey="raised", runtimeMode="full-access")
        modes = [call[1]["runtimeMode"] for call in FakeClient.calls if call[0] == "orchestration.launchThread"]
        self.assertEqual(modes, ["approval-required", "full-access"])

    async def test_optional_shell_failure_does_not_erase_a_valid_response(self):
        launch = await self.launch()
        async def unavailable(client):
            raise OSError("Synthetic shell outage")
        with patch.object(FakeClient, "shell", unavailable):
            read = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
        self.assertEqual(read["lastAssistantMessage"], "First reply")
        self.assertEqual(read["status"], "completed")
        self.assertIsNotNone(read["observedAt"])

    async def test_new_round_requires_confirmed_background_state(self):
        launch = await self.launch()
        async def unavailable(client):
            raise OSError("Synthetic shell outage")
        with patch.object(FakeClient, "shell", unavailable):
            with self.assertRaises(ValueError):
                await self.engine.call("send_to_thread", {"threadId": launch["threadId"], "requestKey": "uncertain-background", "message": "Continue"}, OWNER)
        self.assertFalse(any(call[0] == "orchestration.dispatchCommand" for call in FakeClient.calls))

    async def test_status_can_mix_owned_ids_outside_the_list_window(self):
        launches = [await self.launch(requestKey="request-" + str(number)) for number in range(101)]
        ids = [launches[0]["threadId"], launches[-1]["threadId"]]
        content = await self.engine.call("delegation_status", {"threadIds": ids, "moment": "wrap-up"}, OWNER)
        self.assertEqual([row["threadId"] for row in content["rows"]], ids)

    async def test_rows_carry_the_driver_resolved_once_per_instance(self):
        launch = await self.launch()
        FakeClient.provider_calls = 0
        first = await self.engine.call("delegation_status", {"threadIds": [launch["threadId"]], "moment": "check-in"}, OWNER)
        self.assertEqual(first["rows"][0]["driver"], "claudeAgent")
        self.assertEqual(first["rows"][0]["provider"], "claudeAgent")
        self.assertEqual(first["rows"][0]["excerpt"], "First reply")
        second = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
        self.assertEqual(second["driver"], "claudeAgent")
        self.assertEqual(FakeClient.provider_calls, 1)

    async def test_a_providers_failure_leaves_the_driver_empty_without_failing_the_read(self):
        launch = await self.launch()
        for error in (AuthenticationRequired("expired"), ValueError("metadata"), OSError("down"), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                FakeClient.provider_error = error
                observed = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
                self.assertIsNone(observed["driver"])
                self.assertEqual(observed["lastAssistantMessage"], "First reply")

    async def test_an_unreachable_server_adds_no_providers_call(self):
        launch = await self.launch()
        FakeClient.provider_calls = 0
        async def down(client, thread_id):
            raise OSError("down")
        with patch.object(FakeClient, "projection", down):
            observed = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
        self.assertIsNone(observed["observedAt"])
        self.assertIsNone(observed["driver"])
        self.assertEqual(FakeClient.provider_calls, 0)

    async def test_a_confirmed_missing_instance_is_cached_but_errors_stay_retryable(self):
        launch = await self.launch()
        FakeClient.provider_calls = 0
        FakeClient.provider_error = OSError("down")
        await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
        FakeClient.provider_error = None
        async def other(client):
            FakeClient.provider_calls += 1
            return [{"instanceId": "someoneElse", "driver": "codex", "models": [], "ready": True}]
        with patch.object(FakeClient, "providers", other):
            for _ in range(3):
                observed = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
                self.assertIsNone(observed["driver"])
        self.assertEqual(FakeClient.provider_calls, 2, "one failed call retried once, then the absence is cached")

    async def test_an_instance_missing_from_the_config_has_no_driver(self):
        launch = await self.launch()
        async def other(client):
            return [{"instanceId": "grok_og", "driver": "grok", "models": [], "ready": True, "displayName": "Grok"}]
        with patch.object(FakeClient, "providers", other):
            observed = await self.engine.call("read_thread", {"threadId": launch["threadId"]}, OWNER)
        self.assertIsNone(observed["driver"])
        self.assertEqual(observed["provider"], "claudeAgent")

    async def test_a_mutation_without_auth_keeps_ids_and_explains_repair(self):
        async def denied(client, name, payload):
            raise AuthenticationRequired("expired")
        with patch.object(FakeClient, "rpc", denied):
            unknown = await self.launch()
        self.assertEqual(unknown["operationState"], "unknown")
        self.assertIn("setup.py", unknown["instruction"])
        recovered = await self.launch()
        self.assertEqual(recovered["threadId"], unknown["threadId"])
        self.assertEqual(recovered["commandId"], unknown["commandId"])

    async def test_unavailable_status_has_no_fabricated_observation_time(self):
        launch = await self.launch()
        async def unavailable(client, thread):
            raise OSError("Synthetic outage")
        with patch.object(FakeClient, "projection", unavailable):
            content = await self.engine.call("delegation_status", {"threadIds": [launch["threadId"]], "moment": "update"}, OWNER)
        self.assertIsNone(content["observedAt"])
        self.assertIn("no observation yet", content["digest"])
        self.assertNotIn("seen ", content["digest"])

    async def test_repeated_unchanged_notice_is_suppressed_within_its_chat(self):
        launch = await self.launch()
        args = {"threadIds": [launch["threadId"]], "moment": "update"}
        first = await self.engine.call("delegation_status", args, OWNER)
        second = await self.engine.call("delegation_status", args, OWNER)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(second["digest"], "No changes since the last update. Do not write anything to the user.")

    async def test_mixed_checkouts_do_not_share_a_misleading_heading(self):
        first = await self.launch()
        second = await self.launch(requestKey="other-checkout")
        FakeClient.projections[second["threadId"]]["thread"]["worktreePath"] = "/tmp/other-worktree"
        content = await self.engine.call("delegation_status", {"threadIds": [first["threadId"], second["threadId"]], "moment": "wrap-up"}, OWNER)
        self.assertNotIn("Card: expand", content["digest"])
        self.assertNotIn("tmp/other-worktree", content["digest"])
        self.assertEqual(content["digest"].count("```\nt3code://threads/"), 2)
