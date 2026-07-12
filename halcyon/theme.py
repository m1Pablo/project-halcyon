"""Semantic Textual theme for Halcyon's performance cockpit."""

from __future__ import annotations

from typing import Final

from textual.theme import Theme

HALCYON_THEME: Final[Theme] = Theme(
    name="halcyon",
    primary="#78dce8",
    secondary="#a9dc76",
    warning="#ffd866",
    error="#ff6188",
    success="#a9dc76",
    accent="#ab9df2",
    foreground="#fcfcfa",
    background="#090b10",
    surface="#11151d",
    panel="#171c26",
    boost="#232b38",
    dark=True,
    variables={
        "halcyon-muted": "#8b95a7",
        "halcyon-line": "#2c3545",
        "halcyon-midi": "#a9dc76",
        "halcyon-target": "#78dce8",
        "halcyon-viz": "#ab9df2",
        "halcyon-active": "#ffd866",
        "halcyon-press": "#ff9f43",
        "halcyon-idle": "#596275",
        "footer-background": "#11151d",
        "footer-key-foreground": "#ffd866",
        "footer-description-foreground": "#8b95a7",
        "border": "#2c3545",
        "border-blurred": "#202735",
        "input-selection-background": "#39445a",
    },
)

__all__ = ["HALCYON_THEME"]
