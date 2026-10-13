import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.shared.memory import create_connected_server_and_client_session

from client import MutationUnknown, Target, save_target
from domain import observe, row_signature
from engine import Engine
from server import build_server
from test_core import ENV, OWNER, OTHER, repo
from test_engine import FakeClient


class SteeringClient(FakeClient):
    """Model T3's persisted steering message, without an external provider."""

    server_resolved = False

    def __init__(self, target, credentials):
        super().__init__(target, credentials)
        self.metadata = {"capabilities": {"serverResolvedCommandContext": self.server_resolved}}

    async def rpc(self, name, payload):
        if name != "orchestration.dispatchCommand" or payload["dispatchMode"]["type"] not in {"steer_active", "queue_after_active"}:
            return await super().rpc(name, payload)
        self.calls.append((name, copy.deepcopy(payload)))
        if self.lose_before_commit:
            raise MutationUnknown("Synthetic loss before commit")
        projection = self.projections[payload["threadId"]]
        message_id = payload["messageId"]
        if not any(message.get("id") == message_id for message in projection["messages"]):
            if payload["dispatchMode"]["type"] == "steer_active":
                run = next(run for run in projection["runs"] if run["id"] == payload["dispatchMode"]["targetRunId"])
            else:
                ordinal = len(projection["runs"]) + 1
                active = any(run["status"] in {"preparing", "starting", "running", "waiting"} for run in projection["runs"])
                position = sum(run["status"] == "queued" for run in projection["runs"]) + 1
                run = {"id": f"r{ordinal}", "ordinal": ordinal, "userMessageId": message_id,
                       "status": "queued" if active else "completed", "queuePosition": position if active else None}
                projection["runs"].append(run)
            projection["messages"].append({"id": message_id, "role": "user", "runId": run["id"], "text": payload["text"]})
        if self.lose_after_commit:
            raise MutationUnknown("Synthetic lost acknowledgment")
        return {"threadId": payload["threadId"], "sequence": len(self.calls)}


