---
name: telegram-watch-dev
description: Develop telegram-watch using the current Todoist-backed project workflow, local-first safety rules, and minimal tested changes.
metadata:
  short-description: telegram-watch Todoist workflow development
---

When using this skill, follow the repository instructions in `AGENTS.md` / `CLAUDE.md`. The current workflow is Todoist-backed; historical `docs/requests/**` files are reference only and must not be used for new work.

## Source of truth

- Todoist shared `Dev` project, filtered by the single `telegram-watch` label, is the only task state and progress-log source.
- Do not create a standalone `telegram-watch` Todoist project.
- Do not create or use `Bug`, `商业化`, or other category labels for this project.
- Do not disturb sibling labels such as `CodexBar-Mobile` or `Telegram-Watch-Mac`; always filter by `telegram-watch`.
- `docs/inbox.md` is only a rough idea pool. Mature work goes to Todoist Backlog.
- `docs/requests/**`, `docs/templates/REQ_TEMPLATE.md`, and `docs/WORKFLOW.md` are historical archives only.

## Before coding

1. Search Todoist in the shared `Dev` project for relevant tasks with the `telegram-watch` label.
2. If no task exists for a real feature/bug request, create one in Backlog with:
   - content
   - description
   - labels=`["telegram-watch"]`
   - priority=`p1` through `p4`
   - Release Impact: proposed SemVer bump plus one English changelog sentence
3. Move the task to In Progress before implementation.
4. Work on one Todoist task at a time unless the user explicitly pauses or redirects.

## Implementation rules

- Keep changes minimal and scoped to the task.
- Preserve local-first behavior; do not add cloud dependencies unless explicitly approved.
- Never print or commit secrets, phone numbers, `api_hash`, `*.session`, `config.toml`, `data/`, or `reports/`.
- Respect Telegram rate limits and handle FloodWait/backoff where relevant.
- For full archive work, keep it default-off and run/read `archive-status` as the local health gate; use `archive-qa-init` for the gitignored real Telegram QA record draft before claiming live end-to-end readiness.
- If changing `README.md`, apply the equivalent change to:
  - `docs/README.zh-Hans.md`
  - `docs/README.zh-Hant.md`
  - `docs/README.ja.md`
- Build order for MVP behavior remains `doctor` -> `once` -> `run`.

## Validation

Run focused tests for the changed area. For normal development completion, prefer:

```bash
pytest tests/
python -m tgwatch doctor --config config.toml
python -m tgwatch archive-status --config config.toml
python -m tgwatch once --config config.toml --since 10m
```

If a command cannot be run because local credentials/config are unavailable, say that clearly and run the closest safe offline checks.

## Completion workflow

- When code is complete, move the Todoist task to Code Complete and add a short comment with the verification state.
- QA / Release movement requires the user's confirmation after validation.
- Do not mark Todoist tasks complete on your own; the user confirms completion.
- Mirror actionable Todoist comments back in chat because chat is the primary user interface.

## Versioning and changelog

- Every Todoist task should include Release Impact.
- On completion of release-impacting work, update `pyproject.toml` and prepend `docs/CHANGELOG.md`.
- Patch: backward-compatible fixes/docs.
- Minor: additive features or new config surfaces.
- Major: breaking schema or CLI changes.
- README `pip install ...@vX.Y.Z` examples change only after the GitHub tag exists.

## Commit protocol

When the user asks to commit:

1. Commit with both summary and description:
   ```bash
   git commit -m "SUMMARY" -m "DETAILS"
   ```
2. Push immediately unless the user says otherwise.
3. Add a Todoist comment with the commit link.
4. Move the task to the appropriate board column.
