# DevWisp (Remote Development Workbench)

<p align="center"><img src="public/devwisp-robot.svg" alt="DevWisp robot logo" width="96"></p>

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/web%20version-0.143.0--dev.1-orange?style=flat-square" alt="Web version 0.143.0-dev.1"></a>
  <a href="https://github.com/wottyup/devwisp/stargazers"><img src="https://img.shields.io/github/stars/wottyup/devwisp?style=flat-square" alt="GitHub stars"></a>
  <a href="https://github.com/wottyup/devwisp/forks"><img src="https://img.shields.io/github/forks/wottyup/devwisp?style=flat-square" alt="GitHub forks"></a>
  <a href="https://github.com/wottyup/devwisp/issues"><img src="https://img.shields.io/github/issues/wottyup/devwisp?style=flat-square" alt="Open issues"></a>
  <a href="https://github.com/wottyup/devwisp/commits/main"><img src="https://img.shields.io/github/last-commit/wottyup/devwisp?style=flat-square" alt="Latest commit"></a>
</p>
<p align="center">
  <a href="android/app/build.gradle"><img src="https://img.shields.io/badge/Android-8.0%2B%20%28API%2026%29-3DDC84?logo=android&amp;logoColor=white&amp;style=flat-square" alt="Android 8.0 and up"></a>
  <a href="package.json"><img src="https://img.shields.io/badge/React-19-149ECA?logo=react&amp;logoColor=white&amp;style=flat-square" alt="React 19"></a>
  <a href="package.json"><img src="https://img.shields.io/badge/Vite-7-646CFF?logo=vite&amp;logoColor=white&amp;style=flat-square" alt="Vite 7"></a>
  <a href="server/requirements.txt"><img src="https://img.shields.io/badge/FastAPI-Python-009688?logo=fastapi&amp;logoColor=white&amp;style=flat-square" alt="FastAPI Python backend"></a>
  <a href="android/build.gradle"><img src="https://img.shields.io/badge/Kotlin-2.0.21-7F52FF?logo=kotlin&amp;logoColor=white&amp;style=flat-square" alt="Kotlin 2.0.21"></a>
</p>

Remotely view and control Codex and Claude Code running on your development computer from a mobile browser or native Android app. Supports session history, real-time message exchange, model selection, attachments, workspace files, and a terminal; the coding assistants run on the development computer.

## Team Chats (Web Preview)

Chat and Teamwork share the same conversation sidebar, settings, usage views and workspace panel. Click **New team chat** to create an empty group immediately. Add Codex, Claude or DeepSeek with **+** inside the group; the same engine can join more than once. There are no required roles or setup form. The engines must already be installed and configured.

To bring an existing conversation with its context, right-click it in the left sidebar and choose **Add to team chat**, then an existing group or **New team chat**. Busy chats, pending approvals, queued messages and unconfirmed sends must be resolved before joining. A native conversation belongs to one group at a time. Members share the project directory; the first existing chat can set the directory of an unused group. Removing an idle member retains its history and restores ordinary-chat access. To dissolve a group, right-click its sidebar entry (long-press on mobile) and choose **Dissolve group**. This removes the group conversation and releases all members while preserving their individual histories; replies already in progress continue in the individual chats.

The group uses the same composer as single chats: paste or upload images and documents, translate input, select model and permission settings, schedule messages, and edit or reorder the queue. Drafts, attachments and queues are isolated by group. In mixed groups, choose the engine above the composer to edit that engine’s settings; each message keeps its own settings snapshot. You do not need to create a single chat first.

Write a message in the group as you would in a single chat. Members discuss in parallel using the same snapshot of the conversation, with native read-only permissions. Start with **@** to choose who responds (for example, `@Codex 2` or multiple leading mentions). `@Codex`, `@Claude` and `@DeepSeek` work when the engine has one member; use numbered names when there are several. Without a mention, or with `@all` / `@所有人`, everyone participates. Responses stay in the group, and the next round includes all participants’ previous results; very long replies are excerpted without dropping a participant, while the complete messages stay in the history.