class Steering(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = repo(self.root / "repo")
        self.config, self.state = self.root / "config", self.root / "state"
        save_target(self.config, Target("mac", "Mac", "http://127.0.0.1:3773", ENV))
        FakeClient.projects = [{"id": "p1", "workspaceRoot": str(self.checkout), "title": "Repo"}]
        FakeClient.calls, FakeClient.projections = [], {}
        FakeClient.lose_before_commit = FakeClient.lose_after_commit = False
        FakeClient.provider_calls, FakeClient.provider_error = 0, None
        SteeringClient.server_resolved = False
        self.enterContext(patch("engine.Client", SteeringClient))
        self.engine = Engine(self.config, self.state)

    async def launch(self, status="running"):
        result = await self.engine.call("spawn_thread", {
            "requestKey": "launch", "checkout": str(self.checkout), "workspace": "shared",
            "prompt": "Read only", "modelSelection": {"instanceId": "claudeAgent", "model": "test-model"},
        }, OWNER)
        self.thread = result["threadId"]
        self.projection = FakeClient.projections[self.thread]
        self.projection["runs"][0]["status"] = status

    async def send(self, mode=None, key="send", owner=OWNER):
        arguments = {"threadId": self.thread, "requestKey": key, "message": "New instructions"}
        if mode is not None:
            arguments["mode"] = mode
        return await self.engine.call("send_to_thread", arguments, owner)

    def dispatches(self):
        return [payload for name, payload in FakeClient.calls if name == "orchestration.dispatchCommand"]

    async def test_steer_targets_active_run_and_observes_message_in_that_run(self):
        await self.launch()
        result = await self.send("steer")
        self.assertEqual(result["operationState"], "accepted")
        self.assertEqual(self.dispatches()[0]["dispatchMode"], {"type": "steer_active", "targetRunId": "r1"})
        self.assertEqual(len(self.projection["runs"]), 1)
        observed = await self.engine.call("read_thread", {"threadId": self.thread}, OWNER)
        self.assertEqual((observed["status"], observed["runId"], observed["deliveryConfirmed"]), ("running", "r1", True))
        self.assertIsNone(observed["lastAssistantMessage"])

    async def test_steer_ignores_a_newer_externally_queued_run(self):
        await self.launch()
        self.projection["runs"].append({"id": "r2", "ordinal": 2, "userMessageId": "external", "status": "queued"})
        await self.send("steer")
        self.assertEqual(self.dispatches()[-1]["dispatchMode"], {"type": "steer_active", "targetRunId": "r1"})
        observed = await self.engine.call("read_thread", {"threadId": self.thread}, OWNER)
        self.assertEqual((observed["runId"], observed["status"]), ("r1", "running"))

    async def test_lost_steering_ack_is_reconciled_without_redispatch(self):
        await self.launch()
        FakeClient.lose_after_commit = True
        self.assertEqual((await self.send("steer"))["operationState"], "unknown")
        FakeClient.lose_after_commit = False
        observed = await self.engine.call("read_thread", {"threadId": self.thread}, OWNER)
        self.assertEqual(observed["operationState"], "accepted")
        await self.send("steer")
        self.assertEqual(len(self.dispatches()), 1)

    async def test_steer_retry_after_restart_reuses_exact_identifiers(self):
        await self.launch()
        FakeClient.lose_before_commit = True
        self.assertEqual((await self.send("steer"))["operationState"], "unknown")
        FakeClient.lose_before_commit = False
        self.engine = Engine(self.config, self.state)
        await self.send("steer")
        self.assertEqual(self.dispatches()[0], self.dispatches()[1])
        self.assertEqual(len(self.projection["runs"]), 1)
        with self.assertRaisesRegex(ValueError, "different arguments"):
            await self.send("queue")

    async def test_default_steers_running_work_and_starts_a_turn_when_idle(self):
        await self.launch()
        await self.send()
        self.assertEqual(self.dispatches()[0]["dispatchMode"], {"type": "steer_active", "targetRunId": "r1"})
        self.projection["runs"][0]["status"] = "completed"
        await self.send(key="idle")
        self.assertEqual(self.dispatches()[1]["dispatchMode"], {"type": "start_immediately"})

    async def test_steering_refuses_nonrunning_turns_and_pending_approval(self):
        await self.launch()
        for status in ("preparing", "starting", "waiting"):
            self.projection["runs"][0]["status"] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                await self.send("steer")
        self.projection["runs"][0]["status"] = "running"
        self.projection["nodes"] = [{"id": "node-1", "runId": "r1"}]
        self.projection["runtimeRequests"] = [{"nodeId": "node-1", "status": "pending", "kind": "approval"}]
        with self.assertRaisesRegex(ValueError, "approval|input"):
            await self.send("steer")
        self.assertEqual(self.dispatches(), [])

    async def test_steering_does_not_bypass_owner_or_project_checks(self):
        await self.launch()
        with self.assertRaises(ValueError):
            await self.send("steer", owner=OTHER)
        self.projection["thread"]["projectId"] = "foreign"
        with self.assertRaisesRegex(ValueError, "project"):
            await self.send("steer")
        self.assertEqual(self.dispatches(), [])

    async def test_mcp_accepts_queue_but_rejects_removed_continue_mode(self):
        await self.launch()
        async with create_connected_server_and_client_session(build_server(self.config, self.state)) as session:
            arguments = {"threadId": self.thread, "requestKey": "queue", "message": "New instructions", "mode": "queue"}
            result = await session.call_tool("send_to_thread", arguments, meta={"threadId": OWNER})
            self.assertFalse(result.isError, result.content)
            result = await session.call_tool("send_to_thread", {**arguments, "requestKey": "bad", "mode": "continue"}, meta={"threadId": OWNER})
            self.assertTrue(result.isError)
            self.assertEqual(len(self.dispatches()), 1)

    async def test_queue_order_hold_and_completion_are_observed(self):
        await self.launch()
        await self.send("queue", key="first")
        await self.send("queue", key="second")
        self.assertEqual([payload["dispatchMode"] for payload in self.dispatches()], [{"type": "queue_after_active"}] * 2)
        observed = await self.engine.call("read_thread", {"threadId": self.thread}, OWNER)
        self.assertEqual((observed["status"], observed["queuePosition"], observed["settled"]), ("queued", 2, False))
        self.assertEqual([item["position"] for item in observed["queuedMessages"]], [1, 2])
        first_signature = row_signature(observed)
        self.projection["runs"][-1]["queueHeld"] = True
        with patch("engine.asyncio.sleep", side_effect=AssertionError("held queue must return immediately")):
            held = await self.engine.call("await_thread", {"threadId": self.thread, "timeoutSeconds": 1}, OWNER)
        self.assertTrue(held["queueHeld"])
        self.assertFalse(held["settled"])
        self.assertNotEqual(first_signature, row_signature(held))
        for run in self.projection["runs"]:
            run["status"] = "completed"
        self.projection["messages"].append({"role": "assistant", "runId": "r3", "text": "Queue done"})
        done = await self.engine.call("read_thread", {"threadId": self.thread}, OWNER)
        self.assertEqual((done["status"], done["lastAssistantMessage"]), ("completed", "Queue done"))

    async def test_queue_starts_when_idle_and_retry_keeps_original_ids(self):
        await self.launch("completed")
        FakeClient.lose_before_commit = True
        self.assertEqual((await self.send("queue"))["operationState"], "unknown")
        FakeClient.lose_before_commit = False
        self.engine = Engine(self.config, self.state)
        await self.send("queue")
        self.assertEqual(self.dispatches()[0], self.dispatches()[1])
        observed = await self.engine.call("read_thread", {"threadId": self.thread}, OWNER)
        self.assertEqual(observed["status"], "completed")

    async def test_spawn_defaults_to_full_access_with_explicit_override(self):
        await self.launch()
        payload = next(payload for name, payload in FakeClient.calls if name == "orchestration.launchThread")
        self.assertEqual(payload["runtimeMode"], "full-access")
        await self.engine.call("spawn_thread", {"requestKey": "restricted", "checkout": str(self.checkout), "workspace": "shared",
                              "prompt": "Read only", "runtimeMode": "approval-required",
                              "modelSelection": {"instanceId": "claudeAgent", "model": "test-model"}}, OWNER)
        modes = [payload["runtimeMode"] for name, payload in FakeClient.calls if name == "orchestration.launchThread"]
        self.assertEqual(modes, ["full-access", "approval-required"])

    async def test_capable_server_resolves_steering_atomically_but_queue_stays_explicit(self):
        SteeringClient.server_resolved = True
        await self.launch()
        await self.send(key="steer")
        await self.send("queue", key="queue")
        self.assertEqual(self.dispatches()[0]["deliveryIntent"], "steer")
        self.assertNotIn("deliveryIntent", self.dispatches()[1])

    async def test_older_server_gets_explicit_dispatch_without_new_fields(self):
        await self.launch()
        await self.send()
        self.assertNotIn("deliveryIntent", self.dispatches()[0])
        self.assertEqual(self.dispatches()[0]["dispatchMode"], {"type": "steer_active", "targetRunId": "r1"})

    async def test_completed_steer_does_not_finish_thread_with_other_work_pending(self):
        await self.launch()
        await self.send("steer")
        self.projection["runs"][0]["status"] = "completed"
        self.projection["runs"].append({"id": "other", "ordinal": 2, "userMessageId": "external", "status": "queued"})
        observed = await self.engine.call("read_thread", {"threadId": self.thread}, OWNER)
        self.assertEqual(observed["status"], "background")
        self.assertFalse(observed["settled"])


class SteeringObservation(unittest.TestCase):
    def test_steering_tracks_linked_message_not_a_later_unrelated_run(self):
        operation = {"thread_id": "thread", "message_id": "steering-message", "state": "accepted", "kind": "send",
                     "intent": {"dispatchMode": {"type": "steer_active", "targetRunId": "active"}}}
        projection = {"thread": {"id": "thread"}, "runs": [
            {"id": "active", "ordinal": 1, "userMessageId": "initial", "status": "completed"},
            {"id": "other", "ordinal": 2, "userMessageId": "unrelated", "status": "queued"}],
            "messages": [{"id": "steering-message", "role": "user", "runId": "active", "text": "Steer"},
                         {"role": "assistant", "runId": "active", "text": "Handled steering"}]}
        observed = observe(operation, projection)
        self.assertEqual((observed["runId"], observed["lastAssistantMessage"]), ("active", "Handled steering"))
        projection["messages"][0]["runId"] = "other"
        projection["runs"][1]["status"] = "running"
        observed = observe(operation, projection)
        self.assertEqual((observed["runId"], observed["status"]), ("other", "running"))


if __name__ == "__main__":
    unittest.main()
