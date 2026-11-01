#!/usr/bin/env python3
"""Issue-driven, owner-gated OpenCode executor and GitHub publisher.

Preflight/publish run on GitHub-hosted runners. Execute runs without GitHub
write credentials on the persistent lithdoo-labs organization runner.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

REPO = "lithdoo-labs/movyrix"
OWNER_LOGIN = "lithdoo"
OWNER_ID = 130959182
MARKER = "<!-- movyrix-issue-agent:issue={issue} -->"
STATUS_RE = re.compile(r"<!-- movyrix-agent-turn:issue=(\d+);comment=(\d+);turn=(\d+) -->")
SESSION_RE = re.compile(r"^ses_[A-Za-z0-9_-]+$")
PROTECTED = (".github/", "scripts/issue_agent/", ".git/", ".env",
             "AGENTS.md", "opencode.json", "opencode.jsonc")
MAX_FILES = 40
MAX_PATCH_BYTES = 900_000


def git(*args, cwd=None, env=None, check=True):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, env=env,
                          check=check, capture_output=True, text=True)


def api(method, route, payload=None):
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise RuntimeError("GH_TOKEN missing from GitHub-hosted job")
    data = None if payload is None else json.dumps(payload).encode()
    req = Request("https://api.github.com" + route, data=data, method=method,
                  headers={"Authorization": "Bearer " + token,
                           "Accept": "application/vnd.github+json",
                           "X-GitHub-Api-Version": "2022-11-28",
                           "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as ex:
        if ex.code == 404:
            raise LookupError(route) from ex
        text = ex.read(1024).decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {route}: HTTP {ex.code}: {text}") from ex


def pages(route, limit=5):
    result = []
    for page in range(1, limit + 1):
        joiner = "&" if "?" in route else "?"
        batch = api("GET", f"{route}{joiner}per_page=100&page={page}")
        if not isinstance(batch, list):
            raise RuntimeError("unexpected GitHub API pagination response")
        result.extend(batch)
        if len(batch) < 100:
            break
    return result


def authorized_author(item):
    return (item.get("user") or {}).get("id") == OWNER_ID


def is_protected(path):
    name = path.replace("\\", "/")
    return (name.startswith("/") or "../" in name or name.startswith("../")
            or any(name == p or name.startswith(p) for p in PROTECTED)
            or "/.github/" in name or name.endswith("/AGENTS.md")
            or name.startswith(".."))


def validate_paths(paths):
    if len(paths) > MAX_FILES:
        raise RuntimeError(f"too many changed files ({len(paths)} > {MAX_FILES})")
    for path in paths:
        if not path or is_protected(path):
            raise RuntimeError(f"protected or invalid path: {path!r}")


def event_request(event, name, inputs, actor):
    if name == "issue_comment":
        comment = event.get("comment") or {}
        issue = event.get("issue") or {}
        if issue.get("pull_request") or issue.get("user", {}).get("id") != OWNER_ID:
            raise RuntimeError("not an owner-authored ordinary Issue")
        if not authorized_author(comment):
            raise RuntimeError("not a comment from the authorized owner")
        command = str(comment.get("body", "")).strip()
        if command not in ("/agent start", "/agent revise"):
            raise RuntimeError("not an Issue Agent command")
        return int(issue["number"]), command.split()[-1], int(comment["id"])
    if name == "workflow_dispatch" and actor == OWNER_LOGIN:
        number = int(inputs.get("issue_number", 0))
        operation = inputs.get("operation")
        if operation not in ("start", "revise"):
            raise RuntimeError("invalid dispatch operation")
        return number, operation, 0
    raise RuntimeError("unauthorized event")


def branch_name(issue):
    return f"agent/issue-{issue}"


def find_agent_pr(issue):
    path = f"/repos/{REPO}/pulls?{urlencode({'state':'all','head':'lithdoo-labs:'+branch_name(issue),'base':'main','per_page':100})}"
    matches = api("GET", path)
    valid = [p for p in matches if p.get("head", {}).get("ref") == branch_name(issue)]
    if len(valid) > 1:
        raise RuntimeError("multiple PRs exist for the same Issue branch")
    return valid[0] if valid else None


def get_last_checkpoint(comments, issue, before_id):
    result = None
    for item in comments:
        if before_id and item["id"] >= before_id:
            continue
        match = STATUS_RE.search(item.get("body") or "")
        if match and int(match[1]) == issue:
            if result is None or item["id"] > result["id"]:
                result = {"id": item["id"], "turn": int(match[3]),
                          "created_at": item["created_at"]}
    return result


def collect_feedback(issue, operation, command_id, pr, comments, checkpoint):
    if operation != "revise":
        return []
    watermark = checkpoint["id"] if checkpoint else 0
    output = []
    for item in comments:
        if not authorized_author(item) or item["id"] <= watermark:
            continue
        if command_id and item["id"] > command_id:
            continue
        body = (item.get("body") or "").strip()
        if body in ("/agent start", "/agent revise") or not body:
            continue
        output.append((item["created_at"], "Issue comment", body))
    if pr:
        prn = pr["number"]
        for route, label, time_key in [
            (f"/repos/{REPO}/issues/{prn}/comments", "PR conversation", "created_at"),
            (f"/repos/{REPO}/pulls/{prn}/reviews", "PR review", "submitted_at"),
            (f"/repos/{REPO}/pulls/{prn}/comments", "PR inline review", "created_at")
        ]:
            for item in pages(route):
                if not authorized_author(item):
                    continue
                stamp = item.get(time_key) or item.get("created_at") or ""
                if checkpoint and stamp <= checkpoint["created_at"]:
                    continue
                body = (item.get("body") or "").strip()
                if not body:
                    continue
                where = item.get("path")
                output.append((stamp, label + (f" ({where})" if where else ""), body))
    output.sort()
    return [{"source": src, "text": text[:10000]} for _, src, text in output][-60:]


def preflight(args):
    Path(args.github_output).write_text("allowed=false\n")
    event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    issue_number, operation, comment_id = event_request(
        event, os.environ.get("GITHUB_EVENT_NAME", ""),
        {"issue_number": os.environ.get("INPUT_ISSUE_NUMBER", ""),
         "operation": os.environ.get("INPUT_OPERATION", "")},
        os.environ.get("ACTOR", ""))
    if issue_number < 1:
        raise RuntimeError("invalid Issue number")
    issue = api("GET", f"/repos/{REPO}/issues/{issue_number}")
    if issue.get("pull_request") or issue.get("state") != "open" or not authorized_author(issue):
        raise RuntimeError("Issue must be open and authored by lithdoo")
    if comment_id:
        comment = api("GET", f"/repos/{REPO}/issues/comments/{comment_id}")
        if not authorized_author(comment) or int(comment.get("issue_url", "").split("/")[-1]) != issue_number:
            raise RuntimeError("trigger comment was altered or mismatched")
    pr = find_agent_pr(issue_number)
    if operation == "start" and pr:
        raise RuntimeError("Issue already has an Agent PR; use /agent revise instead")
    if operation == "revise" and (not pr or pr.get("state") != "open"):
        raise RuntimeError("Issue has no open Agent PR; use /agent start")
    if pr:
        if (pr.get("head", {}).get("repo", {}).get("full_name") != REPO
                or pr.get("base", {}).get("ref") != "main"
                or MARKER.format(issue=issue_number) not in (pr.get("body") or "")):
            raise RuntimeError("Agent PR provenance mismatch")
    if operation == "start":
        try:
            api("GET", f"/repos/{REPO}/git/ref/heads/{branch_name(issue_number)}")
        except LookupError:
            pass
        else:
            raise RuntimeError("Agent branch already exists without a bound PR")
    comments = pages(f"/repos/{REPO}/issues/{issue_number}/comments")
    checkpoint = get_last_checkpoint(comments, issue_number, comment_id)
    feedback = collect_feedback(issue_number, operation, comment_id, pr, comments, checkpoint)
    if operation == "revise" and not feedback:
        raise RuntimeError("No new owner feedback found since the last Agent result")
    ref = (pr["head"]["sha"] if pr else
           api("GET", f"/repos/{REPO}/git/ref/heads/main")["object"]["sha"])
    payload = {"repository": REPO, "issue": issue_number, "operation": operation,
               "command_id": comment_id, "issue_title": issue["title"][:300],
               "issue_body": (issue.get("body") or "")[:30000],
               "branch": branch_name(issue_number), "pr": pr["number"] if pr else None,
               "base_sha": ref, "turn": (checkpoint["turn"] + 1 if checkpoint else 1),
               "feedback": feedback}
    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    with Path(args.github_output).open("a") as out:
        out.write(f"allowed=true\nbase_sha={ref}\nissue_number={issue_number}\n")
    print(f"Authorized {operation} for Issue #{issue_number}; expected source SHA {ref[:12]}")


def session_id_from_events(events):
    for line in events.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        stack = [obj]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                for k, v in item.items():
                    if k in ("sessionID", "sessionId", "session_id") and isinstance(v, str) and SESSION_RE.fullmatch(v):
                        return v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(item, list):
                stack.extend(item)
    return None


def response_from_events(events):
    messages = []
    for line in events.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        part = item.get("part") if isinstance(item, dict) else None
        if item.get("type") in ("text", "message.part.updated") and isinstance(part, dict):
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                messages.append(part["text"])
        elif item.get("type") == "text" and isinstance(item.get("text"), str):
            messages.append(item["text"])
    return "\n".join(messages)[-5000:]


def safe_summary(summary):
    for key in ("GLM_API_KEY", "GITHUB_ACCESS_TOKEN", "GITHUB_ORG_ACCESS_TOKEN", "GH_TOKEN"):
        secret = os.environ.get(key, "")
        if secret:
            summary = summary.replace(secret, "[REDACTED]")
    return summary[:5000]


def execute(args):
    inp = json.loads(Path(args.input).read_text())
    result_path = Path(args.result)
    patch_path = Path(args.patch)
    patch_path.write_bytes(b"")
    state = Path(os.environ["ISSUE_AGENT_STATE"]) / "issues" / f"issue-{inp['issue']}"
    workspace = state / "workspace"
    metadata = state / "metadata.json"
    result = {"status": "failed", "issue": inp["issue"], "turn": inp["turn"],
              "session_id": None, "continuity": "unavailable", "summary": ""}
    try:
        state.mkdir(parents=True, exist_ok=True)
        prior = json.loads(metadata.read_text()) if metadata.exists() else None
        if inp["operation"] == "start" and prior:
            raise RuntimeError("previous session state exists; refusing accidental reset")
        if inp["operation"] == "revise":
            if not prior or prior.get("issue") != inp["issue"]:
                raise RuntimeError("persistent Issue session metadata missing")
            candidate = prior.get("session_id", "")
            if not SESSION_RE.fullmatch(candidate):
                raise RuntimeError("persistent session ID invalid")
            verify = subprocess.run([os.getenv("OPENCODE_BIN", "opencode2"),
                                     "session", "export", "--standalone", candidate],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=40, check=False)
            if verify.returncode:
                raise RuntimeError("existing OpenCode session missing; not silently starting a new one")
            result.update(session_id=candidate, continuity="resumed")
        if workspace.exists():
            shutil.rmtree(workspace)
        shutil.copytree(args.source, workspace, symlinks=False,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        git("reset", "--hard", "HEAD", cwd=workspace)
        git("clean", "-fd", cwd=workspace)
        prompt = (
            f"Repository: {REPO}\nIssue #{inp['issue']}: {inp['issue_title']}\n"
            f"Original specification:\n{inp['issue_body']}\n\n"
            f"Turn {inp['turn']}: {inp['operation']}\n"
            f"Owner feedback / PR review (ordered):\n{json.dumps(inp['feedback'], ensure_ascii=False)}\n\n"
            "Implement the requested changes in the checked-out workspace. "
            "On revisions, preserve existing implementation except for requested fixes. "
            "Add/execute appropriate tests when possible. "
            "Never change .github/, scripts/issue_agent/, AGENTS.md, .env or OpenCode config. "
            "Do not run git push, git commit, gh or attempt to publish a PR. "
            "Report what changed, what tests ran and remaining risks."
        )
        env = os.environ.copy()
        for key in ("GH_TOKEN", "GITHUB_TOKEN", "AGENT_GITHUB_TOKEN",
                    "GITHUB_ACCESS_TOKEN", "GITHUB_ORG_ACCESS_TOKEN"):
            env.pop(key, None)
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"
        binary = os.getenv("OPENCODE_BIN", "opencode2")
        command = [binary, "run", "--standalone", "--auto", "--format", "json"]
        if inp["operation"] == "revise":
            command.extend(["--session", result["session_id"]])
        else:
            command.extend(["--title", f"movyrix issue #{inp['issue']}"])
        completed = subprocess.run(command + [prompt], cwd=workspace, env=env,
                                   capture_output=True, text=True, timeout=4800, check=False)
        if completed.returncode != 0:
            raise RuntimeError("OpenCode process failed with exit code " + str(completed.returncode))
        session_id = result["session_id"] or session_id_from_events(completed.stdout)
        if not session_id:
            raise RuntimeError("OpenCode completed without a recoverable session ID")
        summary = safe_summary(response_from_events(completed.stdout))
        git("add", "-A", cwd=workspace)
        paths = git("diff", "--cached", "--name-only", "-z", cwd=workspace).stdout.split("\0")
        paths = [p for p in paths if p]
        validate_paths(paths)
        git("diff", "--cached", "--check", cwd=workspace)
        patch = subprocess.run(["git", "diff", "--cached", "--binary", "HEAD"], cwd=workspace,
                               capture_output=True, check=True).stdout
        if len(patch) > MAX_PATCH_BYTES:
            raise RuntimeError("generated patch exceeds maximum allowed size")
        patch_path.write_bytes(patch)
        result.update(status="ok", session_id=session_id,
                      continuity="resumed" if inp["operation"] == "revise" else "new",
                      changed_files=paths, summary=summary or "OpenCode completed successfully.")
        metadata.write_text(json.dumps({"issue": inp["issue"], "session_id": session_id,
                                        "turn": inp["turn"]}, indent=2) + "\n")
    except Exception as ex:
        result["summary"] = "Executor validation failed: " + str(ex)[:500]
        patch_path.write_bytes(b"")
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Issue #{inp['issue']} turn {inp['turn']}: {result['status']} "
          f"({len(result.get('changed_files', []))} files)")


def git_push(branch):
    token = os.environ["GH_TOKEN"]
    # Never persist the token in .git/config. GitHub Actions logs mask secrets.
    cred = __import__("base64").b64encode(f"x-access-token:{token}".encode()).decode()
    git("-c", "credential.helper=", "-c",
        "http.https://github.com/.extraheader=AUTHORIZATION: basic " + cred,
        "push", "origin", "HEAD:refs/heads/" + branch)


def result_comment(inp, result, pr=None, error=None):
    issue = inp["issue"]
    command = inp["command_id"]
    turn = inp["turn"]
    success = not error and result.get("status") == "ok"
    changed = result.get("changed_files", [])
    body = [
        f"<!-- movyrix-agent-turn:issue={issue};comment={command};turn={turn} -->",
        f"### OpenCode Issue Agent — turn {turn}",
        f"- Status: **{'completed' if success else 'failed / needs attention'}**",
        f"- Session: **{result.get('continuity', 'unavailable')}**",
        f"- PR: {'#' + str(pr) if pr else 'not published'}",
        f"- Changed files: {len(changed)}",
        f"- GitHub Actions run: {os.environ.get('GITHUB_RUN_ID', 'unknown')}",
        "",
        "#### Agent summary",
        safe_summary(result.get("summary", "") or "No agent summary available.")
    ]
    if error:
        body.extend(["", "#### Publishing error", str(error)[:1000]])
    body.append("\nHuman review and merge are required; this workflow never auto-merges.")
    return "\n".join(body)[:12000]


def publish(args):
    inp = json.loads(Path(args.input).read_text())
    candidate_path = Path(args.result)
    result = json.loads(candidate_path.read_text()) if candidate_path.is_file() else {
        "status": "failed", "continuity": "unavailable",
        "summary": "Executor job did not produce a result artifact."
    }
    if result.get("issue") not in (None, inp["issue"]):
        raise RuntimeError("result artifact belongs to another Issue")
    if result.get("turn") not in (None, inp["turn"]):
        raise RuntimeError("result artifact turn mismatch")
    # Idempotence guard for rerun of the publish job.
    for comment in pages(f"/repos/{REPO}/issues/{inp['issue']}/comments"):
        m = STATUS_RE.search(comment.get("body") or "")
        if m and (int(m[1]), int(m[2]), int(m[3])) == (
                inp["issue"], inp["command_id"], inp["turn"]):
            print("This command already has a published result; skipping duplicate push.")
            return
    number = inp.get("pr")
    failure = None
    try:
        if result.get("status") != "ok":
            raise RuntimeError("Agent execution failed")
        if git("rev-parse", "HEAD").stdout.strip() != inp["base_sha"]:
            raise RuntimeError("publisher checkout SHA differs from approved snapshot")
        if inp["operation"] == "revise":
            current = api("GET", f"/repos/{REPO}/pulls/{number}")
            if (current["state"] != "open" or current["head"]["sha"] != inp["base_sha"]
                    or current["head"]["ref"] != inp["branch"]):
                raise RuntimeError("PR changed since preflight; refusing stale push")
        patch = Path(args.patch)
        if not patch.is_file() or patch.stat().st_size > MAX_PATCH_BYTES:
            raise RuntimeError("patch artifact missing or too large")
        if patch.stat().st_size:
            git("apply", "--check", "--index", patch)
            git("apply", "--index", patch)
            paths = [x for x in git("diff", "--cached", "--name-only", "-z").stdout.split("\0") if x]
            validate_paths(paths)
            git("diff", "--cached", "--check")
            modes = git("ls-files", "--stage", "-z").stdout
            for row in modes.split("\0"):
                if row and row.split(" ", 1)[0] in ("120000", "160000"):
                    raise RuntimeError("symlinks or submodules are not publishable")
            git("config", "user.name", "github-actions[bot]")
            git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
            git("commit", "-m", f"feat(agent): issue #{inp['issue']} turn {inp['turn']}")
            git_push(inp["branch"])
        elif inp["operation"] == "start":
            raise RuntimeError("initial implementation produced no code changes")
        if inp["operation"] == "start":
            title = f"agent: {inp['issue_title']}"[:200]
            body = (
                MARKER.format(issue=inp["issue"]) + "\n\n"
                f"Implements #{inp['issue']} using OpenCode 2 on the organization runner.\n\n"
                "Draft PR for human code review; changes and later revisions remain on "
                f"branch \x60{inp['branch']}\x60.\n\n"
                "Post feedback on the linked Issue or in PR review, then comment "
                "\x60/agent revise\x60 on the Issue. **Never auto-merge.**"
            )
            pr = api("POST", f"/repos/{REPO}/pulls",
                     {"title": title, "head": inp["branch"], "base": "main",
                      "body": body, "draft": True})
            number = pr["number"]
        print(f"Published Issue #{inp['issue']} to PR #{number}")
    except Exception as ex:
        failure = str(ex)
        print(f"Publish failure for Issue #{inp['issue']}: {failure}", file=sys.stderr)
    api("POST", f"/repos/{REPO}/issues/{inp['issue']}/comments",
        {"body": result_comment(inp, result, number if not failure else None, failure)})
    if failure:
        raise RuntimeError(failure)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    pre = commands.add_parser("preflight")
    pre.add_argument("--event", required=True)
    pre.add_argument("--output", required=True)
    pre.add_argument("--github-output", required=True)
    exe = commands.add_parser("execute")
    exe.add_argument("--input", required=True)
    exe.add_argument("--result", required=True)
    exe.add_argument("--patch", required=True)
    exe.add_argument("--source", required=True)
    pub = commands.add_parser("publish")
    pub.add_argument("--input", required=True)
    pub.add_argument("--result", required=True)
    pub.add_argument("--patch", required=True)
    args = parser.parse_args()
    {"preflight": preflight, "execute": execute, "publish": publish}[args.action](args)


if __name__ == "__main__":
    main()
