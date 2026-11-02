# Security

orch executes model-generated code. Its guarantees:

- Models only return JSON; the controller alone writes files and runs commands.
- Generated and frozen tests run in an OS sandbox (bubblewrap + seccomp on Linux, Seatbelt
  on macOS) without network access, with wall/CPU limits.
- API keys are read without echo, stored locally (macOS Keychain, or a 0600 file in the data
  directory) and never written to logs, the state database or reports.
- `orch watch --web` listens on loopback by default; any other address requires a token.

Merging the result branch into your code is your decision: review it like any contribution.

Please report vulnerabilities privately through GitHub's "Report a vulnerability"
(Security tab) rather than a public issue.
