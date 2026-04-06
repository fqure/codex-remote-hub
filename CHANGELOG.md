# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Window screenshot chooser** — turn the mobile `Window` control into a real list of open macOS windows, use titles when macOS exposes them, and capture the selected window by CoreGraphics ID instead of opening the interactive camera picker
- **Window picker scrolling** — make the mobile window chooser sheet and its list use touch scrolling correctly on iPhone Safari so long window lists remain reachable
- **Focused mobile composer** — expand the phone input into a full-screen compose state when focused so Safari keyboard entry leaves more room for editing long prompts and URLs
- **Image URL thumbnails** — preview pasted or inserted `.jpg`/`.png`-style image URLs above the composer with removable thumbnails while still sending only the URL text to the tmux session
- **Mobile screenshot controls** — replace the low-value arrow keys with hub-native `Desk` and `Window` buttons that capture on the Mac and insert the returned asset URL into the composer without routing through Codex
- **Configurable screenshot asset root** — preserve `CODEX_REMOTE_HUB_ASSETS_DIR` in the service config so screenshots and uploaded images can live in a user-chosen folder like `codex-remote-hub/Screenshots`
- **Native macOS screenshot helper** — store hub images under `~/Pictures/Screenshots/<session>`, prefer the Tailscale base URL over localhost, and run macOS screenshots through Terminal so remote screenshot requests stop bouncing through raw tmux `screencapture`
- **Dev-root screenshot instructions** — keep a managed `CODEX_DEV_ROOT/AGENTS.md` block so new hub sessions automatically know to use `codex-remote-shot` when the user asks for a screenshot
- **Mobile image URLs** — upload camera-roll images into session storage, insert served URLs into the composer for approval, and render thread URLs as clickable links instead of using a separate image strip
- **Session screenshot helper** — add `codex-remote-shot` so Codex sessions can capture this machine’s screen into the session asset dir and print a served hub URL back into the thread
- **Capture card labeling** — infer repo/workspace names for running Codex desktop and Cursor sessions so the dashboard shows project context instead of bare PIDs with `/`
- **Mobile page controls** — make `PgUp` and `PgDn` scroll the mobile pane itself, since the mobile shell renders a snapshot instead of a live tmux viewport
- **Mobile page end jump** — add a `PgEnd` control beside `Enter` so long threads can jump straight to the bottom
- **Mobile send acknowledgment** — only clear the input after the sent text appears back in the pane snapshot, instead of clearing optimistically on submit
- **Mobile prompt actions** — detect common terminal prompts like numbered menus, `y/n`, and `Press enter` so iPhone/iPad can surface tap targets instead of forcing keyboard input
- **Mobile dictation and composer sizing** — keep the send row compact during iOS keyboard changes and add a mic entry point that falls back to the native iPhone keyboard dictation path
- **iPhone and iPad mobile shell** — route iOS devices to a touch-first session view with live tmux snapshots, a fixed composer, and raw-terminal fallback instead of the desktop ttyd layout
- **Mobile viewport scrolling** — size the iOS shell to the visible Safari viewport and force the pane to use vertical touch scrolling without horizontal drift
- **iPhone terminal controls** — generate a patched ttyd index with larger mobile scaling, arrow/enter controls, and a fallback send box for iPhone sessions
- **HTTP terminal links** — use `http://` for ttyd when HTTPS certificates are unavailable, so local and non-TLS installs can still open sessions
- **Installer configuration** — preserve `CODEX_DEV_ROOT` in LaunchAgent/systemd services so the dashboard folder picker uses the configured project root after install

## [1.0.0] - 2026-03-04

### Added
- **Initial release** — migrated from [Claude Remote Hub](https://github.com/orseni/claude-remote-hub) for OpenAI Codex CLI
- **Dashboard** — web-based session manager with mobile-first dark theme (OpenAI green accent)
- **Session management** — create, stop, and list Codex CLI sessions via tmux
- **Web terminal** — ttyd-based terminal with virtual keyboard for mobile
- **Session capture** — detect running Codex CLI processes and fork them into hub-managed sessions using `codex fork`
- **Cross-platform** — macOS (LaunchAgent), Linux (systemd), Windows (WSL2)
- **Cross-platform installer** (`install.sh`) — auto-detects OS and package manager
- **HTTPS support** — automatic Tailscale certificate setup, TLS 1.2+
- **Folder picker** — browse and select project directories from the dashboard
- **Permission mode** — optional `--dangerously-bypass-approvals-and-sandbox` toggle
- **API endpoints** — 13 routes for session management, terminal control, and folder browsing
- **Zero dependencies** — pure Python stdlib, no pip packages
- Complete open source infrastructure: MIT license, CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md, ROADMAP.md
- GitHub issue templates (bug report, feature request) and PR template
