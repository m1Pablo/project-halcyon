import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from halcyon.console_clipboard import copy_text, paste_text


@pytest.mark.parametrize(
    "failure", [None, OSError("missing"), subprocess.TimeoutExpired("pbcopy", 1)]
)
def test_native_copy_keeps_textual_fallback(monkeypatch, failure) -> None:
    monkeypatch.setattr("halcyon.console_clipboard.sys.platform", "darwin")
    run = Mock(side_effect=failure)
    monkeypatch.setattr("halcyon.console_clipboard.subprocess.run", run)
    app = SimpleNamespace(copy_to_clipboard=Mock())
    text = "quotes '; $(never execute)\n日本語"
    copy_text(app, text)
    run.assert_called_once_with(["pbcopy"], input=text.encode(), timeout=1, check=True, shell=False)
    app.copy_to_clipboard.assert_called_once_with(text)


def test_native_paste_is_explicit_and_preserves_multiline(monkeypatch) -> None:
    monkeypatch.setattr("halcyon.console_clipboard.sys.platform", "darwin")
    run = Mock(return_value=SimpleNamespace(stdout=b"one\ntwo"))
    monkeypatch.setattr("halcyon.console_clipboard.subprocess.run", run)
    assert paste_text(SimpleNamespace(clipboard="old")) == "one\ntwo"
    run.assert_called_once_with(
        ["pbpaste"], capture_output=True, timeout=1, check=True, shell=False
    )


def test_clipboard_unavailable_falls_back_without_process_on_other_platforms(monkeypatch) -> None:
    monkeypatch.setattr("halcyon.console_clipboard.sys.platform", "linux")
    run = Mock()
    monkeypatch.setattr("halcyon.console_clipboard.subprocess.run", run)
    app = SimpleNamespace(clipboard="fallback", copy_to_clipboard=Mock())
    copy_text(app, "text")
    assert paste_text(app) == "fallback"
    run.assert_not_called()
    monkeypatch.setattr("halcyon.console_clipboard.sys.platform", "darwin")
    run.side_effect = OSError("unavailable")
    assert paste_text(app) == "fallback"
