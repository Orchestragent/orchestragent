# Local validation — 2026-10-21

Mac arm64, Python 3.12.2, Xcode 26.6, Swift 6.3.3, iPhone 17 Pro simulator.
The private remote was verified empty before initial implementation. Existing work was preserved.

## Current acceptance

- Python full suite: 359 passed (37.61s), including the final DSH request-bound guard.
  Ruff and mypy passed (38 source files).
- Swift simulator: 14 tests executed, 1 skipped, 0 failures; TEST SUCCEEDED.
  The skipped settings integration would invoke a real scheduler on the live service.
- Local HTTP lifecycle tests explicitly use Mock; these do not consume vendor quota.
- Shared canonical JSON implementation remains only in serialization.py; no duplicated
  atomic-write helper. No re-export-only package initializers.

Tests cover durable queue/event transactions, idempotency, state fencing after cancel,
restart recovery, approval digests, versioned configuration, native error classification,
process-group cleanup after parent death, output limits and API authorization boundaries.
Native tests use fake executables/clients, never vendor inference. macOS sandbox canaries
verify denial of home-file access, networking, fork and shell execution. Generated tests
execute the exact reviewed revision, require a completed unittest run with nonzero test
count, and can be explicitly retried in a fresh verification directory.

## Actual provider evidence

Real task `09209f7a7bb44f56ad1746fddcd4cf5f`: a Unicode slugify Python project.
Codex ChatGPT subscription with gpt-6-astra completed planning and implementation:
planning reported 9826 input / 438 output tokens; implementation 10255 / 1419.
Implementation revision: `e9d6a2a891776c8af942e584af3f5a907392f25b`.
Task is persisted at review/auth_required because Claude native subscription login is
needed. Independent review, integrated tests and full multi-vendor completion are not
claimed. Restart preserved the blocked task without replaying inference.

DSH official config/JSONL adapter tests passed using fixtures; no real DSH call or API
spend occurred. DSH calls are also policy_blocked until an enforceable native request
limit is verified; a hallucinated tool-call can otherwise trigger another agent-loop step. Cursor official CLI is installed but dispatch is policy_blocked pending
verified extension/tool isolation and billing scope. Kimi native 2.2.0 checksum was
verified; official login is available for manual interaction, not unattended Max routing.

## User handoff and limits

Mac loopback service runs on 127.0.0.1:8765. Official native login is user-authorized;
`scripts/login.command` was opened in Terminal. Credentials are entered only into official
flows or local getpass. DSH keys stay in process memory; the exact grant requires local
confirmation. No API grant has been made by the assistant.

The live implementation currently handles Python standard-library projects. No arbitrary
language sandbox, dynamic DAG, authenticated real-phone connection or persistent launch
agent has been implemented. The Mac must remain awake and its service process alive.
No remote push, production/Linux access, publication or physical iPhone installation.
Build/test artifacts under .build are ignored by Git.