When the current request calls for implementation and the discussion yields a workable proposal, a read-only planning pass assigns specific files, owners and dependencies. Independent tasks run in separate development snapshots; overlapping files, dependencies and a member’s own tasks are serialized. Each snapshot includes the project’s existing uncommitted changes. The backend integrates only assigned files after checking that the source has not changed. Incomplete work, out-of-scope changes or integration conflicts pause development and retain the isolated work for inspection. Successful temporary workspaces are cleaned, and a final read-only pass checks and summarizes the integrated result. No mode switch or extra page is needed; ordinary discussion stops after its replies, and uncertain plans stay in discussion rather than writing files.

Messages sent during a round wait until its discussion, development and summary finish, including Ctrl+Enter. Translated and scheduled messages retain recipient IDs; if a recipient leaves, edit the queued message to choose another member. Execution sessions open from the member buttons while they are active. Earlier saved sequential rounds finish under their original scheduling rules. The group title follows its first message unless renamed; right-click it in the sidebar (long-press on mobile) to rename it. Title icons show all current members.

Open a member to inspect its native work and handle approvals or questions. Injected collaboration context in the native history is collapsed by default; expand it to read the complete prompt. Pause stops further handoffs and attempts to interrupt the current turn; continue resumes that round after checking its state. Service restarts leave active rounds paused, and unconfirmed sends are not replayed. Previously created implementer/reviewer tasks retain their original behavior and remain accessible from history.

The shared file panel and terminal follow the group directory. File paths and document text insert into the group composer. No extra dependencies or Android-native group page are introduced.

## Prerequisites

- Codex CLI installed and logged in, invoked as `codex`
- Node.js 20.19+ and pnpm
- Python 3.10+
- Phone and computer on the same trusted LAN

## First-time Setup

Create a Python virtual environment in the project directory and install the server dependencies:

```bash
cd /code/devwisp
python3 -m venv .venv
. .venv/bin/activate
pip install -r server/requirements.txt
```

Frontend dependencies are managed by `/code/pnpm-workspace.yaml`. After running `pnpm install` once from the workspace root, use the start command below.

## Starting

For day-to-day remote use, run the following in a terminal on your computer: it builds the web frontend, then starts both the local Codex engine and the LAN page.

```bash
cd /code
pnpm --filter devwisp start
```

The built assets support compression and caching, which is good for long sessions on a phone. Only use `pnpm --filter devwisp dev` when editing the frontend and you want hot reload; the dev server loads heavier debug assets.

When startup finishes, the terminal prints a pairing code needed to authorize a new browser. Open `http://<computer's LAN IP>:26546` on the phone. After pairing succeeds, that browser is recorded as a trusted device: you won't need to enter the code again even if the local service restarts; only a new browser, an incognito window, or clearing site data requires re-pairing. You can also visit `http://127.0.0.1:26546` locally on the computer.

The web version connects only to the DevWisp service at the URL currently open. To access another computer, just open that computer's web address and pair separately.

To keep it running in the background, install the systemd units from the repo:

```bash
sudo install -m 0644 /code/devwisp/deploy/devwisp.service /etc/systemd/system/devwisp.service
sudo install -m 0644 /code/devwisp/deploy/devwisp-tmux.service /etc/systemd/system/devwisp-tmux.service
sudo systemctl daemon-reload
sudo systemctl enable --now devwisp-tmux.service devwisp.service
```

These units build the web frontend at startup and serve the built assets. After updating an installed unit, run `systemctl daemon-reload`; an already-running service must be restarted after its chat tasks finish to load the new backend. The service also manages Codex child processes, so restarting interrupts any tasks still in progress.

View the resident service's pairing code and logs with:

```bash
journalctl -u devwisp.service -f
```

### Migrating an existing installation

The service unit, environment variables, Python module, and data directory now use the DevWisp names. Finish any active chats before switching units, because stopping the old service also stops its child processes and terminal sessions.

