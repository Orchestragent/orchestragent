# Orch Studio for macOS

A dependency-free SwiftUI client for `orch studio`, targeting macOS 14+. The native
window combines a run sidebar, a draggable dependency graph, and a scoped node inspector.
The API remains the sole authority for task permissions, frozen checks, and merges.

## Download (no terminal needed)

Download **OrchStudio-macOS-arm64.dmg** from the
[releases](https://github.com/Orchestragent/orchestragent/releases) (Apple silicon), open
it and drag **Orch Studio** to Applications. The app carries its own `orch` (a private
Python runtime in `Contents/Resources/orch-runtime`), so nothing else needs installing,
and it hands orch your login shell's `PATH` so your project's toolchains (go, node,
cargo, …) are found even when the app is opened from Finder.

The build is not notarized yet, so the first open is blocked by Gatekeeper: open
**System Settings → Privacy & Security** and click **Open Anyway** next to "Orch Studio"
(once). Subscriptions such as Claude still log in through their own CLI (Settings →
Providers → Log in opens Terminal for that one step); API keys are entered in the app.

## Build from source

Open **OrchStudio.xcodeproj**, select **OrchStudio → My Mac**, and Run. The shared scheme
starts in demonstration mode. Install the `native` backend first (`uv sync --locked --no-editable` at
repository root), then set **Settings → Server → orch executable** to this checkout's
`.venv/bin/orch`, or install `orch` on your PATH. First launch offers direct repository
path entry, Browse, or Demo. This keeps startup responsive when macOS
File Provider stalls while initializing its native folder picker; the automatic
NSOpenPanel behavior in the original spec is intentionally replaced by this choice. The demonstration uses no provider keys
and makes no model calls. **File → Open Project** starts a real local service for a Git
repository; **File → Open Demo** returns to the sandbox. Running an actual spec uses your
configured providers and their normal billing.

Real repositories are served by `RepoBackend` (docs/STUDIO.md); the demo and real
backends speak the same API.

For a checkout-local launch without changing the saved executable preference:

```sh
ORCH_STUDIO_EXECUTABLE="$PWD/.venv/bin/orch" \
  /tmp/OrchStudioBuild/Build/Products/Debug/OrchStudio.app/Contents/MacOS/OrchStudio --demo
```

Build and verify from the repository root (Xcode 15.3+):

```sh
python3 apps/macos/scripts/update_strings.py
python3 apps/macos/scripts/generate_project.py
xcodebuild -project apps/macos/OrchStudio.xcodeproj -scheme OrchStudio \
  -configuration Debug -derivedDataPath /tmp/OrchStudioBuild build CODE_SIGN_IDENTITY=-
TEST_RUNNER_ORCH_STUDIO_EXECUTABLE="$PWD/.venv/bin/orch" \
ORCH_STUDIO_EXECUTABLE="$PWD/.venv/bin/orch" \
xcodebuild -project apps/macos/OrchStudio.xcodeproj -scheme OrchStudio \
  -destination 'platform=macOS' -derivedDataPath /tmp/OrchStudioBuild \
  test CODE_SIGN_IDENTITY=- ONLY_ACTIVE_ARCH=YES
```

Use a derived-data directory outside a File Provider or synchronized folder: Finder
metadata injected into bundles can cause macOS ad-hoc signing to fail. For app-launched
Python inside protected/synchronized Documents, use a development environment outside
that folder (`uv venv /tmp/orch-studio-demo-env --python 3.12`, then
`uv pip install --python /tmp/orch-studio-demo-env/bin/python .`) and choose its `bin/orch`
in app settings. This needs no change to macOS privacy permissions. The generated
project is checked in; generation requires only Python's standard library. String
Catalog entries are collected from bilingual `L` calls. Runtime values from the backend
(such as diagnosis text and reports) are intentionally shown verbatim.

The graph supports drag, Shift-drag grid snapping, background pan, trackpad scrolling,
pinch zoom, Cmd+/−, Cmd+0 fit, Shift+Cmd+L relayout, nearest-direction arrow navigation,
and Tab traversal. Positions persist per repository/run. The inspector has Conversation,
Attempts, Tests, Files and Spec tabs. A DONE node only rebuilds when its explicit rework
checkbox is enabled; PARKED guidance automatically retries; REPLANNED and unknown states
are read-only. Disconnected writes are disabled.

Local launch keeps stdin open for child lifetime, validates the machine handshake,
redacts and rotates server logs, and reconnects/restarts an exited owned process. Remote
mode stores its token in Keychain and never starts a process. Finder/editor actions are
only available for existing files contained within an actual local repository, including
resolved symlinks. API keys clear from input state before awaiting submission; no key is
returned or logged. Subscription login opens a self-deleting Terminal command with a
restricted documented command vocabulary.

The test target covers unknown enum values, nullable backend roles, SSE framing, stable
cycle-safe layout, route effort ordering, and the message-state matrix. Live demo tests
exercise authenticated endpoints, per-node guidance, SSE-only refreshes, upstream blame,
rework/downstream ripple, start/cancel/resume, unauthorized responses, and owned child
cleanup. They skip with an explicit reason if `orch` is unavailable; CI installs the
backend so these tests must run. No provider login, actual API key, paid model run, or
production deployment is part of this suite.

GitHub Actions builds a Release `.app`, signs ad-hoc, and uploads `OrchStudio-app` plus
XCTest results. Notification permission, VoiceOver, keyboard feel, and both light/dark
appearance should also be checked on the target Mac before a release. There is no
notarization or automatic update mechanism yet.
