# Demo Runbook — Sayari interview

> An 8-10 minute walkthrough. Goal: show technical decisions and judgment in a short window, not exhaustively tour every feature.

## The pitch (one line)

An autonomous risk-investigation agent. You give it a name, it traverses the ICIJ Offshore Leaks graph in Neo4j, cross-references global sanctions, and produces a sourced risk report while streaming its reasoning live.

## What the platform does

- Investigates individuals and companies in offshore leak data
- Maps corporate networks: officers, directors, shareholders, intermediaries
- Traces beneficial ownership and shared-address clusters (the shell-company tell)
- Cross-references subjects and their connections against global sanctions and watchlists (OFAC, EU, UN, UK HMT)
- Flags politically exposed persons and surfaces cross-leak connections
- Produces two output shapes: full structured risk reports, and quick conversational answers to follow-ups

---

## The data source: what ICIJ is

This is worth 60 seconds in the demo because it shows you understand the data, not just the code.

**ICIJ** is the International Consortium of Investigative Journalists. They publish the **Offshore Leaks Database**, a public aggregation of several major leaks and investigations into offshore finance:

- **Offshore Leaks** (2013)
- **Panama Papers** (2016, the Mossack Fonseca law firm leak)
- **Bahamas Leaks** (2016)
- **Paradise Papers** (2017, Appleby and corporate registries)
- **Pandora Papers** (2021)

The data ships as a graph, which is why Neo4j is the natural home for it. Four node types do most of the work:

- **Entity**: an offshore company, trust, or foundation
- **Officer**: a person or organization that plays a role in an entity (director, shareholder, beneficiary)
- **Intermediary**: the firm that set the structure up (a law firm or corporate services provider, like Mossack Fonseca)
- **Address**: a registered address, which is the connective tissue that exposes shell hubs

Relationships carry the meaning: `officer_of`, `registered_address`, `intermediary_of`, plus explicit entity-resolution edges in the newer dump (`same_name_as`, `probably_same_officer_as`, `same_id_as`) that link the same real-world actor across different leaks.

Scale: the public database covers roughly 800,000+ offshore entities. Loaded in full, the graph is about 2M nodes. I loaded the complete dump, not a sample.

**The nuance that matters for compliance** (say this out loud): appearing in these leaks is not proof of wrongdoing. Offshore structures are often legal. ICIJ says this explicitly. That is exactly why the product surfaces *patterns and citations* rather than verdicts, and why every claim is sourced. The analyst decides, the tool gives them defensible evidence.

---

## How it's built: the layers (the 60-second arc)

The whole system is a request flowing down through layers and events streaming back up. Each layer has one job and knows nothing about the layer above it. That separation is the point: it's what lets you swap pieces without rewriting the rest.

**Frontend (Next.js on Vercel).** Three panels: chat, the React Flow graph, the tool-call feed. It opens an SSE connection and renders events as they arrive. Choice: server-sent events over websockets, because the traffic is one-directional (server to client) and SSE is simpler to run on Cloud Run. Tradeoff: no client-to-server streaming, which I don't need here.

**API layer (`main.py`, FastAPI).** Endpoints, CORS, request validation, SSE plumbing. Knows nothing about agents or tools. Choice: keep it thin so the interesting logic lives in testable modules, not in HTTP handlers.

**Agent layer (`agent_graph.py`, with `agent_native.py` as fallback).** Runs the tool-use loop, decides the next tool from prior results, picks a terminator, streams events. This is the brain. Choice: I built a hand-rolled loop first to learn the mechanics, then migrated to a LangGraph state machine for typed state and observability. Tradeoff: LangGraph adds a dependency and some indirection, but I get durable state and LangSmith traces for free. The native version stays behind a flag so I can show both.

**Tools layer (`tools.py`).** Six functions Claude can call, inputs in, structured Pydantic out. Knows nothing about the agent. Choice: the tool descriptions ARE the agent's API, so adding a capability is adding a function, not editing the loop. This is the single most important boundary, and the one an interviewer will probe.

**Data layer (`graph.py`, `sanctions.py`).** All Cypher lives in `graph.py`; `sanctions.py` is the OpenSanctions HTTP client. Pure I/O, typed shapes out. Choice: no Cypher leaks above this line, so swapping Neo4j for another graph store touches one file. Tradeoff: a thin abstraction tax, worth it for the seam.

**Session state (Upstash Redis).** Holds conversation memory so multi-turn follow-ups remember the graph already built. Choice: a managed REST-based store so Cloud Run stays stateless and horizontally scalable.

The throughline: **the agent-to-tools boundary is the design.** Replace the LLM and only the agent layer changes. Add a data source (LexisNexis, Refinitiv) and you add a data function plus a tool, nothing else. That's the FDE-relevant claim, and you built both halves cleanly enough to back it up.

---

## The 8-10 minute runbook

### Cold open (30 sec)
"I built an autonomous risk-investigation agent. You give it a name, it traverses the ICIJ Offshore Leaks graph in Neo4j, cross-references global sanctions, and produces a sourced risk report. It's the same problem space Sayari works in: turning messy entity data into defensible answers."

### Act 1: The hero investigation (3 min)
Type: **`Investigate Sergey Roldugin`**