For an existing systemd install, copy the old environment file if present, change each `CODE_REMOTE_` variable to `DEVWISP_`, stop the old units, then install and enable the new units above. If your previous units used the `codex-remote` names, stop those units instead:

```bash
sudo cp -a /etc/code-remote.env /etc/devwisp.env
sudo sed -i 's/CODE_REMOTE_/DEVWISP_/g' /etc/devwisp.env
sudo systemctl disable --now code-remote.service code-remote-tmux.service
```

If no old environment file exists, skip both environment-file commands. Update custom launch scripts from `python -m code_remote.main` to `python -m devwisp.main`. Browser preferences, cached conversations, and the trusted-device token file migrate automatically when the new storage location is empty. On Windows, `DEVWISP_HOME` defaults to `%LOCALAPPDATA%\DevWisp`; existing data in `%LOCALAPPDATA%\CodexRemote` remains untouched and is not automatically merged.

The web service forwards API and WebSocket requests to `127.0.0.1:18766` on the computer. The Codex App Server is reached only via local stdio; it opens no network port. The web connection between phone and computer uses LAN HTTP; use it only on trusted networks and allow the computer's firewall to accept connections on port 26546.

Both Windows and Linux are supported. On native Windows, create `.venv` in a PowerShell session in the project directory, then run `pnpm --filter devwisp start`, or double-click `start-devwisp.bat`; the launcher automatically uses `.venv\Scripts\python.exe`, the shared `F:\1code\node_modules` workspace dependencies, and supports `codex.cmd`/`codex.exe`. If Codex is not on PATH, set it explicitly:

```powershell
$env:DEVWISP_COMMAND = "C:\Users\<your username>\AppData\Roaming\npm\codex.cmd"
$env:DEVWISP_WORKSPACE = "C:\path\to\your\project"
pnpm --filter devwisp start
```

On Linux/macOS the chat terminal keeps sessions alive with tmux; on Linux run `sudo apt-get install tmux`, on macOS run `brew install tmux`. When using systemd, also install `devwisp-tmux.service` above so the tmux shell can reconnect after the Python service restarts. Sessions are cleaned up after a computer reboot, when the tmux service is stopped manually, when you type `exit`, when a session is idle for 30 minutes, or when there are more than 10 unattached sessions. Native Windows terminals keep temporary-shell behavior.

## SSH MCP Remote Servers

In the web UI, go to Settings → SSH MCP Servers and enter the server IP or hostname, port, username, and password; you can add multiple hosts. SSH passwords are stored on the computer running DevWisp, and the settings page never echoes saved passwords. After saving, new or reopened Codex conversations use the latest settings; running conversations are unaffected. SSH MCP currently connects only to Codex; Claude Code's command approval cannot yet be forwarded to the web UI.

The backend must be installed on the same host as the Codex it drives; a Linux backend cannot directly see a Codex process on a separate Windows host.

Trusted-device token digests are stored in the user data directory; raw tokens and pairing codes are never written to that file. Windows defaults to `%LOCALAPPDATA%\DevWisp`, keeping app data out of the project or restricted Codex run directories; Linux/macOS default to `~/.codex/devwisp`. Signing out in the web UI revokes the current device; if a device is lost, stop the service, delete the authorization files in that directory, and start it again to re-pair all devices. Locations can be overridden with `DEVWISP_HOME` or `DEVWISP_AUTH_FILE`.

## Working Directory and Session History

New conversations default to the directory above the app repo as the working directory (for example, if the app lives at `/code/devwisp`, the default working directory is `/code`), so newly cloned projects sit alongside `devwisp` rather than inside the app repo. You can override this with an environment variable or a start argument:

```bash
DEVWISP_WORKSPACE=/path/to/your/project pnpm --filter devwisp engine:dev
```

