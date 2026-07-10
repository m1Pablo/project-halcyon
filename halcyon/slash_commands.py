"""Slash-command parsing and lookup derived from Halcyon's menu registry."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import get_close_matches
import shlex
from typing import Any, Iterable, Literal


@dataclass(frozen=True, slots=True)
class SlashNode:
    label: str
    shortcut: str
    description: str
    payload: dict[str, Any] | None
    children: dict[str, "SlashNode"]


@dataclass(frozen=True, slots=True)
class SlashResolved:
    status: Literal["leaf", "group", "error"]
    command: str
    leaf_command: str = ""
    payload: dict[str, Any] | None = None
    arguments: tuple[str, ...] = ()
    message: str = ""
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SlashCompletion:
    remainder: str
    completed_token: str
    append_space: bool


def _normalize_token(token: str) -> str:
    return token.strip().lower()


def _split_command_parts(command: str) -> list[str]:
    raw = command.strip()
    if not raw.startswith("/"):
        return []
    raw = raw[1:].strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def parse_slash_command(command: str) -> list[str]:
    """Return normalized slash-command tokens, including any argument tokens."""

    return [_normalize_token(part) for part in _split_command_parts(command)]


def iter_slash_leaf_commands(
    menu_def: dict[str, Any],
) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield every executable slash command from the menu definition."""

    for option_name, option in menu_def.items():
        if str(option_name).startswith("__"):
            continue
        option_shortcut = _normalize_token(option.get("shortcut") or option_name)
        for item in option.get("items") or []:
            item_shortcut = _normalize_token(item.get("shortcut") or item.get("name", ""))
            children = item.get("children") or []
            if children:
                for child in children:
                    child_shortcut = _normalize_token(
                        child.get("shortcut") or child.get("name", "")
                    )
                    yield (f"/{option_shortcut} {item_shortcut} {child_shortcut}", child)
                continue
            yield (f"/{option_shortcut} {item_shortcut}", item)

    command_bucket = menu_def.get("__slash_commands__", {})
    command_items = command_bucket.get("items") if isinstance(command_bucket, dict) else None
    if isinstance(command_items, list):
        for command in command_items:
            shortcut = _normalize_token(str(command.get("shortcut", "")))
            if shortcut:
                yield (f"/{shortcut}", command)


def build_slash_tree(menu_def: dict[str, Any]) -> dict[str, SlashNode]:
    """Build a slash-command lookup tree from a menu definition."""

    root: dict[str, SlashNode] = {}
    for option_name, option in menu_def.items():
        if str(option_name).startswith("__"):
            continue
        option_shortcut = _normalize_token(option.get("shortcut") or option_name)
        children: dict[str, SlashNode] = {}
        for item in option.get("items") or []:
            item_shortcut = _normalize_token(item.get("shortcut") or item.get("name", ""))
            item_children: dict[str, SlashNode] = {}
            nested_items = item.get("children") or []
            if nested_items:
                for child in nested_items:
                    child_shortcut = _normalize_token(
                        child.get("shortcut") or child.get("name", "")
                    )
                    item_children[child_shortcut] = SlashNode(
                        label=str(child.get("name", child_shortcut)),
                        shortcut=child_shortcut,
                        description=str(child.get("description", "")),
                        payload=child,
                        children={},
                    )
                payload = None
            else:
                payload = item
            children[item_shortcut] = SlashNode(
                label=str(item.get("name", item_shortcut)),
                shortcut=item_shortcut,
                description=str(item.get("description", "")),
                payload=payload,
                children=item_children,
            )
        root[option_shortcut] = SlashNode(
            label=str(option_name),
            shortcut=option_shortcut,
            description=str(option.get("description", "")),
            payload=None,
            children=children,
        )

    command_bucket = menu_def.get("__slash_commands__", {})
    command_items = command_bucket.get("items") if isinstance(command_bucket, dict) else None
    if isinstance(command_items, list):
        for command in command_items:
            shortcut = _normalize_token(str(command.get("shortcut", "")))
            if not shortcut:
                continue
            root[shortcut] = SlashNode(
                label=str(command.get("name", shortcut)),
                shortcut=shortcut,
                description=str(command.get("description", "")),
                payload=command,
                children={},
            )
    return root


