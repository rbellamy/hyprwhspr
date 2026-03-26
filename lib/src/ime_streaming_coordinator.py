"""
IME-based streaming transcription coordinator for hyprwhspr.

Uses Wayland input-method-v2 preedit/commit instead of wtype keystrokes.
Tentative (in-progress) text is shown as underlined preedit; confirmed
text is committed permanently.  Surrounding-text events let us detect
user edits and adapt rather than freeze.
"""

import re
import threading

try:
    from .dependencies import require_package
except ImportError:
    from dependencies import require_package

np = require_package("numpy")

_HALLUCINATION_MARKERS = frozenset(
    (
        "blank audio",
        "blank",
        "video playback",
        "music",
        "music playing",
        "keyboard clicking",
    )
)

_SENTENCE_END_RE = re.compile(r"[.!?]\s+")


def _is_hallucination(text: str) -> bool:
    normalized = text.lower().replace("_", " ").strip("[]() ")
    return normalized in _HALLUCINATION_MARKERS


def _find_last_sentence_boundary(text: str) -> int:
    """Return the index after the last sentence-ending punctuation+space, or 0."""
    best = 0
    for m in _SENTENCE_END_RE.finditer(text):
        best = m.end()
    return best


class IMEStreamingCoordinator:
    """Streaming coordinator using input-method-v2 preedit/commit.

    Instead of typing characters and backspacing:
    - In-progress transcription is shown as preedit (underlined, tentative)
    - Complete sentences are committed (permanent)
    - User edits are detected via surrounding_text and adapted to
    """

    def __init__(self, whisper_manager, ime_client, text_injector, audio_capture, config_manager):
        self._whisper = whisper_manager
        self._ime = ime_client
        self._injector = text_injector  # for preprocessing (word overrides, filler words)
        self._audio = audio_capture
        self._config = config_manager

        self._committed_text = ""  # text sent via commit_string (permanent)
        self._preedit_text = ""  # text currently shown as preedit (tentative)
        self._language_override = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, language_override=None):
        self._committed_text = ""
        self._preedit_text = ""
        self._language_override = language_override
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._transcription_loop, daemon=True, name="ime-streaming")
        self._thread.start()
        print("[IME-STREAM] Started", flush=True)

    def stop(self) -> str:
        """Stop streaming, commit remaining preedit, return full text."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None

        with self._lock:
            # Final transcription for best quality
            audio = self._audio.get_current_audio_copy()
            if audio is not None and len(audio) >= 1600:
                try:
                    final = self._whisper.transcribe_audio(audio, language_override=self._language_override)
                    if final and final.strip() and not _is_hallucination(final.strip()):
                        self._apply(final.strip())
                except Exception as e:
                    print(f"[IME-STREAM] Final transcription error: {e}", flush=True)

            # Commit any remaining preedit
            if self._preedit_text:
                processed = self._preprocess(self._preedit_text)
                if processed:
                    self._ime.commit_text(processed)
                self._committed_text += self._preedit_text
                self._preedit_text = ""

        result = self._committed_text
        print(f"[IME-STREAM] Stopped ({len(result)} chars committed)", flush=True)
        return result

    # ------------------------------------------------------------------ loop

    def _transcription_loop(self):
        chunk_interval = float(self._config.get_setting("streaming_chunk_seconds", 2.0))
        lookback = float(self._config.get_setting("streaming_lookback_seconds", 30.0))
        max_samples = int(lookback * 16000)

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=chunk_interval)
            if self._stop_event.is_set():
                break

            # Skip if IME is not active (no text field focused)
            if not self._ime.is_active():
                continue

            audio = self._audio.get_current_audio_copy()
            if audio is None or len(audio) < 1600:
                continue

            if len(audio) > max_samples:
                audio = audio[-max_samples:]

            try:
                result = self._whisper.transcribe_audio(audio, language_override=self._language_override)
            except Exception as e:
                print(f"[IME-STREAM] Transcription error: {e}", flush=True)
                continue

            if not result or not result.strip():
                continue

            result = result.strip()
            if _is_hallucination(result):
                continue

            with self._lock:
                self._apply(result)

    # ----------------------------------------------------------- core logic

    def _apply(self, full_text: str):
        """Update preedit/commit based on new transcription result.

        Strategy:
        - Everything up to the last sentence boundary in the new portion → commit
        - The trailing fragment → preedit (tentative, will be revised)
        """
        # Determine the new portion relative to committed text
        if full_text.startswith(self._committed_text):
            # Committed text is a prefix of new transcription — normal case
            new_portion = full_text[len(self._committed_text) :]
        elif len(full_text) > len(self._committed_text):
            # Whisper revised into committed text territory.
            # We can't un-commit, so take everything past committed length.
            new_portion = full_text[len(self._committed_text) :]
            print(
                f"[IME-STREAM] Revision into committed text "
                f"(committed {len(self._committed_text)}, new {len(full_text)})",
                flush=True,
            )
        else:
            # New text is shorter — whisper trimmed; keep current state
            return

        if not new_portion:
            # Clear preedit if nothing new
            if self._preedit_text:
                self._ime.set_preedit("")
                self._preedit_text = ""
            return

        # Split new_portion at sentence boundary
        boundary = _find_last_sentence_boundary(new_portion)

        if boundary > 0:
            to_commit = new_portion[:boundary]
            to_preedit = new_portion[boundary:]

            # Commit the stable portion
            processed_commit = self._preprocess(to_commit)
            processed_preedit = self._preprocess(to_preedit)

            if processed_commit:
                self._ime.set_preedit_and_commit(processed_commit, processed_preedit)
            elif processed_preedit != self._preprocess(self._preedit_text):
                self._ime.set_preedit(processed_preedit)

            self._committed_text += to_commit
            self._preedit_text = to_preedit
        else:
            # No sentence boundary — everything stays as preedit
            processed = self._preprocess(new_portion)
            if processed != self._preprocess(self._preedit_text):
                self._ime.set_preedit(processed)
            self._preedit_text = new_portion

        print(
            f"[IME-STREAM] committed={len(self._committed_text)} preedit={len(self._preedit_text)}",
            flush=True,
        )

    # --------------------------------------------------------- preprocessing

    def _preprocess(self, text: str) -> str:
        """Light preprocessing: word overrides and filler word removal."""
        if not text:
            return text
        processed = self._injector._apply_word_overrides(text)
        processed = self._injector._filter_filler_words(processed)
        return processed
