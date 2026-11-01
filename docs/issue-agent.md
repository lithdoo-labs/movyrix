# Issue-driven OpenCode 2 Agent

This repository supports a one-Issue / one-Draft-PR / one-persistent-session workflow.

## Usage

1. Create an ordinary Issue **as lithdoo**, with requirements and acceptance criteria.
2. Comment **/agent start** on the Issue. Only comments made by GitHub user ID 130959182 are authorized.
3. GitHub-hosted preflight validates the live Issue and author, finds the canonical branch, and snapshots the Issue.
4. Organization Runner **opencode-lithdoo-labs** runs OpenCode 2 with persistent metadata. GitHub write tokens are not provided to the AI runner.
5. GitHub-hosted publisher validates the patch, pushes to **agent/issue-N**, creates a Draft PR, and comments results on the original Issue.
6. Review the Issue and PR. Add feedback to the Issue, a PR conversation, a PR review, or inline review comments.
7. Comment **/agent revise** on the *same Issue*. The next run collects feedback since the previous report, resumes the same OpenCode session, and pushes to the same PR branch.
8. Review the changes and manually merge. No automatic merge is implemented.

An owner-only alternative is Actions → Issue Agent (OpenCode 2) → Run workflow, with Issue number and **start** or **revise** inputs.

## GitHub and Runner prerequisites

- Organization Runner Group permits **lithdoo-labs/movyrix** and has the **labs** runner tag.
- Configure Runner Group **Selected workflows** to allow the trusted workflow at **lithdoo-labs/movyrix/.github/workflows/issue-agent.yml@refs/heads/main**, where supported. Public-repository self-hosted runners have significant exposure to untrusted code. Do not relax the preflight author restrictions.
- Repository Actions settings must permit the publisher to create Pull Requests, with Contents / Issues / Pull requests write rights. An optional secret named **AGENT_GITHUB_TOKEN** can supply a minimum-scope PAT if repository policy prevents the default GITHUB_TOKEN from creating PRs or triggering other workflows.
- The Runner must contain **opencode2** (OpenCode V2), a valid GLM config, and persistent read/write data under /runner/state/lithdoo-labs-org. It currently has **GLM_API_KEY** and writable /shared; OpenCode tool execution is not a strong security sandbox.
- A publisher commit made using the default GITHUB_TOKEN might not trigger new push/PR workflows. Use a properly scoped GitHub App/PAT if CI-triggering commits are required.

## Privilege separation

| Stage | Runner | GitHub token |
| --- | --- | --- |
| Preflight | ubuntu-latest | Read-only |
| Execute | self-hosted, linux, x64, opencode, labs | Read-only checkout, no write token forwarded |
| Publish | ubuntu-latest | Contents / Issues / Pull requests write |

The publisher rejects modifications to .github/, scripts/issue_agent/, AGENTS.md, .env*, and OpenCode config, as well as symlinks, submodules, oversize patches, and stale PR HEAD SHAs. An owner-authored Issue is the only permitted entry for OpenCode execution.

## Persistent mapping

- Git branch: **agent/issue-N**
- PR: one Draft PR per Issue, with explicit marker in its body
- Session metadata: **/runner/state/lithdoo-labs-org/agent-projects/movyrix/issues/issue-N/metadata.json**
- Execution worktree: **$GITHUB_WORKSPACE** in the organization Runner job (the trusted checkout is refreshed each turn). OpenCode runs directly in this worktree; its **session data**, not the checkout, is persisted separately.
- OpenCode data: **/runner/state/lithdoo-labs-org/agent-projects/movyrix/opencode-data**
- Published Issue comments contain a marker identifying the processed turn and triggering comment. Only markers from the Actions bot or authorized owner are trusted; other public commenters cannot forge checkpoints.

A revision refuses to start if the original session has been lost; it does not pretend a new model session has the same context. After a failed first publication with no PR, a fresh /agent start resumes the saved initial session rather than discarding it. PRs that are already closed or merged cannot be revised. Use a new Issue for a new task.

GitHub Actions concurrency avoids overlapping running jobs for the same Issue, but GitHub can replace pending jobs. The next accepted revision includes all owner-authored feedback since the most recent published checkpoint. A changed PR HEAD makes the current publication fail rather than overwriting human commits.

## Testing

### Offline CI tests

Open a PR with these changes. The **Issue Agent Validation** workflow exercises:
- Owner and Issue authorization
- Branch / Issue mapping and checkpoint handling
- Protected-path rejection
- Two consecutive OpenCode turns through a fake OpenCode CLI using a real disposable Git repository
- Resume of the exact same session and rejection of missing session state

These tests do **not** use the real GLM API or consume the organization's Runner.

### Real end-to-end verification

After reviewing and merging the implementation to main, the owner should:

1. Create an Issue as lithdoo, for example: *Add docs/issue-agent-e2e.md with one short Hello World paragraph, without changing other files.*
2. Comment **/agent start**. Verify a run at Actions → Issue Agent (OpenCode 2) completes on **opencode-lithdoo-labs**, opens one Draft PR, and comments a summary back on the Issue.
3. In that Issue, comment: *Change the paragraph to say Hello Movyrix.* Then comment **/agent revise**.
4. Verify the second run updates the **same PR number** and branch, and reports **Session: resumed**. Confirm the real implementation diff changed.
5. Do not merge the test PR automatically. If desired, close and clean up the test Issue/PR only after storing required session data.

The connected ChatGPT GitHub API lacks an Actions workflow_dispatch operation; only the GitHub owner can cause a real authenticated Owner event in this configuration.

## Troubleshooting

- Preflight rejects unauthorized, closed, duplicate, or non-ordinary Issues, and revisions without fresh owner feedback.
- Execute errors are reported back to the Issue when the publisher has permission; check OpenCode availability and state-volume permissions without printing API keys.
- Publish failures: verify GitHub Action PR creation policy and write permissions, the approved source SHA and branch rules. If the Agent pushed branch agent/issue-N but GitHub denied Draft PR creation, enable the repository PR creation setting (or supply AGENT_GITHUB_TOKEN), then comment **/agent start** again. The workflow validates the prior Agent commit and opens the missing Draft PR **without rerunning OpenCode**. The saved Session remains available for the next /agent revise.
- The model's session content lives on the self-hosted Runner; do not delete persistent state when cleaning a PR.
