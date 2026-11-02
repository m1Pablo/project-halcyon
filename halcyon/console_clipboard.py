"""Clipboard paths owned by the console, never credential inputs."""

from __future__ import annotations

import subprocess
import sys
from typing import Protocol


class ClipboardApp(Protocol):
    clipboard: str

    def copy_to_clipboard(self, text: str) -> None: ...


def copy_text(app: ClipboardApp, text: str) -> None:
    """Use macOS's native clipboard and always retain Textual's fallback."""
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["pbcopy"], input=text.encode("utf-8"), timeout=1, check=True, shell=False
            )
        except (OSError, subprocess.SubprocessError):
            pass
    app.copy_to_clipboard(text)


def paste_text(app: ClipboardApp) -> str:
    """Read once on an explicit paste action, falling back to Textual."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["pbpaste"], capture_output=True, timeout=1, check=True, shell=False
            )
            return result.stdout.decode("utf-8")
        except (OSError, subprocess.SubprocessError, UnicodeError):
            pass
    return app.clipboard
