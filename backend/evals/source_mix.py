"""Source-mix regression evals: the intent router can never strand cross-source corroboration.

The prompt mandates corroboration (check_sanctions + search_entity), but narrowing
the toolset per turn used to drop those tools on a confident classification. These
deterministic checks pin that every narrowed subset, the real binding path, and the
per-intent guidance all keep the corroboration set. An optional `--live` check runs
one real agent turn to confirm a narrowed turn actually crosses sources.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.intent import _CORROBORATION_TOOLS, _GUIDANCE, _INTENT_TOOLS, select_tool_names

Row = tuple[str, str, bool, str]

# The intents whose narrowed subsets used to exclude corroboration tools — the
# exact set the audit flagged as silently breaking the prompt's step 4.
_NARROWED_INTENTS = (
    "identify_entity",
    "profile_entity",
    "ownership_network",
    "sanctions_screening",
    "trade_supply_chain",
)


def source_mix_rows() -> list[Row]:
    """The deterministic corroboration checks (subset, binding path, guidance)."""
    case = "source_mix"
    rows: list[Row] = []

    # (1) Every narrowed subset carries the corroboration set. Checked over ALL
    # non-meta intents (not just the five), so a future intent can't reopen the
    # gap by omission.
    for intent, tools in _INTENT_TOOLS.items():
        if not tools:  # meta intents bind the full toolset
            continue
        missing = [t for t in _CORROBORATION_TOOLS if t not in tools]
        rows.append((
            case,
            f"{intent}_has_corroboration",
            not missing,
            f"missing={missing or 'none'}",
        ))

    # (2) Through the real binding path: a confident classification of each
    # previously-stranded intent must bind a subset containing both tools.
    for intent in _NARROWED_INTENTS:
        bound = select_tool_names({"intent": intent, "confidence": 0.95})
        ok = bound is not None and set(_CORROBORATION_TOOLS).issubset(bound)
        rows.append((
            case,
            f"{intent}_binds_confident",
            ok,
            f"bound={'full set' if bound is None else len(bound)} tools",
        ))

    # (3) The narrowed guidance also names both tools, so the agent is told to
    # corroborate, not just permitted to.
    for intent in _NARROWED_INTENTS:
        g = _GUIDANCE.get(intent, "")
        ok = all(t in g for t in _CORROBORATION_TOOLS)
        rows.append((
            case,
            f"{intent}_guidance_corroborates",
            ok,
            f"names={[t for t in _CORROBORATION_TOOLS if t in g]}",
        ))

    return rows


# --- Optional live check (--live; skipped when creds are absent) -------------

_NON_SAYARI_TOOLS = {
    "check_sanctions",
    "search_entity",
    "get_relationships",
    "get_officers",
    "find_address_connections",
    "find_er_links",
}


def _live_creds_present() -> bool:
    """Whether all creds needed for the optional live turn are set."""
    from app.config import get_settings

    s = get_settings()
    return bool(
        s.anthropic_api_key
        and s.sayari_client_id
        and s.neo4j_uri
        and s.opensanctions_api_key
    )


async def live_source_mix_rows() -> list[Row]:
    """One identify/profile turn on a leak-relevant subject (Roldugin appears in
    the Panama Papers AND is sanctioned) must show at least one non-Sayari tool
    call or non-Sayari source_ref — proof the narrowed turn actually crossed
    sources, not just that it could."""
    case = "source_mix_live"
    if not _live_creds_present():
        return [(case, "skipped", True, "creds absent — deterministic checks only")]

    from app import agent_graph

    out = await agent_graph.evaluate_turn(
        "Profile Sergey Roldugin and tell me about his sanctions and offshore exposure."
    )
    tools = set(out.get("tools_used", []))
    non_sayari_tools = sorted(tools & _NON_SAYARI_TOOLS)

    result = out.get("result") or {}
    non_sayari_refs = sorted({
        ref.get("source")
        for c in result.get("claims", [])
        for ref in c.get("source_refs", [])
        if ref.get("source") in ("icij", "opensanctions")
    })

    ok = bool(non_sayari_tools or non_sayari_refs)
    return [(
        case,
        "non_sayari_source_present",
        ok,
        f"tools={non_sayari_tools or 'none'}, refs={non_sayari_refs or 'none'}",
    )]


def _print_table(rows: list[Row]) -> int:
    """Print the results table and return an exit code (0 if all passed)."""
    total = len(rows)
    passed = sum(int(r[2]) for r in rows)
    print("=" * 78)
    print(f"{'CASE':<18}{'CHECK':<40}{'RESULT':<8}COMMENT")
    print("-" * 78)
    for name, check, ok, comment in rows:
        mark = "PASS" if ok else "FAIL"
        print(f"{name:<18}{check:<40}{mark:<8}{comment}")
    print("=" * 78)
    print(f"\n{passed}/{total} checks passed.")
    return 0 if passed == total else 1


def main() -> None:
    """CLI entry point: run the deterministic checks, plus the live turn with --live."""
    parser = argparse.ArgumentParser(description="Source-mix corroboration evals.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run one live agent turn (needs creds; ~60-90s of API time).",
    )
    args = parser.parse_args()

    rows: list[Row] = source_mix_rows()
    if args.live:
        rows += asyncio.run(live_source_mix_rows())
    sys.exit(_print_table(rows))


if __name__ == "__main__":
    main()
