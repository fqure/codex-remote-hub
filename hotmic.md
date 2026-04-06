# Hot Mic Spec

## Goal

Provide a single-tap `hot mic` mode in the mobile UI that:

- starts speech-to-text capture without using the keyboard
- auto-sends dictated text after a short silence window
- reads back Codex replies aloud with browser TTS
- leaves the text visible in the tmux-backed pane
- makes the mic state visually obvious at all times

This mode must behave as a dedicated feature, not as a variation of the old transient dictation button.

## Current Problems

- The existing mic control is overloaded between normal dictation and hot mic behavior.
- UI state is ambiguous: the button can still appear green, which looks like the old mic mode.
- iOS Safari speech recognition stops after short utterances and does not provide a reliable persistent open mic.
- Multiple selector and event-path changes have been layered on the same control, making behavior brittle.
- Repo edits and installed app edits can diverge because the running service serves `~/.codex-remote-hub/templates/mobile.html`.

## Required UX

- One tap on the hot mic button toggles `hot mic` on.
- One tap on the same button toggles `hot mic` off.
- When `hot mic` is on, the button must be red immediately and remain red even if speech recognition temporarily stops.
- When `hot mic` is off, the button returns to its neutral state.
- There must be no separate green “normal mic” mode on this button.
- Dictated text should appear in the composer as STT arrives.
- After approximately 3 seconds of silence, the composed text should be sent automatically.
- Codex replies should be spoken back using `speechSynthesis`.
- Spoken replies must not remove or replace the visible pane text.

## Platform Constraints

- iOS Safari does not provide a dependable, truly persistent Web Speech microphone session.
- `SpeechRecognition` on Safari can stop after a short utterance even when restarted in code.
- Browser TTS via `speechSynthesis` is acceptable for playback.
- The mic UI must therefore represent `hot mic requested/enabled`, not “the browser is definitely still listening right this millisecond.”

## Implementation Requirements

### 1. Separate hot mic from old mic semantics

- Remove legacy “standard mic” behavior from the mobile mic button.
- Use dedicated naming everywhere:
  - DOM id: `hot-mic-btn`
  - CSS class: `hot-mic-btn`
  - JS handle: `hotMicBtn`
- Do not reuse old `mic-btn` selectors or “listening = green” styling.

### 2. Make visual state depend on `hotMicEnabled`, not STT engine state

- The red visual state must be driven only by `hotMicEnabled`.
- Do not bind button color to `recognitionListening`.
- If Safari pauses recognition and code attempts restart, the button must stay red.

### 3. Use a single event path for activation

- The button must have one activation path for touch and one for keyboard/non-touch fallback.
- Avoid mixed `pointerup` plus `click` toggles that can fire twice on iOS.
- Recommended:
  - `touchend` for touch devices
  - `click` only for keyboard/non-touch activation, gated carefully

### 4. Keep speech recognition restart logic explicit

- `recognition.onend` should attempt restart when `hotMicEnabled` is still true.
- `recognition.onerror` should disable hot mic only for clear permission failures such as:
  - `not-allowed`
  - `service-not-allowed`
- Non-permission end events should not silently clear hot mic UI state.

### 5. Auto-send dictated text

- Maintain a silence timer.
- Every new STT result resets the timer.
- After about 3000 ms without new transcript input:
  - send composer text via `/api/send-text/{session}`
  - send `Enter` via `/api/send-keys/{session}`

### 6. Speak tmux reply text

- Poll `/api/pane/{session}` as already implemented.
- Compute incremental pane text instead of re-speaking the whole pane.
- Sanitize terminal output before speech:
  - strip ANSI
  - collapse repeated whitespace
  - avoid speaking prompt echoes or the just-submitted utterance
- Use `speechSynthesis`, chunking long text before playback.
- While playback is active, temporarily pause mic restart logic if needed to reduce feedback loops.

### 7. Deployed-copy discipline

- The running hub serves templates from `~/.codex-remote-hub/templates/`.
- Any repo edit to `templates/mobile.html` must be copied to:
  - `~/.codex-remote-hub/templates/mobile.html`
- Then restart the installed service:
  - `~/.codex-remote-hub/ctl.sh restart`

## Files To Change

- [templates/mobile.html](/Volumes/WD2TB/Users/fqure/Documents/GitHub/codex-remote-hub/templates/mobile.html)
  - primary UI, CSS, STT, polling, auto-send, TTS logic
- [hotmic.md](/Volumes/WD2TB/Users/fqure/Documents/GitHub/codex-remote-hub/hotmic.md)
  - this specification document

Server changes are not required for the first pass if browser TTS is used.

## Recommended Code Cleanup

- Remove any remaining `toggleMic()` function if it is not used.
- Remove any remaining `.mic-btn` CSS once `hot-mic-btn` is fully in place.
- Remove any green/listening button styles tied to the old mic behavior.
- Keep hot mic state variables explicitly declared:
  - `hotMicEnabled`
  - `hotMicSendTimer`
  - `speechTimer`
  - `speechLastText`
  - `recognitionRestartTimer`
  - `recognitionPausedForSpeech`

## Acceptance Criteria

- The mobile button is visibly red immediately after one tap.
- The mobile button stays red until the user taps it again.
- Dictated text appears in the composer without keyboard interaction.
- Silence auto-sends the dictated text.
- Returned Codex text is spoken aloud using browser TTS.
- The pane still shows the printed reply text.
- A short browser STT interruption does not flip the hot mic button back to a neutral or green state.
- The deployed app and repo copy are kept in sync when testing.

## Known Risk

Even with the correct UI and restart logic, iOS Safari may still prevent a truly persistent microphone session. That is a browser/platform limitation, not a tmux or server limitation. If this limitation blocks the product requirement, the correct next step is to move voice capture out of Safari Web Speech and into a different client architecture.
