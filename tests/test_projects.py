import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from client import MutationUnknown, Target, load_targets, save_target
from engine import Engine
from test_core import ENV, OWNER, repo

MODEL = {"instanceId": "claudeAgent", "model": "test-model"}


class FakeClient:
    projects = []
    calls = []
    fail_create = False

    def __init__(self, target, credentials):
        self.target = target

    async def shell(self):
        return {"projects": [dict(project) for project in self.projects], "threads": []}

    async def providers(self):
        return [{"instanceId": "claudeAgent", "models": ["test-model"], "ready": True, "displayName": "Claude", "driver": "claudeAgent"}]

    async def rpc(self, name, payload):
        FakeClient.calls.append((name, dict(payload)))
        if name == "projects.mutate":
            if self.fail_create:
                raise MutationUnknown("Synthetic failure")
            if not any(project["id"] == payload["projectId"] for project in self.projects):
                FakeClient.projects.append({"id": payload["projectId"], "title": payload["title"], "workspaceRoot": payload["workspaceRoot"]})
            return next(project for project in self.projects if project["id"] == payload["projectId"])
        return {"threadId": payload["threadId"], "sequence": 1}


class Projects(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, self.state = self.root / "config", self.root / "state"
        save_target(self.config, Target("box", "Box", "https://t3.example.test", ENV, kind="remote"))
        FakeClient.projects = [{"id": "p-alpha", "title": "Alpha", "workspaceRoot": "/srv/fixture/alpha"},
                               {"id": "p-beta", "title": "Beta", "workspaceRoot": "/srv/fixture/beta"}]
        FakeClient.calls, FakeClient.fail_create = [], False
        self.enterContext(patch("engine.Client", FakeClient))
        self.engine = Engine(self.config, self.state)

    async def spawn(self, key="k1", **changes):
        arguments = {"target": "box", "requestKey": key, "workspace": "shared", "prompt": "Reply OK", "modelSelection": MODEL, **changes}
        await self.engine.call("spawn_thread", arguments, OWNER)
        return [call[1] for call in FakeClient.calls if call[0] == "orchestration.launchThread"][-1]

    async def test_spawn_by_project_id(self):
        payload = await self.spawn(projectId="p-beta")
        self.assertEqual((payload["projectId"], payload["workspaceStrategy"]), ("p-beta", {"type": "root"}))

    async def test_spawn_by_title_is_case_insensitive(self):
        self.assertEqual((await self.spawn(projectTitle="aLPHA"))["projectId"], "p-alpha")

    async def test_single_project_is_the_default(self):
        FakeClient.projects = FakeClient.projects[:1]
        self.assertEqual((await self.spawn())["projectId"], "p-alpha")

    async def test_ambiguous_or_unknown_project_lists_choices(self):
        for index, changes in enumerate(({}, {"projectTitle": "Gamma"}, {"projectId": "p-missing"})):
            with self.assertRaises(ValueError) as caught:
                await self.spawn(key=f"k{index}", **changes)
            self.assertIn("Alpha (p-alpha)", str(caught.exception))
        FakeClient.projects.append({"id": "p-dup", "title": "alpha", "workspaceRoot": "/srv/fixture/dup"})
        with self.assertRaises(ValueError):
            await self.spawn(key="dup", projectTitle="Alpha")
        self.assertFalse(any(call[0] == "orchestration.launchThread" for call in FakeClient.calls))

    async def test_legacy_binding_is_the_default_project_and_others_stay_reachable(self):
        legacy = {"version": 2, "targets": [{"id": "box", "label": "Box", "origin": "https://t3.example.test", "environment_id": ENV, "kind": "remote",
                                             "project_id": "p-beta", "project_root": "/srv/fixture/beta", "web_origin": None, "runtime_mode": "auto"}]}
        (self.config / "targets.json").write_text(json.dumps(legacy))
        self.assertEqual(load_targets(self.config)[0].project_id, "p-beta")
        payload = await self.spawn()
        self.assertEqual((payload["projectId"], payload["runtimeMode"]), ("p-beta", "auto"))
        self.assertEqual((await self.spawn(key="k2", projectId="p-alpha"))["projectId"], "p-alpha")
        listed = await self.engine.call("list_projects", {"target": "box"}, OWNER)
        self.assertEqual([(row["id"], row["default"]) for row in listed["projects"]], [("p-alpha", False), ("p-beta", True)])

    async def test_remote_still_refuses_checkout_and_worktree(self):
        for changes in ({"checkout": "/tmp/x"}, {"workspace": "worktree"}):
            with self.assertRaises(ValueError):
                await self.spawn(projectId="p-alpha", **changes)

    async def test_local_checkout_keeps_git_identity_resolution(self):
        checkout = repo(self.root / "repo")
        save_target(self.config, Target("mac", "Mac", "http://127.0.0.1:3773", ENV.replace(ENV[0], "a" if ENV[0] != "a" else "b", 1)))
        FakeClient.projects.append({"id": "p-local", "title": "Repo", "workspaceRoot": str(checkout)})
        payload = await self.spawn(target="mac", checkout=str(checkout))
        self.assertEqual(payload["projectId"], "p-local")

    async def test_add_project_payload(self):
        result = await self.engine.call("add_project", {"target": "box", "requestKey": "add-gamma", "workspaceRoot": "/srv/fixture/gamma/"}, OWNER)
        name, payload = FakeClient.calls[-1]
        self.assertEqual(name, "projects.mutate")
        self.assertEqual(set(payload), {"type", "commandId", "projectId", "title", "workspaceRoot", "createWorkspaceRootIfMissing"})
        self.assertEqual((payload["type"], payload["title"], payload["workspaceRoot"], payload["createWorkspaceRootIfMissing"]),
                         ("project.create", "gamma", "/srv/fixture/gamma/", False))
        self.assertTrue(result["created"])
        self.assertEqual(result["project"]["id"], payload["projectId"])
        titled = await self.engine.call("add_project", {"target": "box", "requestKey": "add-delta", "workspaceRoot": "/srv/fixture/delta", "title": "Delta App"}, OWNER)
        self.assertEqual(titled["project"]["title"], "Delta App")

    async def test_add_project_is_idempotent(self):
        first = await self.engine.call("add_project", {"target": "box", "requestKey": "add-gamma", "workspaceRoot": "/srv/fixture/gamma"}, OWNER)
        again = await self.engine.call("add_project", {"target": "box", "requestKey": "other-key", "workspaceRoot": "/srv/fixture/gamma/"}, OWNER)
        self.assertEqual((again["created"], again["project"]["id"]), (False, first["project"]["id"]))
        existing = await self.engine.call("add_project", {"target": "box", "requestKey": "add-alpha", "workspaceRoot": "/srv/fixture/alpha"}, OWNER)
        self.assertEqual((existing["created"], existing["project"]["id"]), (False, "p-alpha"))
        self.assertEqual(len([call for call in FakeClient.calls if call[0] == "projects.mutate"]), 1)

    async def test_add_project_retry_reuses_identifiers_and_reports_failure(self):
        FakeClient.fail_create = True
        with self.assertRaises(ValueError):
            await self.engine.call("add_project", {"target": "box", "requestKey": "add-gamma", "workspaceRoot": "/srv/fixture/gamma"}, OWNER)
        FakeClient.fail_create = False
        await self.engine.call("add_project", {"target": "box", "requestKey": "add-gamma", "workspaceRoot": "/srv/fixture/gamma"}, OWNER)
        first, second = [call[1] for call in FakeClient.calls if call[0] == "projects.mutate"]
        self.assertEqual(first, second)

    async def test_add_project_changed_arguments_never_reuse_identifiers(self):
        await self.engine.call("add_project", {"target": "box", "requestKey": "same", "workspaceRoot": "/srv/fixture/gamma"}, OWNER)
        await self.engine.call("add_project", {"target": "box", "requestKey": "same", "workspaceRoot": "/srv/fixture/delta"}, OWNER)
        first, second = [call[1] for call in FakeClient.calls if call[0] == "projects.mutate"]
        self.assertNotEqual(first["projectId"], second["projectId"])
        self.assertNotEqual(first["commandId"], second["commandId"])

    async def test_add_project_title_uses_the_windows_folder_name(self):
        result = await self.engine.call("add_project", {"target": "box", "requestKey": "win", "workspaceRoot": "C:\\Users\\dev\\Gamma"}, OWNER)
        self.assertEqual(result["project"]["title"], "Gamma")

    async def test_local_explicit_project_must_match_the_checkout(self):
        checkout = repo(self.root / "repo")
        save_target(self.config, Target("mac", "Mac", "http://127.0.0.1:3773", ENV))
        FakeClient.projects.append({"id": "p-local", "title": "Repo", "workspaceRoot": str(checkout)})
        with self.assertRaises(ValueError):
            await self.spawn(target="mac", checkout=str(checkout), projectId="p-alpha")
        with self.assertRaises(ValueError):
            await self.spawn(key="rel", target="mac", checkout="relative/path", projectId="p-local")
        self.assertEqual((await self.spawn(key="ok", target="mac", checkout=str(checkout), projectTitle="repo"))["projectId"], "p-local")

    async def test_add_project_rejects_empty_or_relative_paths(self):
        for root in ("   ", "coding/app", "./app", "~/coding/app"):
            with self.assertRaises(ValueError):
                await self.engine.call("add_project", {"target": "box", "requestKey": "bad", "workspaceRoot": root}, OWNER)
        self.assertEqual(FakeClient.calls, [])


if __name__ == "__main__":
    unittest.main()
