import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from domain import observe, owner_from_meta, resolve_owner, resolve_project
from journal import Journal


OWNER = "019f0aae-1111-4222-8333-444455556666"
OTHER = "019f0aae-1111-4222-8333-777788889999"
ENV = "7d2a1c4e-3b5f-4a6d-9e8c-1f2a3b4c5d6e"


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True, text=True).stdout.strip()


def repo(directory):
    directory.mkdir()
    git(directory, "init", "-q")
    git(directory, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "Initial")
    return directory


class Core(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.intent = {"projectId": "project-test", "title": "Read only", "modelSelection": {"instanceId": "claudeAgent", "model": "test-model"},
                       "runtimeMode": "approval-required", "interactionMode": "default", "workspaceStrategy": {"type": "root"},
                       "initialMessage": {"text": "Read the code", "attachments": []}}

    def prepare(self, journal=None, key="review", **extra):
        return (journal or Journal(self.root, OWNER)).prepare(key, "launch", "mac", ENV, self.intent, **extra)

    def test_owner_is_host_thread_id_not_session_tree_or_process(self):
        self.assertEqual(owner_from_meta({"threadId": OWNER, "sessionId": OTHER}), OWNER)
        for meta in ({}, {"sessionId": OWNER}, {"threadId": "../other"}):
            with self.assertRaises(ValueError):
                owner_from_meta(meta)

    def test_resolve_owner_prefers_meta_then_arg_then_env(self):
        self.assertEqual(resolve_owner({"threadId": OWNER}, {"owner": OTHER}, {"T3_LAB_OWNER": OTHER}), OWNER)
        self.assertEqual(resolve_owner({}, {"owner": OWNER}, {"T3_LAB_OWNER": OTHER}), OWNER)
        self.assertEqual(resolve_owner({}, {}, {"T3_LAB_OWNER": OWNER}), OWNER)
        self.assertEqual(resolve_owner({"threadId": "../bad"}, {"owner": OWNER}, {}), OWNER)
        with self.assertRaises(ValueError):
            resolve_owner({}, {}, {}, owner_root=self.root)
        with self.assertRaises(ValueError):
            resolve_owner({}, {"owner": "not-a-uuid"}, {"T3_LAB_OWNER": "also-bad"}, owner_root=self.root)

    def test_resolve_owner_falls_back_to_private_owner_file(self):
        file = self.root / "owner"
        file.write_text(OTHER + "\n")
        file.chmod(0o600)
        self.assertEqual(resolve_owner({}, {}, {}, owner_root=self.root), OTHER)
        self.assertEqual(resolve_owner({}, {}, {"T3_LAB_OWNER": OWNER}, owner_root=self.root), OWNER)
        self.assertEqual(resolve_owner({}, {}, {"T3_LAB_V2_CONFIG_DIR": str(self.root)}), OTHER)
        file.chmod(0o644)
        with self.assertRaises(ValueError):
            resolve_owner({}, {}, {}, owner_root=self.root)

    def test_saved_operation_keeps_exact_payload_and_identifiers_after_restart(self):
        before = self.prepare()
        self.assertEqual(before["state"], "prepared")
        self.assertEqual(before["payload"]["commandId"], before["command_id"])
        self.assertEqual(before["payload"]["threadId"], before["thread_id"])
        self.assertEqual(before["payload"]["initialMessage"]["messageId"], before["message_id"])
        Journal(self.root, OWNER).mark("review", "unknown")
        resumed = self.prepare()
        self.assertEqual(resumed["payload"], before["payload"])
        self.assertEqual(resumed["thread_id"], before["thread_id"])
        self.assertEqual(resumed["state"], "unknown")
        self.assertEqual(len(Journal(self.root, OWNER).threads()), 1)

    def test_same_key_with_changed_intent_refuses_and_retains_original(self):
        before = self.prepare()
        changed = {**self.intent, "title": "Not the original request"}
        with self.assertRaises(ValueError):
            Journal(self.root, OWNER).prepare("review", "launch", "mac", ENV, changed)
        self.assertEqual(Journal(self.root, OWNER).get("review")["payload"], before["payload"])

    def test_two_chats_cannot_list_or_continue_each_others_children(self):
        first = self.prepare()
        other = Journal(self.root, OTHER)
        self.assertEqual(other.threads(), [])
        with self.assertRaises(ValueError):
            other.latest(first["thread_id"])
        second = self.prepare(other)
        self.assertNotEqual(first["thread_id"], second["thread_id"])

    def test_parallel_repeated_preparation_creates_one_operation(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: self.prepare(), range(16)))
        self.assertEqual(len({value["command_id"] for value in values}), 1)
        self.assertEqual(len(Journal(self.root, OWNER).threads()), 1)

    def test_failed_durable_write_does_not_create_a_retry_record(self):
        blocked = self.root / "not-a-directory"
        blocked.write_text("blocked")
        with self.assertRaises(OSError):
            Journal(blocked, OWNER).prepare("review", "launch", "mac", ENV, self.intent)

    def test_unknown_send_hides_prior_response_and_refused_send_preserves_it(self):
        journal = Journal(self.root, OWNER)
        launch = self.prepare(journal)
        journal.mark("review", "accepted", {"sequence": 4})
        send = journal.prepare("round-two", "send", "mac", ENV,
            {"text": "Second round", "attachments": [], "createdBy": "user", "creationSource": "web", "dispatchMode": {"type": "start_immediately"}}, launch["thread_id"])
        journal.mark("round-two", "unknown")
        projection = {"thread": {"id": launch["thread_id"]}, "runs": [{"id": "r1", "userMessageId": launch["message_id"], "status": "completed"}],
                      "messages": [{"role": "assistant", "runId": "r1", "text": "Old reply"}]}
        observed = observe(journal.latest(launch["thread_id"]), projection)
        self.assertEqual(observed["status"], "unknown")
        self.assertIsNone(observed["lastAssistantMessage"])
        journal.mark("round-two", "refused")
        retained = observe(journal.latest(launch["thread_id"]), projection)
        self.assertEqual(retained["lastAssistantMessage"], "Old reply")
        self.assertEqual(send["payload"]["messageId"], send["message_id"])

    def test_only_the_acknowledged_rounds_reply_is_returned(self):
        operation = self.prepare()
        operation["state"] = "accepted"
        projection = {"thread": {"id": operation["thread_id"]},
            "runs": [{"id": "old", "ordinal": 1, "userMessageId": "old-message", "status": "completed"},
                     {"id": "new", "ordinal": 2, "userMessageId": operation["message_id"], "status": "completed"}],
            "messages": [{"role": "assistant", "runId": "old", "text": "Old"}, {"role": "assistant", "runId": "new", "text": "New"}]}
        self.assertEqual(observe(operation, projection)["lastAssistantMessage"], "New")
        projection["runs"][1]["status"] = "queued"
        self.assertEqual(observe(operation, projection)["status"], "starting")
        self.assertIsNone(observe(operation, projection)["lastAssistantMessage"])

    def test_terminal_turn_with_live_children_is_not_finished_work(self):
        operation = self.prepare()
        operation["state"] = "accepted"
        projection = {"thread": {"id": operation["thread_id"]}, "runs": [{"id": "r1", "userMessageId": operation["message_id"], "status": "completed"}],
                      "messages": [{"role": "assistant", "runId": "r1", "text": "Waiting for my child"}],
                      "subagents": [{"runId": "r1", "status": "running"}]}
        state = observe(operation, projection)
        self.assertEqual(state["status"], "background")
        self.assertFalse(state["settled"])

    def test_missing_current_run_is_unknown_not_completed(self):
        operation = self.prepare()
        operation["state"] = "accepted"
        self.assertEqual(observe(operation, {"thread": {"id": operation["thread_id"]}, "runs": [], "messages": []})["status"], "unknown")

    def test_runtime_request_is_linked_through_its_execution_node(self):
        operation = self.prepare()
        operation["state"] = "accepted"
        projection = {"thread": {"id": operation["thread_id"]},
            "runs": [{"id": "r1", "ordinal": 1, "userMessageId": operation["message_id"], "status": "running"}],
            "nodes": [{"id": "question-node", "runId": "r1"}],
            "runtimeRequests": [{"id": "q1", "nodeId": "question-node", "status": "pending", "kind": "user_input"}]}
        self.assertEqual(observe(operation, projection)["status"], "waiting_for_input")

    def test_automatic_wake_reply_after_the_original_run_is_not_lost(self):
        operation = self.prepare()
        operation["state"] = "accepted"
        projection = {"thread": {"id": operation["thread_id"]},
            "runs": [{"id": "r1", "ordinal": 1, "userMessageId": operation["message_id"], "status": "completed"},
                     {"id": "wake", "ordinal": 2, "userMessageId": "notification", "status": "completed"}],
            "messages": [{"role": "assistant", "runId": "r1", "text": "I delegated"},
                         {"role": "assistant", "runId": "wake", "text": "Final result"}]}
        self.assertEqual(observe(operation, projection)["lastAssistantMessage"], "Final result")

    def test_common_dir_validates_worktree_and_rejects_foreign_checkout(self):
        first = repo(self.root / "first")
        other = repo(self.root / "other")
        tree = self.root / "tree"
        git(first, "worktree", "add", "--detach", str(tree))
        projects = [{"id": "p1", "workspaceRoot": str(first)}]
        self.assertEqual(resolve_project(projects, str(tree))["id"], "p1")
        with self.assertRaises(ValueError):
            resolve_project(projects, str(other), "p1")

    def test_ambiguous_or_missing_project_requires_explicit_selection(self):
        first = repo(self.root / "first")
        projects = [{"id": "p1", "workspaceRoot": str(first)}, {"id": "p2", "workspaceRoot": str(first)}]
        with self.assertRaisesRegex(ValueError, "p1.*p2"):
            resolve_project(projects, str(first))
        self.assertEqual(resolve_project(projects, str(first), "p2")["id"], "p2")
        with self.assertRaises(ValueError):
            resolve_project([], str(first))

    def test_waiting_is_live_drain_with_accessible_partial_text(self):
        operation = self.prepare()
        operation["state"] = "accepted"
        projection = {"thread": {"id": operation["thread_id"]}, "runs": [{"id": "r1", "userMessageId": operation["message_id"], "status": "waiting"}],
                      "messages": [{"role": "assistant", "runId": "r1", "text": "Provider response"}]}
        data = observe(operation, projection)
        self.assertEqual(data["status"], "running")
        self.assertFalse(data["settled"])
        self.assertEqual(data["latestAssistantText"], "Provider response")
        self.assertIsNone(data["lastAssistantMessage"])

    def test_list_window_keeps_recent_requests_not_the_oldest_hundred(self):
        operations = [self.prepare(key="review-" + str(number)) for number in range(101)]
        visible = [row["thread_id"] for row in Journal(self.root, OWNER).threads()]
        self.assertEqual(len(visible), 100)
        self.assertIn(operations[-1]["thread_id"], visible)
        self.assertNotIn(operations[0]["thread_id"], visible)

    def progress(self, run, messages):
        operation = self.prepare()
        operation["state"] = "accepted"
        run = {"id": "r1", "ordinal": 1, "userMessageId": operation["message_id"], **run}
        projection = {"thread": {"id": operation["thread_id"]}, "runs": [run],
                      "messages": [{"role": "assistant", "runId": "r1", **message} for message in messages]}
        return observe(operation, projection)

    def test_running_turn_shows_its_start_and_the_last_sentence_of_the_newest_message(self):
        narration = [
            {"createdAt": "2026-10-07T17:25:14.000Z", "text": "Reading the journal module first."},
            {"createdAt": "2026-10-07T17:25:40.000Z", "text": "The journal keeps one file per owner. Next I'll list the five newest entries.\n\nRunning `ls -t` now."},
            {"createdAt": "2026-10-07T17:25:20.000Z", "text": "Found journal.py and its tests."},
        ]
        data = self.progress({"status": "running", "requestedAt": "2026-10-07T17:25:11.756Z", "startedAt": "2026-10-07T17:25:12.085Z",
                              "workStartedAt": "2026-10-07T17:25:12.500Z", "completedAt": None}, narration)
        self.assertEqual((data["startedAt"], data["excerpt"]), ("2026-10-07T17:25:12.500Z", "Running `ls -t` now."))
        self.assertNotIn("completedAt", data)

    def test_acp_message_that_grows_all_turn_yields_its_last_glued_sentence(self):
        data = self.progress({"status": "running", "requestedAt": "2026-10-07T17:27:27.565Z", "startedAt": None},
                             [{"createdAt": "2026-10-07T17:27:30.000Z", "text": "I read the ADR and the journal.Now I check the ADR contract.This time I do run the tests"}])
        self.assertEqual((data["startedAt"], data["excerpt"]), ("2026-10-07T17:27:27.565Z", "This time I do run the tests"))
        long = ("Short opener. Now I am walking through every caller of observe in engine.py, server.py and the two cards, checking that each one "
                "tolerates the new optional fields when an older Nightly server leaves them out entirely, which is the case today")
        self.assertEqual(self.progress({"status": "waiting"}, [{"text": long}])["excerpt"],
                         "…caller of observe in engine.py, server.py and the two cards, checking that each one tolerates the new optional fields "
                         "when an older Nightly server leaves them out entirely, which is the case today")

    def test_finished_turn_shows_its_duration_bounds_and_the_head_of_the_final_reply(self):
        final = ("The delegation_status contract holds. Every row carries **threadId**, status and round, and the card reads only those plus driver and model.\n\n"
                 "Two gaps remain: the digest drops the run's start time, and a waiting row never says what T3 is asking. I'd fix both in `domain.observe()`.")
        data = self.progress({"status": "completed", "requestedAt": "2026-10-07T17:27:27.565Z", "startedAt": "2026-10-07T17:27:27.593Z",
                              "workStartedAt": None, "completedAt": "2026-10-07T17:28:14.308Z"},
                             [{"createdAt": "2026-10-07T17:27:40.000Z", "text": "Checking the contract."},
                              {"createdAt": "2026-10-07T17:28:10.000Z", "text": final}])
        self.assertEqual((data["startedAt"], data["completedAt"]), ("2026-10-07T17:27:27.593Z", "2026-10-07T17:28:14.308Z"))
        self.assertEqual(data["excerpt"], "The delegation_status contract holds. Every row carries **threadId**, status and round, and the card reads only "
                                          "those plus driver and model. Two gaps remain: the digest drops the run's start time…")
        stopped = self.progress({"status": "interrupted", "startedAt": "2026-10-07T17:25:12.085Z", "completedAt": "2026-10-07T17:27:27.507Z"},
                                [{"text": "Renamed the module. Stopped before the tests ran."}])
        self.assertEqual(stopped["excerpt"], "Renamed the module. Stopped before the tests ran.")

    def test_a_server_without_timestamps_or_replies_leaves_the_progress_fields_out(self):
        data = self.progress({"status": "running"}, [])
        self.assertEqual(data["status"], "running")
        self.assertEqual({key for key in ("startedAt", "completedAt", "excerpt") if key in data}, set())
        blank = self.progress({"status": "running", "startedAt": "2026-10-07T17:25:12.085Z"}, [{"text": "  \n "}])
        self.assertEqual((blank["startedAt"], "excerpt" in blank), ("2026-10-07T17:25:12.085Z", False))

    def asking(self, request_kind, items):
        operation = self.prepare()
        operation["state"] = "accepted"
        projection = {"thread": {"id": operation["thread_id"]},
                      "runs": [{"id": "r1", "ordinal": 1, "userMessageId": operation["message_id"], "status": "running"}],
                      "nodes": [{"id": "request-node", "runId": "r1"}],
                      "runtimeRequests": [{"id": "rq1", "nodeId": "request-node", "kind": request_kind, "status": "pending"}],
                      "turnItems": [{"runId": "r1", **item} for item in items]}
        return observe(operation, projection)

    def test_codex_command_approval_asks_with_the_linked_command(self):
        codex = {"driver": "codex", "nativeId": "call_yANKGctvA0iNwyZjGIFHM76q", "strength": "strong"}
        data = self.asking("command", [
            {"type": "command_execution", "nativeItemRef": {**codex, "nativeId": "call_earlier"}, "input": "/bin/zsh -lc ls", "status": "completed"},
            {"type": "command_execution", "nativeItemRef": codex, "input": "/bin/zsh -lc pwd", "status": "inProgress", "title": None},
            {"type": "approval_request", "nativeItemRef": codex, "requestId": "rq1", "requestKind": "command", "prompt": "/bin/zsh -lc pwd", "title": None, "status": "waiting"},
        ])
        self.assertEqual((data["status"], data["ask"]), ("waiting_for_approval", {"kind": "command", "text": "/bin/zsh -lc pwd"}))

    def test_claude_command_approval_adds_its_description_after_the_command(self):
        claude = {"driver": "claudeAgent", "nativeId": "toolu_01Fjtywu8o2YHe6NtQ64FvfZ", "strength": "strong"}
        data = self.asking("command", [
            {"type": "command_execution", "nativeItemRef": claude, "input": "touch /tmp/t3card/probe/claude-approval-probe.txt", "title": None},
            {"type": "approval_request", "nativeItemRef": claude, "requestId": "rq1", "requestKind": "command", "prompt": "Create probe file", "title": None},
        ])
        self.assertEqual(data["ask"], {"kind": "command", "text": "touch /tmp/t3card/probe/claude-approval-probe.txt · Create probe file"})

    def test_claude_question_asks_with_the_first_question_and_counts_the_rest(self):
        question = {"id": "Which parts should switch to English?", "header": "Scope", "question": "Which parts should switch to English?", "multiSelect": False,
                    "options": [{"label": "Card UI only (Recommended)", "description": "Translate the card's own labels."},
                                {"label": "Just reply in English", "description": "Keep the card in Spanish."}]}
        item = {"type": "user_input_request", "requestId": "rq1", "nativeItemRef": {"driver": "claudeAgent", "nativeId": "toolu_01Q272m3jCduqUnyVmpvt7g7"},
                "questions": [question]}
        data = self.asking("user_input", [item])
        self.assertEqual((data["status"], data["ask"]), ("waiting_for_input", {"kind": "user_input", "text": "Which parts should switch to English?"}))
        second = {**question, "id": "tone", "question": "Formal or casual?"}
        self.assertEqual(self.asking("user_input", [{**item, "questions": [question, second]}])["ask"]["text"],
                         "Which parts should switch to English? (+1 more)")

    def test_an_ask_without_linked_text_names_only_its_kind(self):
        self.assertEqual(self.asking("file-change", [])["ask"], {"kind": "file-change"})
        titled = self.asking("permission", [{"type": "approval_request", "requestId": "rq1", "requestKind": "permission", "prompt": None, "title": "Allow network access"}])
        self.assertEqual(titled["ask"], {"kind": "permission", "text": "Allow network access"})
        bare = self.asking("permission", [{"type": "approval_request", "requestId": "rq1", "requestKind": "mcp-elicitation", "prompt": "", "title": None}])
        self.assertEqual(bare["ask"], {"kind": "mcp-elicitation"})
        self.assertNotIn("ask", self.progress({"status": "running"}, []))
