import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import types
from mcp.server.lowlevel import NotificationOptions

from client import Target, save_target
from domain import delegation_card, digest
from server import CARD_MIME, CARD_URI, build_server
from test_core import ENV, OWNER, repo
from test_engine import FakeClient


def row(**changed):
    data = {"n": 1, "threadId": "019f0aae-aaaa-4222-8333-444455556666", "environment": ENV,
            "title": "Review", "status": "running", "project": "repo", "place": "main checkout",
            "provider": "claudeAgent", "model": "test-model", "round": 1, "webOrigin": None}
    data.update(changed)
    return data


class Apps(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = repo(self.root / "repo")
        self.config = self.root / "config"
        self.state = self.root / "state"
        save_target(self.config, Target("mac", "Mac", "http://127.0.0.1:3773", ENV, runtime_mode="approval-required",
                                        web_origin="https://nightly.example"))
        FakeClient.projects = [{"id": "p1", "workspaceRoot": str(self.repo), "title": "Repo"}]
        FakeClient.calls, FakeClient.projections = [], {}
        FakeClient.lose_before_commit = FakeClient.lose_after_commit = False
        self.enterContext(patch("engine.Client", FakeClient))
        self.app = build_server(self.config, self.state)

    async def tools(self):
        result = await self.app.request_handlers[types.ListToolsRequest](types.ListToolsRequest(method="tools/list"))
        return result.root.tools

    async def call(self, name, arguments):
        request = types.CallToolRequest(method="tools/call", params={"name": name, "arguments": arguments})
        result = await self.app.request_handlers[types.CallToolRequest](request)
        return result.root

    async def test_resource_is_registered_without_csp(self):
        caps = self.app.get_capabilities(NotificationOptions(), {})
        self.assertIsNotNone(caps.resources)
        listed = await self.app.request_handlers[types.ListResourcesRequest](types.ListResourcesRequest(method="resources/list"))
        resource = listed.root.resources[0]
        dumped = resource.model_dump(by_alias=True, mode="json", exclude_none=True)
        self.assertEqual(str(resource.uri), CARD_URI)
        self.assertEqual(resource.mimeType, CARD_MIME)
        self.assertNotIn("_meta", dumped)
        read = await self.app.request_handlers[types.ReadResourceRequest](
            types.ReadResourceRequest(method="resources/read", params={"uri": CARD_URI}))
        body = read.root.contents[0]
        self.assertEqual(body.mimeType, CARD_MIME)
        html = body.text
        self.assertIn("ui/initialize", html)
        self.assertIn("ui/notifications/initialized", html)
        self.assertIn("delegation_status", html)
        self.assertIn("check-in", html)
        self.assertNotIn("20000", html, "fixed 20 s polling is gone")
        for needle in ("WAIT_SECONDS = 25", "args.waitSeconds = WAIT_SECONDS", "args.since = card.signature", "signature: row.signature",
                       "age < 60000 ? 3000 : age < 300000 ? 10000 : 30000", "longPoll = false", "setInterval(", "MAX_ERRORS"):
            self.assertIn(needle, html)
        self.assertIn("ui/open-link", html)
        self.assertIn("httpOnly", html)
        self.assertIn("appScheme", html)
        # Opus card: the LINKS table plus links() replace the old openTarget()
        # (t3code:// first, else https, per host mode); #open sends its dataset.url.
        self.assertIn("function links()", html)
        self.assertIn('LINKS[urls.app ? "app/" + mode : urls.web ? "web/" + mode : "none"]', html)
        self.assertIn('"app/open"', html)
        self.assertIn('request("ui/open-link", { url: $("open").dataset.url })', html)

        self.assertIn('id="hint"', html)
        self.assertIn("Open app", html)
        self.assertIn("The host refused to open the link", html)
        self.assertIn("t3code:", html)
        self.assertNotIn("fetch(", html)
        self.assertNotIn("XMLHttpRequest", html)
        self.assertNotIn('href="t3code', html)
        self.assertNotIn("open-link\", { url: card.appUrl", html)
        self.assertNotIn("webhook", html.lower())

    async def test_only_show_delegation_carries_ui_meta(self):
        names = []
        for tool in await self.tools():
            dumped = tool.model_dump(by_alias=True, mode="json", exclude_none=True)
            names.append(tool.name)
            if tool.name == "show_delegation":
                self.assertEqual(dumped["_meta"]["ui"]["resourceUri"], CARD_URI)
                self.assertEqual(dumped["_meta"]["ui"]["visibility"], ["model", "app"])
                self.assertNotIn("csp", dumped["_meta"]["ui"])
            else:
                self.assertNotIn("_meta", dumped)
                self.assertNotIn("meta", dumped)
        for data_tool in ("spawn_thread", "await_thread", "delegation_status", "list_targets", "list_threads"):
            self.assertIn(data_tool, names)

    async def test_show_delegation_keeps_the_markdown_digest(self):
        from engine import Engine
        arguments = {"requestKey": "review", "checkout": str(self.repo), "workspace": "shared", "prompt": "Read only",
                     "title": "Review", "modelSelection": {"instanceId": "claudeAgent", "model": "test-model"}, "owner": OWNER}
        launch = await Engine(self.config, self.state).call("spawn_thread", arguments, OWNER)
        result = await self.call("show_delegation", {"threadId": launch["threadId"], "phase": "handoff", "owner": OWNER})
        self.assertFalse(result.isError)
        text = result.content[0].text
        self.assertNotIn("Card: expand", text)
        self.assertIn("**Handoff**", text)
        self.assertIn("\n```\nt3code://threads/", text)
        self.assertNotIn("nightly.example", text)
        self.assertNotIn("open in T3", text)
        card = result.structuredContent
        self.assertEqual(card["digest"], text)
        self.assertEqual(card["phase"], "handoff")
        self.assertEqual(card["threadId"], launch["threadId"])
        self.assertTrue(card["httpsUrl"].startswith("https://nightly.example/"))
        self.assertTrue(card["appUrl"].startswith("t3code://threads/"))
        self.assertNotIn("webUrl", card)
        self.assertNotIn("latestAssistantText", card)
        self.assertNotIn(OWNER, json.dumps(card))
        status = await self.call("delegation_status", {"threadIds": [launch["threadId"]], "moment": "check-in", "owner": OWNER})
        self.assertEqual(status.content[0].text, status.structuredContent["digest"])
        self.assertNotIn("Card: expand", status.content[0].text)
        self.assertIn("**Check-in**", status.content[0].text)
        legacy = await self.call("show_delegation", {"threadId": launch["threadId"], "phase": "encargo", "owner": OWNER})  # deprecated alias
        self.assertFalse(legacy.isError)
        self.assertEqual(legacy.structuredContent["phase"], "handoff")
        legacy_status = await self.call("delegation_status", {"threadIds": [launch["threadId"]], "moment": "seguimiento", "owner": OWNER})  # deprecated alias
        self.assertIn("**Check-in**", legacy_status.content[0].text)

    def test_spanish_phase_and_moment_values_are_deprecated_aliases(self):
        for alias, canonical in (("encargo", "handoff"), ("aviso", "update"), ("cierre", "wrap-up")):
            self.assertEqual(delegation_card(row(), alias, "x")["phase"], canonical)
        self.assertIn("**Wrap-up**", digest([row()], "cierre", None))  # deprecated alias
        with self.assertRaises(ValueError):
            delegation_card(row(), "check-in", "x")
        with self.assertRaises(ValueError):
            digest([row()], "later", None)

    def test_digest_is_compact_and_demo_safe(self):
        rows = [row(target="mac", webOrigin="https://demo-host.example:3773", place="scratch folder Users/someone/.t3/scratch/x"),
                row(n=2, threadId="019f0aae-1111-4222-8333-777788889999", status="completed", target="mac",
                    webOrigin="https://demo-host.example:3773", place="main checkout")]
        text = digest(rows, "wrap-up", 0)
        for leak in ("ts.net", "demo-host", "example", "https://", "http://", "Users/", "scratch", "🌐", "open in T3"):
            self.assertNotIn(leak, text)
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("**Wrap-up** · repo · mac · seen "))
        self.assertIn("**#1 · Working · Review**  ", lines)
        self.assertIn("claudeAgent · ` test-model ` · 019f0aae", lines)
        self.assertEqual(text.count("```\nt3code://threads/"), 2)
        block = lines.index(f"t3code://threads/{ENV}/019f0aae-aaaa-4222-8333-444455556666")
        self.assertEqual((lines[block - 1], lines[block + 1]), ("```", "```"))
        mixed = digest([row(target="mac"), row(n=2, target="omarchy")], "check-in", None)
        self.assertIn("several projects or targets", mixed)
        self.assertIn("· repo · omarchy", mixed)

    async def test_missing_thread_is_an_error_without_a_card(self):
        result = await self.call("show_delegation", {"threadId": "019f0aae-aaaa-4222-8333-444455556666", "phase": "handoff", "owner": OWNER})
        self.assertTrue(result.isError)
        self.assertTrue(result.content[0].text)
        self.assertIsNone(result.structuredContent)

    def test_card_carries_driver_and_model_only_when_present(self):
        card = delegation_card(row(provider="grok_og", driver="grok", model="grok-4"), "handoff", "digest")
        self.assertEqual((card["driver"], card["model"]), ("grok", "grok-4"))
        bare = delegation_card(row(model=None, driver=None), "handoff", "digest")
        self.assertNotIn("driver", bare)
        self.assertNotIn("model", bare)
        self.assertNotIn("driver", delegation_card(row(), "handoff", "digest"))
        self.assertNotIn("grok_og", json.dumps(card))

    def test_card_urls_follow_the_open_policy(self):
        https = delegation_card(row(webOrigin="https://nightly.example"), "handoff", "digest")
        self.assertEqual(https["httpsUrl"], f"https://nightly.example/{ENV}/019f0aae-aaaa-4222-8333-444455556666")
        self.assertTrue(https["appUrl"].startswith("t3code://threads/"))
        self.assertNotIn("webUrl", https)
        local = delegation_card(row(webOrigin="http://127.0.0.1:3773"), "update", "digest")
        self.assertNotIn("httpsUrl", local)
        self.assertTrue(local["webUrl"].startswith("http://127.0.0.1:3773/"))
        self.assertTrue(local["appUrl"].startswith("t3code://threads/"))
        bare = row()
        text = digest([bare], "wrap-up", None)
        self.assertNotIn("Card: expand", text)
        self.assertIn(f"```\nt3code://threads/{ENV}/019f0aae-aaaa-4222-8333-444455556666\n```", text)
        self.assertNotIn("](https://", text)
        self.assertNotIn("](http://", text)
        local_text = digest([row(webOrigin="http://127.0.0.1:3773")], "wrap-up", None)
        self.assertIn("```\nt3code://threads/", local_text)
        self.assertNotIn("127.0.0.1", local_text)
        bare_card = delegation_card(bare, "handoff", text)
        self.assertTrue(bare_card["appUrl"].startswith("t3code://threads/"))
        self.assertNotIn("httpsUrl", bare_card)
        self.assertNotIn("webUrl", bare_card)
        poisoned = delegation_card(row(webOrigin="javascript:alert(1)", latestAssistantText="SECRET TRANSCRIPT"), "wrap-up", "short")
        blob = json.dumps(poisoned)
        self.assertNotIn("javascript:", blob)
        self.assertNotIn("SECRET", blob)
        self.assertEqual(poisoned["status"], "running")
        self.assertEqual(delegation_card(row(status="failed"), "wrap-up", "x")["status"], "error")
        self.assertEqual(delegation_card(row(status="completed"), "handoff", "x")["status"], "done")

    def test_card_tells_waiting_and_stopped_apart_from_running_and_done(self):
        self.assertEqual(delegation_card(row(status="waiting_for_input"), "update", "x")["status"], "waiting")
        self.assertEqual(delegation_card(row(status="waiting_for_approval"), "update", "x")["status"], "waiting")
        self.assertEqual(delegation_card(row(status="stopped"), "wrap-up", "x")["status"], "stopped")

    def test_card_carries_progress_fields_only_when_present(self):
        progress = {"startedAt": "2026-10-07T17:25:12.500Z", "completedAt": "2026-10-07T17:27:27.507Z", "excerpt": "Running `ls -t` now."}
        card = delegation_card(row(status="completed", **progress), "wrap-up", "digest")
        self.assertEqual({key: card.get(key) for key in progress}, progress)
        bare = delegation_card(row(startedAt=None, excerpt=""), "handoff", "digest")
        self.assertEqual({key for key in progress if key in bare}, set())

    def test_card_carries_the_pending_ask_only_when_present(self):
        ask = {"kind": "command", "text": "touch /tmp/t3card/probe/claude-approval-probe.txt · Create probe file"}
        self.assertEqual(delegation_card(row(status="waiting_for_approval", ask=ask), "update", "digest")["ask"], ask)
        self.assertNotIn("ask", delegation_card(row(status="waiting_for_approval"), "update", "digest"))
