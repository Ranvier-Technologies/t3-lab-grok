import asyncio
import os
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.shared.memory import create_connected_server_and_client_session

from client import Target, save_target
from engine import Engine
from server import build_server
from test_core import ENV, OWNER, repo
from test_engine import FakeClient


class SlowClient(FakeClient):
    """FakeClient whose projection read can be made to block without touching the event loop."""
    delay = 0.0

    async def projection(self, thread_id):
        if SlowClient.delay:
            await asyncio.sleep(SlowClient.delay)
        return await super().projection(thread_id)


def running(projection):
    projection = copy.deepcopy(projection)
    projection["runs"][-1]["status"] = "running"
    return projection


class Wait(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = repo(self.root / "repo")
        self.config, self.state = self.root / "config", self.root / "state"
        save_target(self.config, Target("mac", "Mac", "http://127.0.0.1:3773", ENV, runtime_mode="approval-required"))
        FakeClient.projects = [{"id": "p1", "workspaceRoot": str(self.repo), "title": "Repo"}]
        FakeClient.calls, FakeClient.projections = [], {}
        FakeClient.lose_before_commit = FakeClient.lose_after_commit = False
        SlowClient.delay = 0.0
        self.enterContext(patch("engine.Client", SlowClient))

    async def launch(self, engine):
        arguments = {"requestKey": "wait", "checkout": str(self.repo), "workspace": "shared", "prompt": "Read only",
                     "title": "Wait", "modelSelection": {"instanceId": "claudeAgent", "model": "test-model"}}
        launch = await engine.call("spawn_thread", arguments, OWNER)
        thread = launch["threadId"]
        FakeClient.projections[thread] = running(FakeClient.projections[thread])
        return thread

    async def test_server_handles_a_quick_call_while_a_long_wait_is_in_flight(self):
        app = build_server(self.config, self.state)
        engine = Engine(self.config, self.state)
        thread = await self.launch(engine)
        SlowClient.delay = 5.0
        async with create_connected_server_and_client_session(app) as session:
            started = time.monotonic()
            slow = asyncio.create_task(session.call_tool("read_thread", {"threadId": thread, "owner": OWNER}))
            await asyncio.sleep(0.2)
            quick = await session.call_tool("list_targets", {})
            quick_at = time.monotonic() - started
            result = await slow
            slow_at = time.monotonic() - started
        self.assertFalse(quick.isError)
        self.assertFalse(result.isError)
        self.assertLess(quick_at, 1.5, "quick call waited for the slow one")
        self.assertGreaterEqual(slow_at, 4.9)
        print(f"\nconcurrency: list_targets returned at {quick_at:.2f}s while read_thread (5 s block) returned at {slow_at:.2f}s")

    async def test_status_wait_returns_early_on_change_and_holds_otherwise(self):
        engine = Engine(self.config, self.state)
        engine.wait_interval = 0.05
        thread = await self.launch(engine)
        first = await engine.call("delegation_status", {"threadIds": [thread], "moment": "check-in"}, OWNER)
        self.assertEqual(first["rows"][0]["status"], "running")
        self.assertNotIn("wait", first)
        since = first["signature"]

        started = time.monotonic()
        held = await engine.call("delegation_status", {"threadIds": [thread], "moment": "check-in", "waitSeconds": 1, "since": since}, OWNER)
        self.assertGreaterEqual(time.monotonic() - started, 0.95)
        self.assertEqual((held["wait"]["changed"], held["wait"]["timedOut"]), (False, True))

        async def finish():
            await asyncio.sleep(0.3)
            FakeClient.projections[thread]["runs"][-1]["status"] = "completed"
        started = time.monotonic()
        changed, _ = await asyncio.gather(
            engine.call("delegation_status", {"threadIds": [thread], "moment": "check-in", "waitSeconds": 10, "since": since}, OWNER), finish())
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(changed["rows"][0]["status"], "completed")
        self.assertEqual((changed["wait"]["changed"], changed["wait"]["timedOut"]), (True, False))
        self.assertNotEqual(changed["signature"], since)

        started = time.monotonic()
        stale = await engine.call("delegation_status", {"threadIds": [thread], "moment": "check-in", "waitSeconds": 10, "since": since}, OWNER)
        self.assertLess(time.monotonic() - started, 0.5, "a stale signature returns immediately")
        self.assertTrue(stale["wait"]["changed"])

    def test_signature_is_fixed_length_and_unambiguous(self):
        from domain import row_signature
        long = row_signature({"status": "failed", "lastError": "x" * 100000, "round": 1})
        self.assertEqual(len(long), 32)
        self.assertNotEqual(row_signature({"status": "a|b", "round": 1}), row_signature({"status": "a", "ask": {"kind": "b"}, "round": 1}))
        self.assertEqual(row_signature({"status": "running", "round": 1}), row_signature({"status": "running", "round": 1}))

    async def test_owner_file_fallback_uses_the_server_config_root(self):
        owner = "019f0aae-1111-4222-8333-777788889999"
        file = self.config / "owner"
        file.write_text(owner)
        file.chmod(0o600)
        app = build_server(self.config, self.state)
        engine = Engine(self.config, self.state)
        await self.launch(engine)
        with patch.dict("os.environ", {"T3_LAB_V2_CONFIG_DIR": str(self.root / "unrelated")}):
            os.environ.pop("T3_LAB_OWNER", None)
            async with create_connected_server_and_client_session(app) as session:
                result = await session.call_tool("list_threads", {})
        self.assertFalse(result.isError, result.content)
        self.assertEqual(result.structuredContent["threads"], [])

    async def test_card_signature_matches_status_signature(self):
        app = build_server(self.config, self.state)
        engine = Engine(self.config, self.state)
        thread = await self.launch(engine)
        async with create_connected_server_and_client_session(app) as session:
            card = await session.call_tool("show_delegation", {"threadId": thread, "phase": "handoff", "owner": OWNER})
            status = await session.call_tool("delegation_status", {"threadIds": [thread], "moment": "check-in", "waitSeconds": 1,
                                                                    "since": card.structuredContent["signature"], "owner": OWNER})
        self.assertFalse(status.isError)
        self.assertEqual(status.structuredContent["signature"], card.structuredContent["signature"])
        self.assertTrue(status.structuredContent["wait"]["timedOut"])
        row = status.structuredContent["rows"][0]
        for key in ("status", "signature", "threadId", "environment"):
            self.assertIn(key, row)


if __name__ == "__main__":
    unittest.main()
