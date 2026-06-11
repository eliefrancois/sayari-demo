"""LangSmith evaluation runner for the Entity Risk Resolver agent.

Pulls the live `sayari-demo` golden dataset from LangSmith, runs each example's
`inputs.message` through the REAL agent turn (`agent_graph.evaluate_turn`), and
scores the structured terminator output with reference-based evaluators that
grade against the example `outputs`. Adds an optional Anthropic LLM judge
(faithfulness / coverage vs `reference_answer`) plus two standalone extra evals
(recall-over-distance and a token-budget guardrail).

Reference-based evaluators (score vs example.outputs):
  - terminator_kind_match    expected_kind (answer vs summary) == which terminator fired
  - found_match              found bool matches (derived for answer turns)
  - report_ready_match       report_ready matches (summary => report produced)
  - sanctions_status_match   resolved sanctions label matches, respecting labeling
                             discipline (sanctioned / not_sanctioned / non_sdn /
                             name_collision dispatch to the right structured check)
  - expected_entities_recall fraction of expected_entities present in the answer
  - must_not_absent          structural guards: no false sanctions/SDN promotion,
                             no fabricated claims on not-found/clarify rows

Per-example `metadata.checks` are ALSO scored by reusing the exact check logic in
`run_evals.EVALUATORS` (each self-skips the rows it doesn't apply to), so the
LangSmith grid mirrors the local regression suite.

Modes (dry-run is the default and spends NO credits):
  # dry-run: load the dataset, validate evaluators are well-formed against a real
  # reference example, and run the two deterministic extra evals. Proves wiring.
  .venv/bin/python -m evals.langsmith_eval

  # full live run uploaded to LangSmith (hits the live agent; spends Anthropic):
  .venv/bin/python -m evals.langsmith_eval --live

  # add the Anthropic LLM judge (faithfulness/coverage vs reference_answer):
  .venv/bin/python -m evals.langsmith_eval --live --judge

  # cheap smoke: one live example, no judge:
  .venv/bin/python -m evals.langsmith_eval --live --limit 1

Env (names only; never printed/logged):
  LANGCHAIN_API_KEY    required (dataset pull + experiment upload)
  ANTHROPIC_API_KEY    required only for --live and --judge
  LANGCHAIN_PROJECT    optional (defaults to entity-risk-resolver)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Callable

from app import agent_graph, conversations
from app.agent_common import (
    ALLOWED_MODELS,
    DEFAULT_MODEL,
    MAX_TOKENS_PER_TURN,
    MODEL,
    build_context_block,
    build_followup_prefetch,
)
from app.config import apply_langsmith_env, get_settings
from app.schema import Claim, SanctionsHit, SourceRef, TurnAnswer
from app.tools import recall_state_tool
from evals import multiturn, run_evals

DATASET_ID = "25e331c3-42aa-40db-8096-3f5ff3c392d5"
DATASET_NAME = "sayari-demo"

# The judge reuses the project's primary configured model (the same Sonnet
# snapshot the agent runs on), so the faithfulness grade reflects the deployed
# model family. Guarded behind --judge so it never spends credits implicitly.
_JUDGE_MODEL = MODEL

# A row is (case, check, passed, comment) — the same shape run_evals prints.
Row = tuple[str, str, bool, str]

# The main-agent model for this run, set from --model in main(). None = the
# default Sonnet 4.5. aevaluate calls `target` with only `inputs`, so the model
# is carried here rather than as a parameter.
_RUN_MODEL: str | None = None


def _model_tag(model_id: str) -> str:
    """Derive a short, stable experiment tag from a full Anthropic model id so
    each model's run shows up as a clearly-named experiment under the dataset:
      claude-sonnet-4-5-20250929 -> sonnet-4-5
      claude-haiku-4-5-20251001  -> haiku-4-5
      claude-3-7-sonnet-20250219 -> sonnet-3-7
    Drops the `claude` prefix and the trailing YYYYMMDD snapshot, then orders the
    family word ahead of its version numbers regardless of source ordering."""
    parts = [p for p in model_id.split("-") if p != "claude"]
    if parts and parts[-1].isdigit() and len(parts[-1]) == 8:
        parts = parts[:-1]
    family = next((p for p in parts if p in ("sonnet", "haiku", "opus")), None)
    nums = [p for p in parts if p.isdigit()]
    if family and nums:
        return "-".join([family, *nums])
    return "-".join(parts) if parts else "model"


# --- Normalized target ------------------------------------------------------


def _result(out: dict[str, Any]) -> dict[str, Any]:
    return out.get("result") or {}


def _answer_blob(out: dict[str, Any]) -> str:
    """All free-text the agent surfaced this turn, for entity recall + judging."""
    r = _result(out)
    parts: list[str] = [
        r.get("answer") or "",
        r.get("investigation_summary") or "",
        r.get("entity_name") or "",
    ]
    parts += [c.get("text") or "" for c in r.get("claims", [])]
    for f in r.get("sayari_risk_factors") or []:
        parts.append(str(f.get("name") or ""))
        parts.append(str(f.get("value") or ""))
    for h in r.get("sanctions_hits", []):
        parts.append(str(h.get("matched_name") or ""))
    return " ".join(p for p in parts if p)


def _derive_found(out: dict[str, Any]) -> bool:
    """`found` for the golden rows. RiskSummary carries it directly; a TurnAnswer
    does not, so derive it: a pure clarification turn or a no-evidence answer is
    NOT found, otherwise found when the turn produced any substantive evidence."""
    r = _result(out)
    if out.get("kind") == "summary":
        return bool(r.get("found"))
    if r.get("clarification_questions") and not r.get("claims"):
        return False
    resolved, _ = run_evals.resolved_subject(out)
    return bool(
        r.get("claims")
        or r.get("sayari_risk_factors")
        or r.get("sanctions_hits")
        or r.get("referenced_node_ids")
        or resolved
    )


def _derive_report_ready(out: dict[str, Any]) -> bool:
    """A summary turn IS the formal report, so report_ready is implicitly true;
    an answer turn carries the explicit guarded affordance flag."""
    if out.get("kind") == "summary":
        return True
    return bool(_result(out).get("report_ready"))


async def target(inputs: dict[str, Any]) -> dict[str, Any]:
    """Run one golden `inputs.message` through the real agent turn and return the
    structured terminator output in a normalized dict. Keeps `kind` / `result` /
    `tools_used` (so run_evals' checks read it unchanged) and adds the derived
    top-level fields the reference evaluators grade on."""
    out = await agent_graph.evaluate_turn(inputs["message"], model=_RUN_MODEL)
    return {
        **out,
        "found": _derive_found(out),
        "report_ready": _derive_report_ready(out),
        "answer_text": _answer_blob(out),
    }


# --- Reference-based evaluators (graded vs example.outputs) -----------------


def _ref(example: Any) -> dict[str, Any]:
    return (getattr(example, "outputs", None) or {})


def terminator_kind_match(outputs: dict[str, Any], example: Any) -> dict[str, Any]:
    exp = _ref(example).get("expected_kind")
    got = outputs.get("kind")
    return {"key": "terminator_kind_match", "score": int(exp == got),
            "comment": f"expected={exp} got={got}"}


def found_match(outputs: dict[str, Any], example: Any) -> dict[str, Any]:
    exp = bool(_ref(example).get("found"))
    got = _derive_found(outputs)
    return {"key": "found_match", "score": int(exp == got),
            "comment": f"expected={exp} got={got}"}


def report_ready_match(outputs: dict[str, Any], example: Any) -> dict[str, Any]:
    exp = bool(_ref(example).get("report_ready"))
    got = _derive_report_ready(outputs)
    return {"key": "report_ready_match", "score": int(exp == got),
            "comment": f"expected={exp} got={got} kind={outputs.get('kind')}"}


# Each expected sanctions label maps to the structured check that encodes its
# labeling discipline — so name_collision / non_sdn are graded by the existing
# hedge / non-SDN guards rather than a naive string compare.
_SANCTIONS_DISPATCH: dict[str, Callable[[dict[str, Any]], tuple[bool, str]]] = {
    "sanctioned": run_evals.sanctions_present,
    "not_sanctioned": run_evals.no_false_sanctions,
    "non_sdn": run_evals.ofac_non_sdn_labeling,
    "name_collision": run_evals.name_match_hedged,
}


def sanctions_status_match(outputs: dict[str, Any], example: Any) -> dict[str, Any]:
    exp = _ref(example).get("sanctions_status")
    fn = _SANCTIONS_DISPATCH.get(exp)
    if fn is None:
        return {"key": "sanctions_status_match", "score": 0,
                "comment": f"unknown expected status {exp!r}"}
    ok, comment = fn(outputs)
    return {"key": "sanctions_status_match", "score": int(ok),
            "comment": f"[{exp}] {comment}"}


def expected_entities_recall(outputs: dict[str, Any], example: Any) -> dict[str, Any]:
    expected = _ref(example).get("expected_entities") or []
    if not expected:
        return {"key": "expected_entities_recall", "score": 1.0,
                "comment": "no expected_entities (vacuous)"}
    blob = _answer_blob(outputs).lower()
    matched = [e for e in expected if str(e).lower() in blob]
    score = len(matched) / len(expected)
    return {"key": "expected_entities_recall", "score": score,
            "comment": f"{len(matched)}/{len(expected)} matched: {matched}"}


def _asserts_sdn(out: dict[str, Any]) -> bool:
    """Affirmative 'subject is OFAC SDN' assertion, after neutralizing the legit
    negative/non-SDN phrasings — mirrors the ofac_non_sdn_labeling guard."""
    text = _answer_blob(out).lower()
    neutral = (
        text.replace("non-sdn", " ")
        .replace("non sdn", " ")
        .replace("not on the sdn", " ")
        .replace("not the sdn", " ")
        .replace("not sdn", " ")
    )
    return any(p in neutral for p in
               ("sdn #", "ofac sdn", "sdn list", "specially designated national"))


def must_not_absent(outputs: dict[str, Any], example: Any) -> dict[str, Any]:
    """None of the `must_not` constraints are violated. Each phrase is classified
    and checked structurally:
      - sanctions/watchlist phrases -> no false `sanctioned` signal / watchlist hit
      - sdn/promote phrases         -> the subject is not asserted OFAC SDN
      - claim/connection/subject phrases on a NOT-found expectation -> no claims
    Soft, non-structural phrases (e.g. 'invented connection' on a clean found
    entity) are deferred to the LLM judge and pass here by design."""
    must_not = _ref(example).get("must_not") or []
    if not must_not:
        return {"key": "must_not_absent", "score": 1.0, "comment": "no must_not constraints"}

    no_false, _ = run_evals.no_false_sanctions(outputs)
    asserts_sdn = _asserts_sdn(outputs)
    n_claims = len(_result(outputs).get("claims", []))
    expects_found = bool(_ref(example).get("found"))

    violations: list[str] = []
    for phrase in must_not:
        p = str(phrase).lower()
        if "sdn" in p or "promote" in p:
            if asserts_sdn:
                violations.append(phrase)
        elif "sanction" in p or "watchlist" in p:
            if not no_false:
                violations.append(phrase)
        elif ("any claim" in p or "fabricated subject" in p) and not expects_found:
            if n_claims > 0:
                violations.append(phrase)
        # else: soft/semantic constraint -> LLM judge territory; pass here.
    ok = not violations
    return {"key": "must_not_absent", "score": int(ok),
            "comment": "clean" if ok else f"violated: {violations}"}


REFERENCE_EVALUATORS: list[Callable[..., dict[str, Any]]] = [
    terminator_kind_match,
    found_match,
    report_ready_match,
    sanctions_status_match,
    expected_entities_recall,
    must_not_absent,
]


# --- LLM judge over reference_answer (optional, guarded) --------------------


_JUDGE_TOOL = {
    "name": "grade_answer",
    "description": "Grade the agent answer against the reference for faithfulness and coverage.",
    "input_schema": {
        "type": "object",
        "properties": {
            "faithful": {
                "type": "boolean",
                "description": "True if the agent answer makes no claim that contradicts the reference (esp. no fabricated sanctions/connections).",
            },
            "coverage": {
                "type": "number",
                "description": "0.0-1.0 fraction of the reference's key points the agent answer covers.",
            },
            "rationale": {"type": "string", "description": "One sentence."},
        },
        "required": ["faithful", "coverage", "rationale"],
    },
}


def make_llm_judge() -> Callable[..., Any]:
    """Anthropic faithfulness/coverage judge vs `reference_answer`. Async so it
    runs inside aevaluate; lazily constructs the client so importing this module
    never needs a key."""
    from anthropic import AsyncAnthropic

    async def llm_judge(outputs: dict[str, Any], example: Any) -> dict[str, Any]:
        reference = _ref(example).get("reference_answer") or ""
        answer = _answer_blob(outputs)
        if not reference:
            return {"key": "llm_judge_faithfulness", "score": 1.0,
                    "comment": "no reference_answer (vacuous)"}
        client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
        prompt = (
            f"REFERENCE (ground truth):\n{reference}\n\n"
            f"AGENT ANSWER:\n{answer[:4000]}\n\n"
            "Grade the agent answer for faithfulness (no contradictions or "
            "fabrications vs the reference) and coverage of the reference's key "
            "points. Call grade_answer."
        )
        try:
            resp = await client.messages.create(
                model=_JUDGE_MODEL,
                max_tokens=300,
                tools=[_JUDGE_TOOL],
                tool_choice={"type": "tool", "name": "grade_answer"},
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    args = dict(block.input or {})
                    faithful = bool(args.get("faithful"))
                    coverage = float(args.get("coverage") or 0.0)
                    score = coverage if faithful else 0.0
                    return {"key": "llm_judge_faithfulness", "score": score,
                            "comment": f"faithful={faithful} coverage={coverage:.2f} — {args.get('rationale', '')}"}
        except Exception as e:  # never let the judge crash the experiment
            return {"key": "llm_judge_faithfulness", "score": 0.0,
                    "comment": f"judge error: {type(e).__name__}"}
        return {"key": "llm_judge_faithfulness", "score": 0.0, "comment": "no verdict"}

    return llm_judge


# --- Per-example metadata.checks (reuse run_evals' exact check logic) --------


def make_metadata_check_evaluators() -> list[Callable[..., dict[str, Any] | None]]:
    """One LangSmith evaluator per distinct check named across the golden rows'
    `metadata.checks`, reusing run_evals.EVALUATORS verbatim. Each self-skips the
    rows it doesn't apply to (returns None), so the grid shows exactly the cells
    the local suite scores."""
    def _make(check: str):
        fn = run_evals.EVALUATORS[check]

        def _ls(outputs: dict[str, Any], example: Any) -> dict[str, Any] | None:
            applicable = (getattr(example, "metadata", None) or {}).get("checks", [])
            if check not in applicable:
                return None
            ok, comment = fn(outputs)
            return {"key": check, "score": int(ok), "comment": comment}

        _ls.__name__ = f"check_{check}"
        return _ls

    return [_make(c) for c in sorted(run_evals.EVALUATORS)]


# --- Dataset load -----------------------------------------------------------


def _mirror_langsmith_key() -> bool:
    """Mirror the LangSmith key from Settings into os.environ so `Client()`
    authenticates even when LANGCHAIN_TRACING_V2 is off (the eval default). Never
    prints the value. Returns whether a key is available."""
    import os

    s = get_settings()
    if s.langchain_api_key:
        os.environ.setdefault("LANGCHAIN_API_KEY", s.langchain_api_key)
        os.environ.setdefault("LANGSMITH_API_KEY", s.langchain_api_key)
        os.environ.setdefault("LANGCHAIN_ENDPOINT", s.langchain_endpoint)
    return bool(s.langchain_api_key)


def load_dataset(client: Any) -> Any:
    """Read the live dataset by id, falling back to name."""
    try:
        return client.read_dataset(dataset_id=DATASET_ID)
    except Exception:
        return client.read_dataset(dataset_name=DATASET_NAME)


# --- Extra eval 1: recall-over-distance (deterministic, zero credit) --------


async def recall_over_distance_rows(n_intervening: int = 3) -> list[Row]:
    """Establish sanctioned Rosneft subsidiaries on turn 1, run N intervening
    turns on unrelated subjects, then assert the turn-1 entities + the dismissed
    name-collision SDN row are STILL recoverable via recall_state — exercising the
    real state_doc write+read path across distance (doc 09 §F memory regression
    net). Deterministic: it persists structured deltas exactly as production does
    and reads them back through the real recall tool; no model, no Redis, no
    credits."""
    case = f"recall_over_distance(n={n_intervening})"
    conv = multiturn._Conversation()

    # Turn 1: two Sayari-sanctioned subsidiaries + a DISMISSED name-collision SDN.
    subs = [
        {"id": "sayari-gazprom-neft", "name": "Gazprom Neft", "label": "Entity",
         "source_system": "sayari",
         "properties": {"sanctioned": True, "pep": False, "countries": ["RUS"]}},
        {"id": "sayari-tuapse-refinery", "name": "Tuapse Refinery LLC", "label": "Entity",
         "source_system": "sayari",
         "properties": {"sanctioned": True, "pep": False, "countries": ["RUS"]}},
    ]
    dismissed_hit = SanctionsHit(
        name_searched="Rosneft Global Trade S.A.",
        matched_name="Rosneft Trading S.A.",
        lists=["OFAC SDN"], sanctions_id="ofac-30947", score=0.81,
        on_watchlist=True, countries=["ch"],
    ).model_dump()
    turn1_state: dict[str, Any] = {
        "turn_index": 1,
        "user_message": "Map Rosneft's sanctioned subsidiaries.",
        "intent": "ownership_analysis",
        "pinned_node_ids": [],
        "turn_nodes": subs,
        "turn_leads": [],
        "raw_strong_hits": [dismissed_hit],
    }
    turn1_answer = TurnAnswer(
        answer="Gazprom Neft and Tuapse Refinery LLC are sanctioned subsidiaries.",
        referenced_node_ids=["sayari-gazprom-neft", "sayari-tuapse-refinery"],
        claims=[Claim(
            text="Rosneft Trading S.A. is a separate OFAC SDN entity (name collision).",
            source_refs=[SourceRef(source="opensanctions", sanctions_id="ofac-30947")],
            confidence="high",
        )],
        sanctions_hits=[],
    )
    conv.finalize(turn1_state, summary=None, answer=turn1_answer)

    # N intervening turns on unrelated, clean subjects (push turn 1 into the past).
    fillers = ["Spotify", "a clean logistics company", "an unrelated tech vendor",
               "a domestic retailer", "a media holding company"]
    for i in range(n_intervening):
        nm = fillers[i % len(fillers)]
        state: dict[str, Any] = {
            "turn_index": 2 + i,
            "user_message": f"Investigate {nm}.",
            "intent": "profile_entity",
            "pinned_node_ids": [],
            "turn_nodes": [{
                "id": f"sayari-filler-{i}", "name": nm, "label": "Entity",
                "source_system": "sayari",
                "properties": {"sanctioned": False, "pep": False, "countries": ["USA"]},
            }],
            "turn_leads": [],
            "raw_strong_hits": [],
        }
        conv.finalize(state, summary=None,
                      answer=TurnAnswer(answer=f"{nm} is a clean low-risk entity."))

    # Recall AFTER the distance: the turn-1 findings must still be there.
    async with multiturn._recall_against(conv.doc):
        ents = await recall_state_tool("c-rod", kind="entities", sanctioned=True)
        sanc = await recall_state_tool("c-rod", kind="sanctions")

    ent_ids = {r.get("id") for r in ents.get("items", [])}
    subs_recalled = {"sayari-gazprom-neft", "sayari-tuapse-refinery"}.issubset(ent_ids)
    dismissed_recalled = any(
        r.get("matched_name") == "Rosneft Trading S.A." and r.get("verdict") == "dismissed"
        for r in sanc.get("items", [])
    )
    # The deterministic follow-up prefetch still surfaces the dismissed row by name.
    prefetch = build_followup_prefetch(conv.doc, "which subsidiaries were sanctioned again?")
    prefetch_ok = "Rosneft Trading S.A." in prefetch and "dismissed" in prefetch

    return [
        (case, "subsidiaries_recalled_after_distance", subs_recalled,
         f"sanctioned_ids={sorted(i for i in ent_ids if i)}"),
        (case, "dismissed_collision_recalled", dismissed_recalled,
         "Rosneft Trading S.A. still recoverable as a dismissed SDN row"),
        (case, "followup_prefetch_surfaces", prefetch_ok,
         f"prefetch_len={len(prefetch)}ch"),
    ]


# --- Extra eval 2: token-budget guardrail (deterministic, zero credit) ------


def _estimate_tokens(text: str) -> int:
    """Conservative offline token estimate (~4 chars/token for English). Avoids a
    tokenizer dependency and any network call so the guardrail runs free."""
    return max(1, len(text) // 4)


def _make_state_doc(n_entities: int, n_leads: int, n_sanctions: int) -> dict[str, Any]:
    """A state_doc of arbitrary size, projected through the real registry."""
    doc = {
        **conversations._empty_state_doc(),
        "resolved_entities": {
            f"subject {i}": {
                "entity_id": f"sayari-subj-{i}", "label": f"Subject {i}",
                "type": "company", "sanctioned": False,
                "first_seen_turn": 1, "last_seen_turn": 3,
            } for i in range(max(1, n_entities // 3))
        },
        "leads": [
            {"entity_id": f"lead-{i}", "label": f"Lead Co {i}", "type": "company",
             "countries": ["CYP"], "sanctioned": False, "from_turn": 3,
             "from_query": "Rosneft-linked trading companies"}
            for i in range(n_leads)
        ],
        "sanctions_adjudicated": [
            {"sanctions_id": f"ofac-{i}", "matched_name": f"Sanctioned Co {i}",
             "lists": ["OFAC SDN"], "verdict": "confirmed" if i % 2 else "dismissed",
             "from_turn": 1}
            for i in range(n_sanctions)
        ],
        "named_ids": {f"named-{i}": {"label": f"Named {i}", "type": "company"}
                      for i in range(n_entities)},
        "pinned_node_ids": [f"pin-{i}" for i in range(20)],
    }
    doc["entities"] = conversations._project_entities(doc)
    return doc


def token_budget_rows() -> list[Row]:
    """The assembled per-turn context must stay within MAX_TOKENS_PER_TURN even as
    structured state grows unboundedly — the context-stuffing guardrail (doc 09
    §6 / Phase C). Assert (1) the fully-assembled context for a HUGE investigation
    fits the budget, and (2) the injected INVESTIGATION STATE core does not scale
    with case size (a huge state_doc renders a core no materially larger than a
    tiny one)."""
    case = "token_budget_guardrail"

    small = _make_state_doc(4, 3, 2)
    huge = _make_state_doc(400, 80, 100)

    # A realistic (capped) prose digest + a graph payload accompany the state core.
    prose = "Prior turns digest. " * 120
    graph = {"nodes": [{"id": f"n{i}", "name": f"Node {i}", "label": "Entity"}
                       for i in range(30)], "edges": []}

    small_ctx = build_context_block(prose, graph, small["pinned_node_ids"], False, small)
    huge_ctx = build_context_block(prose, graph, huge["pinned_node_ids"], False, huge)

    huge_tokens = _estimate_tokens(huge_ctx)
    within_budget = huge_tokens <= MAX_TOKENS_PER_TURN

    # The state core's contribution must not balloon with the investigation: the
    # huge assembled context is within a small constant of the small one.
    core_growth = len(huge_ctx) - len(small_ctx)
    fixed_core = core_growth <= 400

    return [
        (case, "assembled_context_within_budget", within_budget,
         f"~{huge_tokens} tok <= {MAX_TOKENS_PER_TURN} (huge state: 400 entities/80 leads/100 sanctions)"),
        (case, "state_core_does_not_scale", fixed_core,
         f"core_growth={core_growth}ch (huge vs small state_doc)"),
    ]


async def run_extras() -> tuple[int, int, list[Row]]:
    """Run both standalone extra evals. Deterministic + zero-credit, so they run
    in every mode to prove the write/read + budget wiring."""
    rows: list[Row] = []
    for label, coro in (("recall_over_distance", recall_over_distance_rows()),):
        try:
            rows += await coro
        except Exception as e:
            rows.append((label, "deterministic_check", False, f"crashed: {e}"))
    try:
        rows += token_budget_rows()
    except Exception as e:
        rows.append(("token_budget_guardrail", "deterministic_check", False, f"crashed: {e}"))
    passed = sum(1 for row in rows if row[2])
    return passed, len(rows), rows


def _print_rows(rows: list[Row]) -> None:
    print(f"\n{'CASE':<34}{'CHECK':<38}{'RESULT':<8}COMMENT")
    print("-" * 100)
    for name, check, ok, comment in rows:
        print(f"{name:<34}{check:<38}{'PASS' if ok else 'FAIL':<8}{comment}")


# --- Modes ------------------------------------------------------------------


async def run_dry(limit: int | None) -> int:
    """Zero-credit verification: load the dataset, confirm the evaluators are
    well-formed against a REAL reference example (no agent call), and run the two
    deterministic extra evals."""
    from langsmith import Client

    if not _mirror_langsmith_key():
        print("LANGCHAIN_API_KEY not set; cannot reach LangSmith.")
        return 2

    client = Client()
    ds = load_dataset(client)
    examples = list(client.list_examples(dataset_id=ds.id))
    print(f"Dataset loaded: {ds.name} ({ds.id}) — {len(examples)} examples")

    ref_evaluators = list(REFERENCE_EVALUATORS)
    meta_evaluators = make_metadata_check_evaluators()
    print(f"Evaluators: {len(ref_evaluators)} reference + "
          f"{len(meta_evaluators)} metadata-check (+1 LLM judge when --judge)")

    # Validate every reference evaluator is well-formed against a SYNTHETIC target
    # output graded vs a REAL example — proves the wiring without an agent call.
    sample = examples[0]
    synthetic = {
        "kind": "answer",
        "result": {
            "answer": "Synthetic answer mentioning Gazprom and Gazprom Neft.",
            "claims": [{"text": "x", "source_refs": [{"source": "sayari", "sayari_entity_id": "e1"}]}],
            "sayari_risk_factors": [{"name": "sanctioned", "path": [1]}],
            "report_ready": True,
            "referenced_node_ids": ["e1"],
        },
        "tools_used": ["sayari_search"],
    }
    bad = 0
    for ev in ref_evaluators:
        res = ev(synthetic, sample)
        if not (isinstance(res, dict) and "key" in res and "score" in res):
            bad += 1
            print(f"  ! malformed evaluator: {ev.__name__} -> {res!r}")
    print(f"Reference evaluators well-formed: {len(ref_evaluators) - bad}/{len(ref_evaluators)}")

    p, t, rows = await run_extras()
    _print_rows(rows)
    print(f"\nExtra evals: {p}/{t} checks passed.")
    print("\nDry-run OK (no credits spent). Run a live experiment with --live.")
    return 0 if (bad == 0 and p == t) else 1


async def run_live(limit: int | None, judge: bool) -> int:
    """Upload a real experiment to LangSmith: run the live agent on each example
    and score it with the reference + metadata-check evaluators (+ optional judge)."""
    from langsmith import Client, aevaluate

    if not _mirror_langsmith_key():
        print("LANGCHAIN_API_KEY not set; cannot reach LangSmith.")
        return 2
    if not get_settings().anthropic_api_key:
        print("ANTHROPIC_API_KEY not set; a live run needs it.")
        return 2

    apply_langsmith_env(get_settings())  # turn tracing on if configured
    client = Client()
    ds = load_dataset(client)
    data: Any = ds.id
    if limit:
        data = list(client.list_examples(dataset_id=ds.id))[:limit]
        print(f"Live SMOKE run: {len(data)} example(s) from {ds.name}")
    else:
        print(f"Live run: full dataset {ds.name} ({ds.id})")
    print(f"Agent model: {_RUN_MODEL or DEFAULT_MODEL}")

    evaluators: list[Any] = list(REFERENCE_EVALUATORS) + make_metadata_check_evaluators()
    if judge:
        evaluators.append(make_llm_judge())
        print(f"LLM judge ON ({_JUDGE_MODEL})")

    # Name each model's run as its own comparable experiment under the dataset,
    # and stamp the full model id into metadata so the runs are filterable.
    model_id = _RUN_MODEL or DEFAULT_MODEL
    experiment_prefix = (
        f"sayari-demo-{_model_tag(model_id)}" if _RUN_MODEL else "sayari-demo-langsmith"
    )
    metadata: dict[str, Any] = {"model": model_id}

    results = await aevaluate(
        target,
        data=data,
        evaluators=evaluators,
        client=client,
        experiment_prefix=experiment_prefix,
        metadata=metadata,
        max_concurrency=1,
    )
    print("Uploaded to LangSmith. Experiment:",
          getattr(results, "experiment_name", "(see UI)"))

    # The extra evals ride along (deterministic, free) so one command covers all.
    p, t, rows = await run_extras()
    _print_rows(rows)
    print(f"\nExtra evals: {p}/{t} checks passed.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="LangSmith eval runner for the ERR agent.")
    parser.add_argument("--live", action="store_true",
                        help="Run the live agent + upload an experiment (spends Anthropic credits).")
    parser.add_argument("--judge", action="store_true",
                        help="Include the Anthropic LLM judge (only meaningful with --live).")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap examples (cheap live smoke test, e.g. --limit 1).")
    parser.add_argument("--model", default=None,
                        help=(
                            "Main-agent model for the live run (allowlisted; "
                            "off-list falls back to the default Sonnet 4.5). "
                            "One of: " + ", ".join(sorted(ALLOWED_MODELS))
                        ))
    args = parser.parse_args()

    global _RUN_MODEL
    _RUN_MODEL = args.model
    if args.model is not None and args.model not in ALLOWED_MODELS:
        print(f"warning: --model {args.model!r} is not allowlisted; falling back "
              f"to the default ({DEFAULT_MODEL}). Allowed: "
              + ", ".join(sorted(ALLOWED_MODELS)))

    if args.live:
        sys.exit(asyncio.run(run_live(args.limit, args.judge)))
    else:
        sys.exit(asyncio.run(run_dry(args.limit)))


if __name__ == "__main__":
    main()
