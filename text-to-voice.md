# Text-to-Voice Plan

## Goal
Automatically play Codex terminal replies as speech in Safari after each STT input, while still showing reply text in tmux-backed UI.

## Requirements
- Existing flow remains in repo: send user text to tmux via existing API and read pane output.
- No Twilio required for this path.
- Must autoplay audio in Safari only after user action (user tapped Send).
- Preserve existing session management and keyboard features.

## Constraints
- Browser autoplay policies in iOS/Safari require explicit user-gesture initiation.
- `speechSynthesis` cannot reliably queue huge blobs; long responses should be chunked.
- Terminal text includes ANSI control codes that should be cleaned before speaking.
- Terminal output may include prompt re-renders; only incremental diffs should be spoken.

## Architecture
- STT input (existing) -> send as text via existing API (`/api/send-text/{session}`).
- Poll/consume terminal output (`/api/pane/{session}`) and detect new text since last sample.
- Convert new text chunk(s) to speech via Web Speech API (`speechSynthesis`).
- Keep text UI untouched: the same pane snapshot remains visible.

## Implementation steps
1) Client-side TTS utility functions in mobile shell
- Add in `templates/mobile.html`:
  - `stripAnsi()` for terminal escape removal.
  - `sanitizeForSpeech()` to collapse whitespace and remove control text.
  - `splitForSpeech()` with ~1800-2200 char chunking.
  - `speakText()` wrapper using `SpeechSynthesisUtterance`, with stop-on-new-input.

2) Capture and track pane history
- Track `lastPaneText` per session in JS state.
- After each send (and/or on short polling interval), parse `/api/pane/{session}` response.
- Compute diff (`newText = text.slice(lastPaneText.length)` or more robust token-based fallback).
- Update `lastPaneText` only when response is newer and non-empty.

3) Auto-play trigger points
- Trigger speech only in the user Send handler after TMUX paste completes.
- Optionally also trigger in poll callback if a background change is detected and user has not disabled voice.
- Add `userHasInteracted` gate (set true on first Send/gesture) to satisfy Safari autoplay.

4) Voice controls
- Add optional checkbox/lock button: `Voice enabled` + `Read latest`.
- Add `Stop` action to cancel ongoing speech.
- Add per-session memory: do not auto re-read entire pane on page refresh.

5) Speaker quality and readability
- Skip very short/empty deltas.
- Filter prompts like shell prompt lines if duplicated in every poll.
- Replace progress glyphs (`●`, `…`) and remove ANSI color codes.

6) Error handling
- If TTS unsupported (`!('speechSynthesis' in window)`): show inline banner and fallback to text only.
- Catch and log poll errors without breaking UI.

7) Optional future enhancement (post-MVP)
- If later you want real MP3 playback:
  - add server-side TTS route that outputs an audio file from plain text,
  - return URL/token to client,
  - play via `<audio>` element.
- Keep this as phase 2; phase 1 uses native Safari TTS only.

## Suggested milestones
- M0 (15–30 min): add utility functions and `speakText` in `mobile.html`.
- M1 (30–60 min): integrate diff tracking and auto-play on send response.
- M2 (60–90 min): add controls (mute/stop/enable), polish prompt filtering.
- M3 (optional): add MP3 server route and playback fallback.

## Validation checklist
- Send one command, text updates in pane and speech starts immediately.
- Repeated sends do not re-speak already heard text.
- Long outputs are split and all chunks are audible.
- New output while another sample is speaking interrupts previous sample cleanly.
- Safari iOS does not require extra user taps after initial send action.