Narrate over the live stream:
- "Text is streaming token-by-token from Claude Sonnet 4.5. Real reasoning steps, not canned."
- Point at the agent activity feed: "Each row is a real tool call. It chose `search_entity`, then `get_relationships`, then `check_sanctions` on its own. I didn't script the order."
- Point at the graph building in real time: "The agent drives the graph. Every node it touches gets added via server-sent events."
- When the risk report lands: "Every claim has a source chip. Click one, it focuses the node. That's enforced at the schema level. The model literally cannot emit a claim without provenance."

This run reliably fires all five signals (sanctioned, connected to sanctioned, shell pattern, nominee, struck off). It's the money shot.

### Act 2: Conversational depth (2 min)
Same conversation, follow-up: **`how is he connected to sanctioned parties?`**

"Notice it answered conversationally instead of generating a whole new report. Deliberate design: two terminators. `submit_summary` for investigations, `submit_answer` for follow-ups. The agent decides which fits. The conversation persists in Upstash Redis, so it remembers the graph it already built."

Optional: right-click a node, expand it. "The user can also drive the graph, not just the agent."

### Act 3: The guardrails (2 min, the engineering-maturity moment)
New investigation, type something vague: **`I'm looking into Caribbean shell companies`**

"It asks clarifying questions instead of hallucinating a target. In compliance, a confident wrong answer is worse than a question."

Then show the evals. Do NOT run them live: each case is 60-90s and there are 6, so the harness takes 6-9 minutes. Pre-run it the night before with `--push` (see checklist), then in the demo just open the **LangSmith experiment** (the scored grid under Datasets & Experiments).

"14 regression checks across 6 cases: does it find real entities, decline on nonsense, never emit an unsourced claim, never flag a false-positive sanction. Same LangGraph agent that serves production, traces pushed to LangSmith." Click into one trace to show the tool-by-tool replay.

> LangSmith surfaces, so you say the right thing:
> - `python -m evals.run_evals` (no flag) emits 6 raw trace trees only; scores stay in the terminal.
> - `python -m evals.run_evals --push` creates the scored Experiment grid. That's what you show.

### Close (1 min)
Tradeoffs and what's next (below). End on: "This is a working slice of a production system, and I know exactly where the edges are."

---

## Technical decisions to name (the "why")

- **Hand-rolled the agent loop first, then migrated to LangGraph.** "I wanted to understand the ReAct loop before abstracting it. LangGraph gave me a typed state machine and free LangSmith observability. I kept the native version behind a feature flag as a fallback."
- **Tools are a clean data layer.** All Cypher lives in one module. The agent never sees a query string. "If we swapped Neo4j for TigerGraph, one file changes."
- **Provenance enforced by Pydantic, not prompting.** Claims require `source_refs`. The model can't skip it.
- **Sanctions gating.** A match needs both a high score AND confirmation it's on an actual watchlist. "I hit a false positive on a name collision early. The fix was to stop trusting score alone."
- **Streaming over SSE**, client state in a reducer, graph layout with d3-force (industry-standard force-directed, not hand-rolled).

## Tradeoffs (be honest, recruiters value this)

- **Token budget vs context depth.** Every step re-sends history, so deep investigations grow quadratically. I slim tool payloads to identity plus key fields, but hub nodes still strain a low API rate tier. Real fix is caching results across turns and a purpose-built aggregation tool.
- **Hub nodes are sampled, capped at 50.** Keeps the canvas and token count sane, but very large networks aren't exhaustive.
- **Fuzzy search.** Lucene gives fuzzy matches. I push relevance judgment to the LLM instead of a hard score cutoff. Flexible, occasionally over-eager.
- **Static ICIJ dump, single public Neo4j.** Demo infra, not hardened.

## What I'd add with more time

- **An aggregation/hub tool** (`find_hub_addresses`, count-by-jurisdiction). "Broad questions like 'top Russian addresses' have no tool today, so the agent improvises and burns tokens. A purpose-built tool answers them better and cheaper."
- **Cross-turn result caching** so the agent reuses what it already fetched instead of re-paying for it.
- **Confidence scoring on entity resolution**, not just presence of an ER edge.
- **Auth, per-user rate handling, a real observability dashboard.**

---

## Rate-limit safety (read before you present)

Two query shapes break the demo on a low API tier, because they pull hub nodes with thousands of neighbors and blow past the 30k input-tokens-per-minute cap:

- Hub nodes: **Mossack Fonseca**, large intermediaries
- "All X" aggregations: **"all Russian addresses"**, "every BVI entity"

Avoid both live. Use bounded, named subjects.

## Pre-demo checklist (do the night before)

1. **Raise Anthropic to Tier 2** at console.anthropic.com/settings/limits (30k/min to 450k/min). Biggest safety net.
2. **Restart the backend** if demoing locally, so it picks up the latest context-slimming and retry fixes.
3. **Pre-run the evals with `--push`** so the scored LangSmith experiment exists before you present (the run itself takes 6-9 min, you don't want to do it live):
   ```bash
   cd backend && .venv/bin/python -m evals.run_evals --push
   ```
   Then bookmark the experiment URL it prints. In the demo you just open that page.
4. **Warm a Roldugin run** 30 minutes before, so connections are hot.
5. **Screen-record a clean Roldugin run** as backup. If the network or API misbehaves live, play the tape and keep talking.
6. **Pin safe subjects:** Roldugin (hero), Jeffrey Epstein (shows false-positive sanctions handling), a greeting (shows the conversational terminator). Avoid Mossack Fonseca and any "all X" query.
