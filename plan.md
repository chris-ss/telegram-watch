# Plan — telegram-watch

> Generated: 2026-03-20 | Version: v1.5.0 | Branch: `dev`

## Project Status: Stable / Maintenance

The core product (MTProto user-account watcher with GUI, bridge mode, topic routing, and multi-admin support) is feature-complete at v1.5.0. All 70+ requirements have been delivered and archived to `docs/requests/Done/`.

## Completed Milestones

| Milestone | Key REQs | Version |
|-----------|----------|---------|
| MVP bootstrap (capture, report, control chat) | REQ-001 ~ 006 | v0.1.x |
| Media, reply context, push notifications | REQ-007 ~ 014 | v0.2.x ~ v0.3.x |
| Bark notifications, heartbeat, error handling | REQ-018 ~ 024 | v0.3.x |
| Topic routing, multi-admin, bridge mode | REQ-025-002 ~ 129-002 | v0.4.x ~ v1.0.0 |
| GUI (config editor, launcher, stop/run controls) | REQ-203 ~ 206 | v1.1.x ~ v1.3.x |
| Forum reply disambiguation, CLI unification | REQ-212-001 ~ 004 | v1.0.4 |
| Skip HTML report, network reconnect resilience | REQ-304-001, REQ-310-001 | v1.5.0 |

## Active Backlog

No `Approved` or `Implementing` requirements. Backlog is clean.

## Inbox / Ideas

`docs/inbox.md` is empty — no pending ideas in intake.

## What's Next (Suggested Priorities)

When new work arises, follow the existing requirements workflow (`docs/WORKFLOW.md`):

1. Add ideas to `docs/inbox.md`
2. Formalize to `docs/requests/REQ-YYYYMMDD-###-slug.md` (Status: Draft)
3. Get approval → implement → complete

Potential directions (not yet formalized):
- **Stability**: long-running daemon stress testing, edge-case error recovery
- **UX**: GUI improvements (theme, log viewer, real-time status dashboard)
- **Distribution**: packaging (Homebrew, PyPI release, `.app` bundle)
- **Integrations**: additional notification channels beyond Bark