The history list reads the 100 most recent non-archived Codex sessions on the machine, including ones created in other working directories; after opening a session, its working directory is shown below the title. The backend watches the Codex session files and pushes updates to the sidebar over WebSocket, so no page refresh is needed.

The local Codex App Server uses the user-level `CODEX_HOME` directly by default, so the web UI and desktop Codex read and write the same configuration, models, and session history. Like Hapi, on Windows it prefers the Codex CLI actually resolved from PATH and starts the app-server directly, connecting through a random port that listens only on `127.0.0.1` plus a capability token; it no longer depends on a shared daemon/control socket requiring extra privileges. A code-installed Codex resolves from the `.cmd` shim to the `codex.js` in the same installation and is started by Node. Conversations created or resumed in the web UI are written to the user's existing history by the Codex app-server. You can set `DEVWISP_APP_SERVER_HOME` explicitly to pin a separate data directory.

## Handing Sessions Back and Forth Between the Web UI and Local Codex

A session to which the web UI has sent messages is held for writing by the web-specific App Server. To continue that session in the local Codex, first wait for the web reply to finish, then click "Return to machine" at the top-right of the chat page and confirm. This only closes the web-specific Codex child process; it does not restart the dev service, SSH sessions, or browser pairing, and the web UI will not automatically reclaim the session.

To continue in the web UI again, make sure the local Codex has finished the session's tasks, then click "Take over" at the top-right. If the machine still holds the session, the web UI clearly warns that the session is being used by another Codex instance.

## Building

```bash
cd /code
pnpm --filter devwisp build
```

Build output goes to `devwisp/dist`. `pnpm start` builds automatically and serves the page via Vite preview, with `scripts/dev.mjs --production` starting the Python engine at the same time; under systemd, `devwisp.service` manages it continuously in the background.


### Local DeepSeek Harness

Install the pinned Python dependencies with `.venv/bin/python -m pip install -r server/requirements.txt` (Windows: `.venv\Scripts\python`). This includes the official Harness SDK and its native `dsh` runtime; no system Node.js is required for ordinary SDK operation. Click the **DeepSeek Harness** logo among the three engine buttons in the chat header, then configure the protocol, model, API URL and API key under **Settings → DeepSeek API**. Third-party APIs support **OpenAI Responses**, **Anthropic Messages** and **OpenAI Chat Completions** through Harness’s native drivers, including streaming and tool calls. Enter the API base URL (optionally ending in `/v1`), without `/responses`, `/messages` or `/chat/completions`; version suffixes are normalized for each driver. Use the model identifier supplied by your provider. Save multiple named profiles for primary and backup providers. **Save configuration** stores an edit without changing the active provider; **Save and apply** switches it for the next turn and updates the active DeepSeek chat model without losing session history. **Test connection** sends a small real request using the current form and the selected profile’s saved key when the key field is blank; it does not save or apply changes. It may incur a small API charge. Existing single-provider settings are imported automatically.

The backend also accepts `DEEPSEEK_API_KEY` and `DEEPSEEK_BASE_URL`. Native Harness credentials continue to work for the official DeepSeek route; third-party routes use the saved key or `DEEPSEEK_API_KEY` and do not fall back to unrelated OpenAI/Anthropic credentials. Keys are stored only in the private backend configuration and are never returned to the browser.

For `mercury-3` through Chat Completions, the reasoning menu offers **default**, **instant**, **low**, **medium** and **high**. This adapter's default is **high**; saved **max** selections also use **high**. The native runtime maps **instant** through its **minimal** level. Other models retain their existing reasoning choices. API failures display classified guidance without exposing raw provider diagnostics.

