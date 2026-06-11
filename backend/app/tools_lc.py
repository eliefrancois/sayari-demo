"""LangChain tool adapter.

`ChatAnthropic.bind_tools()` accepts several shapes; we feed it OpenAI-style
function dicts because that's the most explicit and the easiest to eyeball
against what the native loop sends Anthropic. This module is a PURE ADAPTER —
the single source of truth for every tool (name, description, JSON schema)
stays in `tools.py` (the 6 investigation tools) and `prompts.py` (the two
terminators). If a descriptor changes there, it changes here for free.

Conversion: Anthropic's tool shape is
    {"name", "description", "input_schema": <json-schema>}
OpenAI's function shape is
    {"type": "function", "function": {"name", "description", "parameters": <json-schema>}}
The JSON schema body is identical, so this is a one-line wrap.
"""

from __future__ import annotations

from typing import Any

from app.prompts import SUBMIT_ANSWER_TOOL, SUBMIT_SUMMARY_TOOL
from app.tools import TOOLS


def _to_openai_function(tool: dict[str, Any]) -> dict[str, Any]:
    """Wrap an Anthropic-format tool descriptor as an OpenAI function tool."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }


# The investigation tools (6 ICIJ/OpenSanctions + 9 Sayari + 2 recall).
INVESTIGATION_TOOLS_LC: list[dict[str, Any]] = [_to_openai_function(t) for t in TOOLS]

# The two terminators.
SUBMIT_SUMMARY_TOOL_LC = _to_openai_function(SUBMIT_SUMMARY_TOOL)
SUBMIT_ANSWER_TOOL_LC = _to_openai_function(SUBMIT_ANSWER_TOOL)

# Everything bound to the model on a turn: all investigation tools + 2 terminators.
ALL_TOOLS_LC: list[dict[str, Any]] = INVESTIGATION_TOOLS_LC + [
    SUBMIT_SUMMARY_TOOL_LC,
    SUBMIT_ANSWER_TOOL_LC,
]

# Tool names that END a turn (their args ARE the structured output).
TERMINATOR_NAMES: frozenset[str] = frozenset({"submit_summary", "submit_answer"})


def _anthropic_cached(tool: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI-format function tool re-expressed in ANTHROPIC-native shape with
    an ephemeral cache breakpoint. langchain-anthropic passes a dict that already
    has {name, description, input_schema} through verbatim (preserving
    cache_control), whereas its OpenAI->Anthropic converter drops unknown keys —
    so the breakpoint tool must be emitted in native form to actually cache."""
    fn = tool["function"]
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "input_schema": fn["parameters"],
        "cache_control": {"type": "ephemeral"},
    }


def tools_for(tool_names: set[str] | None, *, cache: bool = False) -> list[dict[str, Any]]:
    """The LangChain tool list to bind for a turn. `tool_names` is the intent
    router's selected INVESTIGATION-tool subset; the two terminators are ALWAYS
    appended so the agent can always finish. None (or empty) -> the full toolset
    (the safe fallback for low-confidence classification).

    With cache=True the LAST tool (always a stable terminator) carries an
    ephemeral cache breakpoint so the whole tool-definitions block is cached
    across turns. The breakpoint tool is emitted in Anthropic-native form so
    cache_control survives langchain-anthropic's tool conversion."""
    if not tool_names:
        base = list(ALL_TOOLS_LC)
    else:
        selected = [
            t for t in INVESTIGATION_TOOLS_LC if t["function"]["name"] in tool_names
        ]
        base = selected + [SUBMIT_SUMMARY_TOOL_LC, SUBMIT_ANSWER_TOOL_LC]
    if not cache or not base:
        return base
    *head, last = base
    return [*head, _anthropic_cached(last)]
