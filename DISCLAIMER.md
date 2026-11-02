# Disclaimer: your models, your accounts, your terms

orch is an orchestration architecture. It does not ship, resell or proxy any model. Every
model call goes to a provider **you** configure — bring your own CLI subscription (BYOC)
or bring your own API key (BYOK) — under **your** account and that provider's terms.

**You are responsible for using each provider within its terms of service**, including
any rules on automated or programmatic use, use through third-party tools, rate limits,
data handling and acceptable use. Terms change; read your plan's current terms before
connecting it to orch.

In particular:

- **Some consumer subscriptions do not allow automated, non-interactive or third-party
  use.** Do not connect such a subscription to orch. orch refuses the combinations it knows
  to be disallowed (currently: Kimi's subscription), but this list is not exhaustive and is
  not legal advice. When in doubt, use the provider's API with a key (BYOK), which is
  intended for programmatic use.
- **CLI subscriptions (Claude Code, Codex CLI, Cursor CLI)** are used only through the
  vendor's own official CLI in its documented non-interactive mode, logged in by you.
  orch never extracts, copies or shares login tokens, and never switches accounts or
  billing modes.
- **API keys** are stored only on your machine (macOS Keychain, or a 0600 file in the orch
  data directory) or read from an environment variable you name. They are never logged,
  written to the state database or sent anywhere except the provider you registered.
- **Costs are yours.** orch records spend it can see (`orch cost`, `orch doctor`) but sets
  no spending cap of its own; your provider's balance or limits apply. API-key routes are
  billed per token.
- **Results are not guaranteed.** The published benchmarks used the verified model set
  shown by `orch models`. Any other combination is unverified: quality, speed and cost may
  differ. Review every result branch before merging; generated code is your responsibility.
- **Data.** Prompts contain your specification and source code and are sent to the
  providers you configure. Do not use providers whose data terms you do not accept for
  that code.

orch is provided "as is", without warranty of any kind, under the Apache License 2.0
(see LICENSE).
