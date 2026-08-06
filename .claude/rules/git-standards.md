# Git Standards

## Commit Messages

This project uses **Conventional Commits**.

### Format

```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

### Types

| Type       | When                                                          |
| ---------- | ------------------------------------------------------------- |
| `feat`     | New feature or user-facing functionality                      |
| `fix`      | Bug fix                                                       |
| `docs`     | Documentation only                                            |
| `chore`    | Tooling, config, dependencies, no production code change      |
| `refactor` | Code restructuring without behavior change                    |
| `style`    | Formatting, whitespace, no logic change                       |
| `test`     | Adding or fixing tests                                        |
| `perf`     | Performance improvement                                       |
| `ci`       | CI/CD pipeline changes                                        |

### Scopes

Use the area of the codebase affected:

| Scope | Area |
|-------|------|
| `bot` | auto-tracker runtime, Slack event handlers (`slackbot.py`, `slack_client.py`) |
| `mcp` | MCP server and tools (`mcpserver.py`) |
| `sheet` | Google Sheet store and row mapping (`sheet.py`) |
| `classify` | LLM classifier and resolution judge (`classify.py`) |
| `reports` | pure report builders (`reports.py`) |
| `config` | env, taxonomy constants, ids/logutil (`config.py`, `ids.py`, `logutil.py`) |
| `deploy` | Dockerfiles, docker-compose, deploy assets (`deploy/`) |
| `docs` | documentation (`docs/`) |
| `tests` | test suite (`tests/`) |
| `ci` | CI/CD pipeline (`.github/`) |

For cross-cutting changes: omit the scope.

### Breaking Changes

Append `!` after the type/scope or add a `BREAKING CHANGE:` footer:

```
feat(sheet)!: rename Product column header

BREAKING CHANGE: existing sheets must be re-migrated to the new header
```

### Rules

- MUST use lowercase for type and scope.
- MUST use imperative mood in description: "add feature" not "added feature" or "adds feature".
- MUST keep the first line under 72 characters.
- MUST NOT end the description with a period.
- MUST NOT use em dashes anywhere in the message. Use a period, comma, or parentheses instead.
- SHOULD include a body for non-trivial changes explaining **why**.
- MUST reference issue numbers in the footer when applicable: `Closes #42`.
- MUST NOT include `Co-Authored-By` trailers.
- **Commit early and often on branches.** Make small, atomic commits after each logical unit of work. The full test gate is required only before `/ship`, not before every commit.

### Examples

```
feat(mcp): add weekly_report tool with product filter
fix(sheet): stop clobbering the legacy Escalated To column
refactor(reports): extract aging buckets into a pure helper
chore(config): add ANTHROPIC_MODEL default of claude-haiku-4-5
docs(mcp): document post_message override flow
test(classify): cover taxonomy fallback to Other
ci: add ruff and pytest workflow
feat(classify)!: change resolution judge output schema
```

---

## Branch Strategy

### Branch Naming

```
<type>/<short-description>
```

- `feature/aging-report-filter`
- `fix/sheet-column-drift`
- `chore/update-deps`
- `refactor/extract-report-builders`

### Rules

- Any work that introduces a new feature, touches 3+ files, spans multiple tasks, or takes more than one commit MUST go on a dedicated branch.
- Hotfixes and urgent single-file fixes may go directly on `main`.
- If a branch is used, keep it short-lived and delete it after merge.
- MUST NOT force-push to shared branches.

---

## Pull Requests

- PR title MUST follow the same Conventional Commit format as the merge commit.
- PR description MUST include: what changed, why, and how to test.
- MUST NOT merge with failing CI checks.
- SHOULD squash-merge to keep `main` history clean.
- MUST NOT force-push to shared branches.

---

## What Not To Commit

- `.env` files (use `.env.example` for structure)
- The Google service-account json and anything under `secrets/`
- `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `*.pyc`
- API keys, Slack tokens, or any credentials
- OS files (`.DS_Store`)
- Editor-specific files (`.vscode/settings.json` with personal config)
- Large binary files, use Git LFS if necessary
- Temporary debug code and stray print statements
