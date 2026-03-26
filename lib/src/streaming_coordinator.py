"""
Streaming transcription coordinator for hyprwhspr.

Periodically re-transcribes accumulating audio and types new text into the
focused application via wtype as the user speaks.  When whisper revises
earlier text, backspaces erase the stale portion and the correction is retyped
in-place — similar to phone dictation.

If the user presses a physical key or clicks during streaming, corrections are
frozen and only new text is appended (like tapping into text on a phone).
"""

import select
import threading

try:
    from .dependencies import require_package
except ImportError:
    from dependencies import require_package

np = require_package("numpy")
evdev = require_package("evdev")

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


def _is_hallucination(text: str) -> bool:
    normalized = text.lower().replace("_", " ").strip("[]() ")
    return normalized in _HALLUCINATION_MARKERS


class StreamingCoordinator:
    """Coordinates chunked re-transcription with in-place correction.

    Text is typed eagerly on every transcription pass.  If a subsequent pass
    revises earlier output, backspaces erase back to the divergence point and
    the corrected text is retyped — unless the user has interacted with the
    keyboard or mouse, in which case only appending is allowed.
    """

    def __init__(self, whisper_manager, text_injector, audio_capture, config_manager):
        self._whisper = whisper_manager
        self._injector = text_injector
        self._audio = audio_capture
        self._config = config_manager

        self._typed_text = ""
        self._corrections_frozen = False
        self._language_override = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._input_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, language_override=None):
        self._typed_text = ""
        self._corrections_frozen = False
        self._language_override = language_override
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._transcription_loop, daemon=True, name="streaming-transcribe")
        self._thread.start()
        self._input_thread = threading.Thread(
            target=self._monitor_physical_input, daemon=True, name="streaming-input-mon"
        )
        self._input_thread.start()
        print("[STREAMING] Started", flush=True)

    def stop(self) -> str:
        """Stop streaming and return the text that is in the application."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            self._thread = None
        if self._input_thread is not None:
            self._input_thread.join(timeout=2.0)
            self._input_thread = None

        # Final transcription on complete audio for best quality.
        # Skip if corrections are frozen — user edited, our model is stale.
        if not self._corrections_frozen:
            with self._lock:
                audio = self._audio.get_current_audio_copy()
                if audio is not None and len(audio) >= 1600:
                    try:
                        final = self._whisper.transcribe_audio(audio, language_override=self._language_override)
                        if final and final.strip() and not _is_hallucination(final.strip()):
                            self._apply(final.strip())
                    except Exception as e:
                        print(f"[STREAMING] Final transcription error: {e}", flush=True)

        result = self._typed_text
        print(f"[STREAMING] Stopped ({len(result)} chars)", flush=True)
        return result

    # -------------------------------------------------------- input monitor

    def _monitor_physical_input(self):
        """Watch physical input devices for user interaction during streaming.

        On any physical key press or mouse button, freeze corrections so we
        only append from that point forward.
        """
        devices = []
        try:
            for path in evdev.list_devices():
                try:
                    dev = evdev.InputDevice(path)
                    name_lower = dev.name.lower()
                    # Skip virtual keyboards (wtype, ydotool, hyprwhspr)
                    if any(
                        v in name_lower
                        for v in (
                            "virtual",
                            "wtype",
                            "ydotool",
                            "hyprwhspr",
                            "uinput",
                        )
                    ):
                        dev.close()
                        continue
                    caps = dev.capabilities()
                    if evdev.ecodes.EV_KEY in caps:
                        devices.append(dev)
                    else:
                        dev.close()
                except Exception:
                    continue
        except Exception as e:
            print(f"[STREAMING] Could not open input devices: {e}", flush=True)
            return

        if not devices:
            return

        try:
            while not self._stop_event.is_set():
                r, _, _ = select.select(devices, [], [], 0.2)
                for dev in r:
                    try:
                        for event in dev.read():
                            if event.type == evdev.ecodes.EV_KEY and event.value == 1:  # key down
                                self._corrections_frozen = True
                                print("[STREAMING] User input detected — corrections frozen", flush=True)
                                return
                    except Exception:
                        continue
        finally:
            import contextlib

            for dev in devices:
                with contextlib.suppress(Exception):
                    dev.close()

    # ------------------------------------------------------------------ loop

    def _transcription_loop(self):
        chunk_interval = float(self._config.get_setting("streaming_chunk_seconds", 2.0))
        lookback = float(self._config.get_setting("streaming_lookback_seconds", 30.0))
        max_samples = int(lookback * 16000)

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=chunk_interval)
            if self._stop_event.is_set():
                break

            # Once frozen, stop transcribing — our model of on-screen text is stale
            if self._corrections_frozen:
                continue

            audio = self._audio.get_current_audio_copy()
            if audio is None or len(audio) < 1600:
                continue

            if len(audio) > max_samples:
                audio = audio[-max_samples:]

            try:
                result = self._whisper.transcribe_audio(audio, language_override=self._language_override)
            except Exception as e:
                print(f"[STREAMING] Transcription error: {e}", flush=True)
                continue

            if not result or not result.strip():
                continue

            result = result.strip()
            if _is_hallucination(result):
                continue

            with self._lock:
                self._apply(result)

    # --------------------------------------------------------- core logic

    def _apply(self, new_text: str):
        """Diff new_text against typed_text, correct or append as appropriate."""
        old = self._typed_text

        # Find longest common prefix
        common = 0
        limit = min(len(old), len(new_text))
        for i in range(limit):
            if old[i] == new_text[i]:
                common = i + 1
            else:
                break

        chars_to_erase = len(old) - common
        chars_to_type = new_text[common:]

        if chars_to_erase == 0 and not chars_to_type:
            return  # Nothing changed

        if chars_to_erase > 0:
            success = self._injector.send_backspaces(chars_to_erase)
            if not success:
                print("[STREAMING] Backspace failed, freezing corrections", flush=True)
                self._corrections_frozen = True
                return

        if chars_to_type:
            processed = self._injector._apply_word_overrides(chars_to_type)
            processed = self._injector._filter_filler_words(processed)

            if processed:
                wtype_delay = int(self._config.get_setting("streaming_wtype_delay_ms", 0))
                success = self._injector.type_text_direct(processed, delay_ms=wtype_delay)
                if not success:
                    print("[STREAMING] Type failed", flush=True)
                    return

        self._typed_text = new_text
        if chars_to_erase > 0 or chars_to_type:
            frozen_tag = " [frozen]" if self._corrections_frozen else ""
            print(
                f"[STREAMING] {len(self._typed_text)} chars (-{chars_to_erase} +{len(chars_to_type)}){frozen_tag}",
                flush=True,
            )
