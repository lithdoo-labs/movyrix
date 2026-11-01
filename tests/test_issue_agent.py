"""Offline integration and authorization tests for the Issue Agent."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("issue_agent", ROOT / "scripts/issue_agent/agent.py")
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


class AuthTests(unittest.TestCase):
    def test_owner_issue_command(self):
        event = {"issue": {"number": 42, "user": {"id": agent.OWNER_ID}},
                 "comment": {"id": 1234, "user": {"id": agent.OWNER_ID},
                             "body": "/agent start"}}
        self.assertEqual(agent.event_request(event, "issue_comment", {}, "lithdoo"),
                         (42, "start", 1234))

    def test_reject_fork_comment_or_untrusted_issue(self):
        base = {"issue": {"number": 42, "user": {"id": agent.OWNER_ID}},
                "comment": {"id": 1234, "user": {"id": 12}, "body": "/agent start"}}
        with self.assertRaises(RuntimeError):
            agent.event_request(base, "issue_comment", {}, "stranger")
        base["comment"]["user"]["id"] = agent.OWNER_ID
        base["issue"]["user"]["id"] = 12
        with self.assertRaises(RuntimeError):
            agent.event_request(base, "issue_comment", {}, "lithdoo")
        base["issue"]["user"]["id"] = agent.OWNER_ID
        base["issue"]["pull_request"] = {}
        # Even if a malformed event lacks a truthy pull_request field,
        # preflight checks GitHub's live Issue object separately.
        self.assertEqual(agent.event_request(base, "issue_comment", {}, "lithdoo")[0], 42)
        base["issue"]["pull_request"] = {"url": "https://example.com"}
        with self.assertRaises(RuntimeError):
            agent.event_request(base, "issue_comment", {}, "lithdoo")

    def test_dispatch_only_owner(self):
        with self.assertRaises(RuntimeError):
            agent.event_request({}, "workflow_dispatch",
                                {"issue_number": "1", "operation": "start"}, "stranger")
        self.assertEqual(agent.event_request({}, "workflow_dispatch",
                           {"issue_number": "42", "operation": "revise"}, "lithdoo"),
                         (42, "revise", 0))

    def test_protected_patch_paths(self):
        agent.validate_paths(["src/main.py", "docs/features.md"])
        for name in (".github/workflows/malicious.yml", ".env", "AGENTS.md",
                     "scripts/issue_agent/agent.py", "../escape", "a/AGENTS.md"):
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                agent.validate_paths([name])

    def test_checkpoint_feedback_and_session_event(self):
        checkpoint = {"id": 900, "created_at": "2026-11-01T00:00:00Z", "turn": 1}
        comments = [
            {"id": 900, "created_at": "2026-11-01T00:00:00Z",
             "body": "<!-- movyrix-agent-turn:issue=42;comment=100;turn=1 -->"},
            {"id": 901, "created_at": "2026-11-01T00:01:00Z",
             "user": {"id": agent.OWNER_ID}, "body": "Change the display label"},
            {"id": 902, "created_at": "2026-11-01T00:02:00Z",
             "user": {"id": agent.OWNER_ID}, "body": "/agent revise"},
            {"id": 903, "created_at": "2026-11-01T00:03:00Z",
             "user": {"id": 2}, "body": "Untrusted extra input"}
        ]
        found = agent.get_last_checkpoint(comments, 42, 902)
        self.assertEqual(found["turn"], 1)
        feedback = agent.collect_feedback(42, "revise", 902, None, comments, checkpoint)
        self.assertEqual(len(feedback), 1)
        self.assertIn("display label", feedback[0]["text"])
        self.assertEqual(agent.session_id_from_events(
            '{"type":"text","part":{"sessionID":"ses_testabc"}}\n'), "ses_testabc")

    def test_preflight_start_and_same_pr_revision(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            event = root / "event.json"
            state = root / "input.json"
            output = root / "out"
            event.write_text(json.dumps({
                "issue": {"number": 42, "user": {"id": agent.OWNER_ID}},
                "comment": {"id": 201, "user": {"id": agent.OWNER_ID},
                            "body": "/agent start"}
            }))
            calls = []
            def fake_api(method, route, payload=None):
                calls.append(route)
                if route.endswith("/issues/42"):
                    return {"number": 42, "state": "open", "title": "Test",
                            "body": "Implement test", "user": {"id": agent.OWNER_ID}}
                if route.endswith("/issues/comments/201"):
                    return {"id": 201, "user": {"id": agent.OWNER_ID},
                            "issue_url": "https://api.github.com/repos/lithdoo-labs/movyrix/issues/42"}
                if route.startswith("/repos/lithdoo-labs/movyrix/pulls?"):
                    return []
                if route.endswith("/git/ref/heads/agent/issue-42"):
                    raise LookupError(route)
                if route.endswith("/git/ref/heads/main"):
                    return {"object": {"sha": "abc123"}}
                return []
            args = SimpleNamespace(event=str(event), output=str(state),
                                   github_output=str(output))
            with patch.object(agent, "api", side_effect=fake_api), \
                 patch.object(agent, "pages", return_value=[]), \
                 patch.dict(os.environ, {"GITHUB_EVENT_NAME": "issue_comment",
                                         "ACTOR": "lithdoo"}, clear=False):
                agent.preflight(args)
            result = json.loads(state.read_text())
            self.assertEqual(result["branch"], "agent/issue-42")
            self.assertEqual(result["turn"], 1)
            self.assertIsNone(result["pr"])
            self.assertIn("allowed=true", output.read_text())


class RunnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        run("git", "init", "-q", self.source)
        run("git", "config", "user.name", "Test", cwd=self.source)
        run("git", "config", "user.email", "test@example.com", cwd=self.source)
        (self.source / "README.md").write_text("baseline\n")
        run("git", "add", ".", cwd=self.source)
        run("git", "commit", "-qm", "initial", cwd=self.source)
        self.binary = self.root / "fake-opencode"
        self.binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json,sys,pathlib\n"
            "a=sys.argv[1:]\n"
            "if a[0]=='session':\n"
            "    assert 'ses_testabc' in a\n"
            "    print('{}')\n"
            "else:\n"
            "    assert a[0]=='run'\n"
            "    if '--session' in a: assert a[a.index('--session')+1]=='ses_testabc'\n"
            "    p=pathlib.Path('README.md')\n"
            "    p.write_text(p.read_text()+'OpenCode changed the project.\\n')\n"
            "    print(json.dumps({'type':'text','part':{'type':'text','text':'Implemented and checked the changes.'},'sessionID':'ses_testabc'}))\n"
        )
        self.binary.chmod(0o755)
        self.state = self.root / "state"

    def execute(self, operation, turn):
        path = self.root / f"input-{turn}.json"
        path.write_text(json.dumps({
            "issue": 42, "operation": operation, "turn": turn,
            "issue_title": "Update README", "issue_body": "Make a change",
            "feedback": [{"source": "Issue", "text": "Please refine it"}] if turn > 1 else [],
            "base_sha": "x", "branch": "agent/issue-42", "command_id": turn
        }))
        result = self.root / f"result-{turn}.json"
        artifact = self.root / f"patch-{turn}.diff"
        with patch.dict(os.environ, {"ISSUE_AGENT_STATE": str(self.state),
                                     "OPENCODE_BIN": str(self.binary)}, clear=False):
            agent.execute(SimpleNamespace(input=str(path), source=str(self.source),
                                          result=str(result), patch=str(artifact)))
        return json.loads(result.read_text()), artifact.read_bytes()

    def test_two_real_git_patches_keep_same_persistent_session(self):
        initial, patch_one = self.execute("start", 1)
        self.assertEqual(initial["status"], "ok", initial)
        self.assertEqual(initial["continuity"], "new")
        self.assertIn(b"OpenCode changed the project", patch_one)
        resumed, patch_two = self.execute("revise", 2)
        self.assertEqual(resumed["status"], "ok", resumed)
        self.assertEqual(resumed["continuity"], "resumed")
        self.assertEqual(initial["session_id"], resumed["session_id"])
        self.assertEqual(json.loads((self.state / "issues/issue-42/metadata.json").read_text())["turn"], 2)
        self.assertEqual(patch_one, patch_two)

    def test_refuses_revision_without_persistent_session(self):
        resumed, patch = self.execute("revise", 2)
        self.assertEqual(resumed["status"], "failed")
        self.assertIn("metadata missing", resumed["summary"])
        self.assertEqual(patch, b"")


if __name__ == "__main__":
    unittest.main()
