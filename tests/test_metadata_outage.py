import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from client import Client, Credentials, Target, save_target
from engine import Engine
from journal import Journal
from test_core import ENV, OWNER


class MetadataOutage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.credentials = Credentials(self.root / "config")

    async def test_closed_metadata_socket_is_recoverable_for_all_callers(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            target = Target("fixture", "Fixture", f"http://127.0.0.1:{reserved.getsockname()[1]}", ENV)
            for method in ("public_metadata", "shell", "providers"):
                with self.subTest(method=method):
                    with self.assertRaisesRegex(ValueError, "public metadata"):
                        await getattr(Client(target, self.credentials, timeout=0.3), method)()

    async def test_stalled_metadata_is_recoverable_and_sends_no_authorization(self):
        release = asyncio.Event()
        finished = asyncio.Event()
        requests = []
        async def stall(reader, writer):
            try:
                requests.append((await reader.readuntil(b"\r\n\r\n")).decode())
                await release.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()
        server = await asyncio.start_server(stall, "127.0.0.1", 0)
        try:
            target = Target("fixture", "Fixture", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", ENV)
            with self.assertRaisesRegex(ValueError, "public metadata"):
                await Client(target, self.credentials, timeout=0.1).public_metadata()
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(len(requests), 1)
        self.assertIn("GET /.well-known/t3/environment ", requests[0])
        self.assertNotIn("authorization:", requests[0].lower())

    async def test_real_connection_failure_preserves_unknown_and_suppresses_repeat(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            save_target(self.root / "config", Target("fixture", "Fixture", f"http://127.0.0.1:{reserved.getsockname()[1]}", ENV))
            journal = Journal(self.root / "state", OWNER)
            intent = {"projectId": "fixture-project", "title": "Recorded task", "modelSelection": {"instanceId": "opencode", "model": "fixture-model"},
                "runtimeMode": "approval-required", "interactionMode": "default", "workspaceStrategy": {"type": "root"}, "creationSource": "mcp",
                "initialMessage": {"text": "Read only", "attachments": []}}
            recorded = journal.prepare("recorded-task", "launch", "fixture", ENV, intent)
            journal.mark("recorded-task", "accepted", {"threadId": recorded["thread_id"]})
            engine = Engine(self.root / "config", self.root / "state")
            observed = await engine.call("read_thread", {"threadId": recorded["thread_id"]}, OWNER)
            self.assertEqual(observed["status"], "unknown")
            self.assertFalse(observed["settled"])
            self.assertIsNone(observed["lastAssistantMessage"])
            self.assertIsNone(observed["latestAssistantText"])
            self.assertIsNone(observed["observedAt"])
            self.assertFalse(observed["canContinue"])
            notice = {"threadIds": [recorded["thread_id"]], "moment": "update"}
            first = await engine.call("delegation_status", notice, OWNER)
            second = await engine.call("delegation_status", notice, OWNER)
            self.assertTrue(first["changed"])
            self.assertIn("No data", first["digest"])
            self.assertIn("no observation yet", first["digest"])
            self.assertFalse(second["changed"])
            self.assertEqual(second["digest"], "No changes since the last update. Do not write anything to the user.")
            self.assertEqual(journal.get("recorded-task")["payload"], recorded["payload"])
            self.assertEqual(journal.get("recorded-task")["state"], "accepted")