The integration uses the `sdk` profile with an explicit `DSH_HOME` (default `~/.dsh`). Before sending the first message in a new DeepSeek chat, use the compact **Agent** menu to choose Harness’s native **Standard**, **PTC**, **Minimal** or **Creator** composition. The choice is recorded and locked for that chat, including queued messages and recovery; start a new chat to change it. Existing chats retain their original composition as **Legacy SDK**. Minimal uses its native reduced tools and persistent-shell implementation and does not offer plan mode. Consecutive turns with unchanged configuration reuse the conversation’s native process, preserving native persistent-shell state. Settings, model, reasoning or permission changes, failures, stopping, deleting a conversation and service restarts rebuild or release that process; conversation history remains durable. Internal shell state does not survive a process restart. The chat shows runtime status, provider-returned reasoning, per-turn main-agent token usage and separate child-agent activity. Context occupancy appears only when native request capacity/counters are available; usage and reasoning are not estimated for providers that omit them. Creator loads native host inspection tools; plugin panels that require the original Harness client are not embedded. Preset definitions match the pinned Harness runtime and preserve native loader expressions. Plugin configuration belongs to the `sdk` profile. Backend plugins can be loaded here; plugin panels are not yet embedded in Dev Wisp. Native questions support single/multiple selections and custom answers, while plan reviews show the plan and return your explicit decision. Pending forms recover after a reconnect. Ordinary messages queue during a reply; **Ctrl+Enter** or **Send now** adds a message to the current native task for its next step, retaining that turn’s model and permission settings. Slash commands that manage session state finish before a new prompt begins. The permission menu offers read-only, workspace write, full access with approval enabled, full access without prompts, and plan mode. Approval-enabled presets allow requests for operations that need approval; they do not require approval for every ordinary operation. Plan mode enforces read-only inspection until an explicit successful plan review: shell commands, mutations, SSH/terminal MCP, delegation, plugin changes and general PTC programs are blocked. PTC inspection accepts a single literal call to the read/search/question/review tools. Approving the plan releases workspace-write execution from the next model step; rejection, cancellation and entering a new plan turn keep or restore the restrictions. A separate command approval or a general question cannot approve the plan. **Terminal → Hand over to AI** grants access to the already-connected SSH terminal until revoked. Configured **Settings → SSH MCP** servers load on the next DeepSeek turn, keeping each server’s role, read-only restriction and approval policy. SSH MCP confirmation requests appear in the browser even when the local Harness preset allows full access. The model, credentials and workspace are applied when the next turn starts; persisted session history is resumed without creating a replacement on recovery errors.

The SDK is pinned to `0.2.2rc2`, a developer preview. Its Linux x64 flock addon requires AVX even though the main runtime can start without it. On a Linux x64 host without AVX, run `.venv/bin/python scripts/repair-harness-flock.py` once. This builds the official `@deepseek-ai/node-addon-system@0.1.2` flock source for baseline x86-64, checks locking and contention, and installs it separately in the user cache. Dev Wisp's small Harness bridge loads it without changing the official binary, Landlock or permission policy. This repair needs a C compiler and Node.js headers. The installed CLI is `.venv/bin/dsh`; for direct SDK-profile CLI use, apply the same bridge configuration as the adapter on hosts requiring this compatibility repair.

### Chat token usage

The web composer displays a compact **Chat tokens** total beneath the input. It follows the selected conversation and updates through the existing paired chat connection as soon as the engine returns usage; incomplete reporting is labeled explicitly. The project dashboard also refreshes on usage events. Initial loads, reconnects and returning to the page recover the latest saved counts; a 30-second fallback supports older connections. If an engine reports usage only at request completion, counters update then rather than estimating consumption from generated text.

Open **Token usage → Project usage** to see usage returned by Codex, Claude Code and DeepSeek Harness for chats started through Dev Wisp, including third-party API providers. Filter by date, engine or model, view daily trends and open individual turns. Counters are stored locally in `chat-usage.sqlite3` alongside shared state and survive service restarts; no prompts, replies or API keys are stored in the ledger.

Recording starts after the updated backend is loaded. Historical CLI sessions are not imported. Unreported counters appear as **—**, and partial or missing usage is identified explicitly. Cache tokens are included in input and reasoning tokens in output, so neither is added twice. The existing provider dashboards remain available under **Platform usage**; their billing totals are separate.
