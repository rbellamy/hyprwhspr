"""
Wayland input-method-v2 client for hyprwhspr.

Connects to the compositor, binds zwp_input_method_manager_v2, and provides
a high-level API for preedit/commit text operations.  Runs the Wayland event
loop in a dedicated daemon thread.
"""

import select

# Generated bindings (from scripts/generate-ime-bindings.sh)
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from pywayland.client import Display
from pywayland.protocol.wayland import WlSeat

sys.path.insert(0, str(Path(__file__).parent))
from ime_protocol.input_method_unstable_v2 import (
    ZwpInputMethodManagerV2,
    ZwpInputMethodV2,
)


class IMEClient:
    """Wayland input-method-v2 client.

    Only one input method can be bound per seat.  Create the client when
    recording starts and destroy it when recording stops so other IMEs
    (fcitx5, ibus) are only interrupted during active dictation.
    """

    def __init__(self):
        self._display: Display | None = None
        self._seat = None
        self._im_manager = None
        self._im: ZwpInputMethodV2 | None = None

        # Protocol state (updated by event handlers under _lock)
        self._active = False
        self._serial = 0
        self._surrounding_text = ""
        self._surrounding_cursor = 0
        self._surrounding_anchor = 0
        self._content_hint = 0
        self._content_purpose = 0
        self._text_change_cause = 0
        self._unavailable = False

        # Threading
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Optional callbacks
        self.on_activate: Callable | None = None
        self.on_deactivate: Callable | None = None
        self.on_surrounding_text: Callable | None = None

    # ----------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Connect to compositor and start event loop.

        Returns True if input-method-v2 is available, False otherwise.
        """
        try:
            self._display = Display()
            self._display.connect()
        except Exception as e:
            print(f"[IME] Failed to connect to Wayland display: {e}", flush=True)
            return False

        registry = self._display.get_registry()
        registry.dispatcher["global"] = self._on_global

        self._display.dispatch(block=True)
        self._display.roundtrip()

        if self._im_manager is None:
            print("[IME] zwp_input_method_manager_v2 not available", flush=True)
            self._display.disconnect()
            self._display = None
            return False

        if self._seat is None:
            print("[IME] No wl_seat found", flush=True)
            self._display.disconnect()
            self._display = None
            return False

        # Bind input method to seat
        self._im = self._im_manager.get_input_method(self._seat)
        self._im.dispatcher["activate"] = self._on_activate
        self._im.dispatcher["deactivate"] = self._on_deactivate
        self._im.dispatcher["surrounding_text"] = self._on_surrounding_text
        self._im.dispatcher["text_change_cause"] = self._on_text_change_cause
        self._im.dispatcher["content_type"] = self._on_content_type
        self._im.dispatcher["done"] = self._on_done
        self._im.dispatcher["unavailable"] = self._on_unavailable

        self._display.roundtrip()

        # Start event loop thread
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._event_loop, daemon=True, name="ime-wayland")
        self._thread.start()

        print("[IME] Connected to compositor", flush=True)
        return True

    def stop(self):
        """Disconnect from compositor and clean up."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

        try:
            if self._im is not None:
                self._im.destroy()
                self._im = None
            if self._im_manager is not None:
                self._im_manager.destroy()
                self._im_manager = None
            if self._display is not None:
                self._display.flush()
                self._display.disconnect()
                self._display = None
        except Exception as e:
            print(f"[IME] Cleanup error: {e}", flush=True)

        self._active = False
        print("[IME] Disconnected", flush=True)

    # ------------------------------------------------------------- state

    def is_active(self) -> bool:
        """True if a text-input-v3 client has an active text field."""
        with self._lock:
            return self._active and not self._unavailable

    def is_unavailable(self) -> bool:
        """True if another IME took over the seat."""
        with self._lock:
            return self._unavailable

    def get_surrounding(self) -> tuple:
        """Return (text, cursor_byte_offset, anchor_byte_offset)."""
        with self._lock:
            return (
                self._surrounding_text,
                self._surrounding_cursor,
                self._surrounding_anchor,
            )

    def get_text_change_cause(self) -> int:
        """Return the cause of the last text change (0=other, 1=input_method)."""
        with self._lock:
            return self._text_change_cause

    # ---------------------------------------------------------- requests

    def set_preedit(self, text: str) -> None:
        """Set preedit (tentative/underlined) text at cursor position."""
        with self._lock:
            if self._im is None or not self._active:
                return
            encoded = text.encode("utf-8")
            self._im.set_preedit_string(text, 0, len(encoded))
            self._im.commit(self._serial)
            self._display.flush()

    def commit_text(self, text: str) -> None:
        """Finalize text — clears any preedit and inserts permanently."""
        with self._lock:
            if self._im is None or not self._active:
                return
            self._im.set_preedit_string("", 0, 0)
            self._im.commit_string(text)
            self._im.commit(self._serial)
            self._display.flush()

    def delete_surrounding(self, before_bytes: int, after_bytes: int) -> None:
        """Delete text around cursor (byte counts in UTF-8)."""
        with self._lock:
            if self._im is None or not self._active:
                return
            self._im.delete_surrounding_text(before_bytes, after_bytes)
            self._im.commit(self._serial)
            self._display.flush()

    def set_preedit_and_commit(self, commit: str, preedit: str) -> None:
        """Atomically commit finalized text and set new preedit in one operation."""
        with self._lock:
            if self._im is None or not self._active:
                return
            self._im.commit_string(commit)
            encoded = preedit.encode("utf-8")
            self._im.set_preedit_string(preedit, 0, len(encoded))
            self._im.commit(self._serial)
            self._display.flush()

    # --------------------------------------------------- registry handler

    def _on_global(self, registry, id_num, iface_name, version):
        if iface_name == "wl_seat" and self._seat is None:
            self._seat = registry.bind(id_num, WlSeat, min(version, 8))
        elif iface_name == "zwp_input_method_manager_v2":
            self._im_manager = registry.bind(id_num, ZwpInputMethodManagerV2, 1)

    # ---------------------------------------------------- event handlers

    def _on_activate(self, im):
        with self._lock:
            self._active = True
            # Reset state per protocol spec
            self._surrounding_text = ""
            self._surrounding_cursor = 0
            self._surrounding_anchor = 0
            self._text_change_cause = 0
        if self.on_activate:
            self.on_activate()
        print("[IME] Activated (text field focused)", flush=True)

    def _on_deactivate(self, im):
        with self._lock:
            self._active = False
        if self.on_deactivate:
            self.on_deactivate()
        print("[IME] Deactivated", flush=True)

    def _on_surrounding_text(self, im, text, cursor, anchor):
        with self._lock:
            self._surrounding_text = text
            self._surrounding_cursor = cursor
            self._surrounding_anchor = anchor
        if self.on_surrounding_text:
            self.on_surrounding_text(text, cursor, anchor)

    def _on_text_change_cause(self, im, cause):
        with self._lock:
            self._text_change_cause = cause

    def _on_content_type(self, im, hint, purpose):
        with self._lock:
            self._content_hint = hint
            self._content_purpose = purpose

    def _on_done(self, im):
        with self._lock:
            self._serial += 1

    def _on_unavailable(self, im):
        with self._lock:
            self._unavailable = True
            self._active = False
        print("[IME] Unavailable (another IME took over)", flush=True)

    # --------------------------------------------------------- event loop

    def _event_loop(self):
        """Run Wayland dispatch loop until stopped."""
        while not self._stop_event.is_set():
            try:
                if self._display is None:
                    break
                self._display.flush()
                fd = self._display.get_fd()
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    self._display.dispatch(block=False)
            except Exception as e:
                if not self._stop_event.is_set():
                    print(f"[IME] Event loop error: {e}", flush=True)
                break