def _suggest_from_candidates(candidates: Iterable[str], prefix: str) -> str | None:
    candidates_list = sorted(candidates)
    prefix = _normalize_token(prefix)
    if not prefix:
        return None
    prefix_matches = [candidate for candidate in candidates_list if candidate.startswith(prefix)]
    if prefix_matches:
        return sorted(prefix_matches, key=lambda value: (len(value), value))[0]
    close = get_close_matches(prefix, candidates_list, n=1, cutoff=0.75)
    return close[0] if close else None


def resolve_slash_command(menu_def: dict[str, Any], command: str) -> SlashResolved:
    """Resolve a slash command to a group, executable leaf, or error."""

    raw = command.strip()
    parts = _split_command_parts(raw)
    if not parts:
        return SlashResolved(
            status="error",
            command=raw,
            message="Missing slash command. Try `/synth`, `/preset`, or `/panic`.",
        )

    tree = build_slash_tree(menu_def)
    current_level = tree
    node: SlashNode | None = None
    path_tokens: list[str] = []
    arguments: tuple[str, ...] = ()

    for index, raw_part in enumerate(parts):
        token = _normalize_token(raw_part)
        next_node = current_level.get(token)
        if next_node is not None:
            node = next_node
            path_tokens.append(token)
            current_level = node.children
            continue

        if node is not None and node.payload is not None and not node.children:
            arguments = tuple(parts[index:])
            break

        candidates = tuple(sorted(current_level.keys()))
        suggestion = _suggest_from_candidates(candidates, token)
        if index == 0:
            message = f"Unknown command: `/{token}`"
            if suggestion:
                message += f". Did you mean `/{suggestion}`?"
        else:
            message = f"Unknown command segment: `{token}`"
            if suggestion:
                message += f". Did you mean `{suggestion}`?"
        return SlashResolved(status="error", command=raw, message=message)

    if node is None:
        return SlashResolved(status="error", command=raw, message="Could not resolve command.")

    leaf_command = "/" + " ".join(path_tokens)
    if node.payload is not None:
        return SlashResolved(
            status="leaf",
            command=raw,
            leaf_command=leaf_command,
            payload=node.payload,
            arguments=arguments,
        )

    return SlashResolved(
        status="group",
        command=raw,
        leaf_command=leaf_command,
        message=f"Available under `{leaf_command}`:",
        options=tuple((child.shortcut, child.label) for child in node.children.values()),
    )


def suggest_slash_completion(menu_def: dict[str, Any], command: str) -> SlashCompletion | None:
    """Return a completion suffix for a partial slash command."""

    if not command.startswith("/") or "\n" in command:
        return None

    after = command[1:]
    ends_with_space = bool(after) and after[-1].isspace()
    parts = [part for part in after.split() if part.strip()]
    if ends_with_space:
        path_tokens = [_normalize_token(part) for part in parts]
        prefix = ""
    else:
        path_tokens = [_normalize_token(part) for part in parts[:-1]]
        prefix = _normalize_token(parts[-1]) if parts else ""

    tree = build_slash_tree(menu_def)
    level = tree
    for token in path_tokens:
        node = level.get(token)
        if node is None or node.payload is not None:
            return None
        level = node.children

    if not level:
        return None

    if prefix:
        suggestion = _suggest_from_candidates(level.keys(), prefix)
    else:
        suggestion = sorted(level.keys())[0]
    if not suggestion or (prefix and not suggestion.startswith(prefix)):
        return None

    remainder = suggestion[len(prefix) :] if prefix else suggestion
    if not remainder:
        return None

    node = level.get(suggestion)
    append_space = bool(node and node.children)
    return SlashCompletion(
        remainder=remainder,
        completed_token=suggestion,
        append_space=append_space,
    )
