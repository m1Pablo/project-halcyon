"""Menu XML loader for Halcyon's slash-command ABI."""

from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET
from typing import Any

_SHORTCUT_SANITIZE_RE = re.compile(r"[^a-z0-9]+")


def _derive_shortcut(name: str) -> str:
    slug = _SHORTCUT_SANITIZE_RE.sub("_", name.strip().lower()).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "cmd"


def _resolve_menu_path(filename: str | Path) -> Path:
    path = Path(filename)
    if path.is_absolute() and path.exists():
        return path
    package_path = Path(__file__).resolve().parent / path
    if package_path.exists():
        return package_path
    return path


def _coerce_bool_attr(raw: str | None) -> bool | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _extract_mcp_attrs(element: ET.Element) -> dict[str, Any]:
    attrs: dict[str, Any] = {}

    single_flight = _coerce_bool_attr(element.attrib.get("mcp_single_flight"))
    if single_flight is not None:
        attrs["mcp_single_flight"] = single_flight

    parallel_ok = _coerce_bool_attr(element.attrib.get("mcp_parallel_ok"))
    if parallel_ok is not None:
        attrs["mcp_parallel_ok"] = parallel_ok

    concurrency_group = element.attrib.get("mcp_concurrency_group", "").strip()
    if concurrency_group:
        attrs["mcp_concurrency_group"] = concurrency_group

    for attr_name in ("mcp_poll_after_s", "mcp_expected_duration_s"):
        raw_value = element.attrib.get(attr_name, "").strip()
        if not raw_value:
            continue
        try:
            attrs[attr_name] = int(float(raw_value))
        except ValueError:
            continue

    return attrs


def _leaf_payload(element: ET.Element, *, inherited_tool: str | None = None) -> dict[str, Any]:
    name = element.attrib.get("name", "").strip()
    if not name:
        raise ValueError(f"<{element.tag}> leaf requires name")
    shortcut = element.attrib.get("shortcut") or _derive_shortcut(name)
    tool = (element.attrib.get("tool") or inherited_tool or "").strip()
    if not tool:
        raise ValueError(f"<{element.tag}> leaf {name!r} requires tool")
    return {
        "name": name,
        "shortcut": shortcut,
        "tool": tool,
        "description": element.attrib.get("description", ""),
        "usage": element.attrib.get("usage", ""),
        **_extract_mcp_attrs(element),
    }


def load_menu_from_xml(filename: str | Path = "menu.xml") -> dict[str, dict[str, Any]]:
    """Load a Halcyon slash-command menu definition from XML."""

    path = _resolve_menu_path(filename)
    root = ET.parse(path).getroot()
    if root.tag != "menu":
        raise ValueError("Root element must be <menu>")

    commands: list[dict[str, Any]] = []
    for command in root.findall("command"):
        commands.append(_leaf_payload(command))

    menu_def: dict[str, dict[str, Any]] = {}
    for option in root.findall("option"):
        option_name = option.attrib.get("name", "").strip()
        if not option_name:
            raise ValueError("<option> requires name")
        option_shortcut = option.attrib.get("shortcut") or _derive_shortcut(option_name)
        items: list[dict[str, Any]] = []

        for suboption in option.findall("suboption"):
            name = suboption.attrib.get("name", "").strip()
            if not name:
                raise ValueError("<suboption> requires name")

            children = suboption.findall("suboption_suboption")
            if children:
                child_items = [
                    _leaf_payload(child, inherited_tool=suboption.attrib.get("tool"))
                    for child in children
                ]
                items.append(
                    {
                        "name": name,
                        "shortcut": suboption.attrib.get("shortcut") or _derive_shortcut(name),
                        "description": suboption.attrib.get("description", ""),
                        "children": child_items,
                    }
                )
                continue

            items.append(_leaf_payload(suboption))

        menu_def[option_name] = {
            "description": option.attrib.get("description", ""),
            "shortcut": option_shortcut,
            "items": items,
        }

    if commands:
        menu_def["__slash_commands__"] = {
            "description": "Slash-only commands",
            "shortcut": "__slash_commands__",
            "items": commands,
        }

    return menu_def


def load_menu(filename: str | Path = "menu.xml") -> dict[str, dict[str, Any]]:
    """Load the default Halcyon command menu."""

    return load_menu_from_xml(filename)
