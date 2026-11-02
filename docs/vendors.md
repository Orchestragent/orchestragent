# Provider boundaries and evidence

Official documentation was checked on 2026-10-21. Current implementation and newer
DSH/Cursor/Kimi findings are in [vendors-expansion.md](vendors-expansion.md) and
[LIVE_CONTRACT.md](LIVE_CONTRACT.md). Codex native ChatGPT login and two real stages
are verified; Claude awaits official login. DSH requires an explicit API grant;
Cursor/Kimi unattended calls remain blocked. No API spend is authorized. The table
below records the initial research scope, not current executable readiness.

| Candidate | Documented integration | Subscription and unattended boundary | MVP |
| --- | --- | --- | --- |
| Codex | Native app-server JSON-RPC, sessions, streamed events, approvals | Managed ChatGPT auth exists; API-key billing is separate; a personal remote server's entitlement needs confirmation | Disabled descriptor |
| Claude Code | Native `-p`, JSON/stream-json, session resume | Native subscription login exists; credential precedence can select another billing route; SDK product restrictions apply | Disabled descriptor |
| DSH, interpreted as DeepSeek Harness | Official headless profile, JSON events, session ID | Harness availability does not confer model access; headless defaults ignore GUI saved credentials | Research only |
| Cursor | Local headless CLI; distinct Cloud Agents service/API | CLI availability does not establish unlimited cloud use; Cloud Agents have model-priced usage | Research only |
| Kimi Code | Official CLI and third-party coding tools | Subscription policy prohibits non-interactive automation, despite autonomous CLI modes | Excluded from unattended subscription routing |

## Codex

