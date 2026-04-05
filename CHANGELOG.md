# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Capture card labeling** — infer repo/workspace names for running Codex desktop and Cursor sessions so the dashboard shows project context instead of bare PIDs with `/`
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