Official OpenAI documentation describes app-server as the rich-client boundary. It
supports thread/turn lifecycle, approvals, and provider-classified rate-limit data.
Its managed `chatgpt` mode lets Codex own sign-in, persistence, and refresh. External
token mode is a different, experimental mechanism; this project must not extract or
inject existing OAuth tokens. Native login status was checked; no OAuth tokens were extracted.
[App-server reference](https://developers.openai.com/codex/app-server).

ChatGPT plan usage and API-key usage are separate billing routes. The current pricing
page says local and cloud use share allowances, with task/model-dependent consumption;
plan inclusion is not a fixed number of jobs or unlimited capacity. Hitting limits
must surface a blocked state rather than trigger an API-key fallback or credit purchase.
[Pricing](https://developers.openai.com/codex/pricing).

The settings default is `codex` / `gpt-6.1-sol`. The current official model guidance
recommends GPT-6.1 Sol for complex, repeated agentic workflows with lower cost than
Astra. This is a saved preference, not a claim that this account can use that model,
an executed model call, or authorization to incur charges. A future real scheduler
must verify availability and the chosen billing route before using it.
[Official model guidance](https://developers.openai.com/codex/models).

The separate Sign in with ChatGPT plan-sharing preview describes open-source/local
apps and self-hosted VMs, but asks paid or remotely hosted apps to use an interest
form. That is not blanket authorization for this architecture, nor authorization to
transfer credentials. Whether a proposed personal remote orchestrator fits the
applicable entitlement remains unresolved until its concrete deployment is reviewed.
[Plan-sharing scope](https://developers.openai.com/siwc/token-sharing-open-source).

## Claude Code

The native CLI supports Pro/Max and organizational subscription sign-in. Its documented
credential precedence includes provider selection and environment/config credentials;
an approved `ANTHROPIC_API_KEY` can override subscription auth. A future adapter must
verify the selected route without exposing secret values and stop on ambiguity.
This milestone does not inspect or modify environment credentials.
[Authentication](https://code.claude.com/docs/en/authentication).

Print mode supports noninteractive execution, JSON/stream-json, session continuation,
and explicit tool permissions. The documentation warns that ordinary `-p` can load
project hooks/MCP without a workspace trust dialog. `--bare` changes that behavior
but also bypasses subscription credentials and requires a supported API auth route;
it is therefore not a free subscription-preserving switch.
[Programmatic CLI](https://code.claude.com/docs/en/headless).

The Agent SDK documentation states that third-party developers cannot offer claude.ai
login or rate limits in their products without prior approval, and directs them to
API-key methods. Native CLI availability does not remove that product-integration
restriction. A personal native-CLI proposal needs its own entitlement review; this
document neither promises SDK subscription reuse nor attempts it.
[Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview).

## DSH

The official repository identifies `dsh` as DeepSeek Harness. That is the interpretation
used here; if the user's DSH denotes another product, its URL is still needed.
The harness is a developer preview, not a promise of free hosted inference.
[Official repository](https://github.com/deepseek-ai/deepseek-harness).

Its headless profile runs one task and exits, supports `--json` and `--session-id`,
and exposes exit/turn failure signals. The shipped profile disables the managed
credential store: API-key routes consult the launch environment and documented `.env`
fallbacks; GUI sign-ins are ignored by default. Restoring saved auth is an explicit
configuration change, not something an orchestrator may infer from an installed GUI.
No DSH adapter, launcher, credential reader, or profile change was added.
[Headless README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md).

## Cursor

The local CLI supports print mode and structured streaming. Its documentation requires
explicit force behavior for scripted file modifications; that option is not a proposed
permission policy for this project.
[Headless CLI](https://cursor.com/docs/cli/headless).

Cloud Agents run in isolated development environments and can clone and push repositories.
Their documentation states selected-model API pricing and a spend limit at first use.
Cloud deployment and remote pushes are outside the approved MVP.
[Cloud Agents](https://cursor.com/docs/cloud-agent).

The pricing page observed on the verification date lists Ultra at USD 200/month and
50× Plus usage, with third-party model usage billed separately. This differs from
older descriptions of Ultra as 20× Pro. Neither multiplier establishes a fixed task
count, the user's remaining quota, or an all-inclusive Cloud Agent entitlement.
Taxes, currency conversion, regional availability, and actual account terms remain
unverified; no RMB budget equivalence is assumed.
[Current pricing](https://cursor.com/pricing).

## Kimi

Kimi Code's current community guidelines limit subscriptions to personal interactive
use and explicitly prohibit non-interactive automation such as scripted batches.
They also prohibit reselling access or spoofing client identity. Therefore this project
must not route unattended jobs through a Kimi Code subscription or disguise its client.
[Community guidelines](https://www.kimi.com/code/docs/en/kimi-code/community-guidelines.html).

The CLI does document a fully autonomous Never Ask mode. A technical mode does not
override the subscription policy above. Product/API integration would need the separate
Kimi Platform terms, pricing, and explicit approval before implementation.
[CLI modes](https://www.kimi.com/help/kimi-code/cli-interaction),
[Kimi Code versus Kimi Platform](https://www.kimi.com/code/docs/en/).

The membership page distinguishes rolling rate windows, legacy weekly quota, monthly
quota, and optional paid Extra Usage. Devices and keys share the same allowance.
This supports separate quota/rate-limit handling, not account rotation, automatic
top-ups, or silently enabling paid overflow.
[Membership](https://www.kimi.com/code/docs/en/kimi-code/membership.html).

## Implemented mock behavior

`StepResult` keeps approval, transient rate limit, exhausted quota, missing authentication,
timeout, and failure separate. No real-provider fallback exists. Review uses a distinct
session and a fixed revision, and rejects unexpected artifacts. In the explicit
`test_failure` demo, it accepts only the known faulty fixture for structural review
and states that functional correctness is deferred to actual controller tests. This
lets the downstream test failure be demonstrated without claiming a correct review.

All mock costs are zero. Reported CLI cost estimates, when a real provider is eventually
approved, must not be treated as proof of a subscription charge or the actual invoice.

Settings revisions and proposal applications are persisted atomically in SQLite.
Policies contain only scheduler/provider model preferences and instructions. All five
known provider IDs must appear once; unrecognized fields, including enablement,
credentials, budgets, and shell commands, are rejected. Model fields allow at most 128
characters, instruction fields 4096, and the whole canonical policy 12000 UTF-8 bytes.
One-sentence proposals accept up to 2048 characters and append the complete sentence
to the sole mentioned provider, or global instructions when zero/multiple providers
are named. Preview text explicitly disclaims semantic interpretation. Applying a stale
preview fails; replaying an applied proposal returns its original applied revision.
