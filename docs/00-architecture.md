# Architecture — mental model

> Read this before any code. It's the map.

## The pitch (one sentence)

A user types a person or company name. An LLM agent investigates by calling tools across
two data sources (ICIJ graph + OpenSanctions watchlists) and returns a structured risk
summary with citations, while streaming its reasoning live to a 3-panel UI.

## Four layers, three boundaries

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Browser (Next.js on Vercel)                          │
│  - 3 panels: chat, graph (React Flow), tool-call feed                       │
│  - SSE client subscribes to a session_id and renders events as they arrive  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      │  HTTPS (POST /assess, GET /stream/:id)
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│              Backend (FastAPI on Cloud Run)  ── 4 LAYERS ──                 │
│                                                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ 1. API layer  (main.py)                                             │    │
│  │    - Endpoints, CORS, request validation, SSE plumbing              │    │
│  │    - Knows nothing about agents or tools                            │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                  │                                          │
│                                  ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ 2. Agent layer  (agent_native.py)                                   │    │
│  │    - Runs the Anthropic tool-use loop                               │    │
│  │    - Decides which tool to call next based on prior results         │    │
│  │    - Emits SSE events as it goes                                    │    │
│  │    - SWAPPABLE: agent_graph.py replaces this in Phase 2 (LangGraph) │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                  │                                          │
│                                  ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ 3. Tools layer  (tools.py)                                          │    │
│  │    - 6 tool functions Claude can call                               │    │
│  │    - Knows nothing about agents — just inputs in, structured out    │    │
│  │    - Same tools serve Phase 1 native agent AND Phase 2 LangGraph    │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                  │                                          │
│                                  ▼                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │ 4. Data layer  (graph.py, sanctions.py)                             │    │
│  │    - graph.py: Neo4j driver wrapper, Cypher queries                 │    │
│  │    - sanctions.py: OpenSanctions HTTP client                        │    │
│  │    - Pure I/O, returns Pydantic-typed shapes                        │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────────────┘
              │                       │                       │
              ▼                       ▼                       ▼
   ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────────┐
   │  Neo4j 5.13      │   │  OpenSanctions   │   │  Upstash Redis       │
   │  ICIJ leaks      │   │  /match/default  │   │  Session state       │
   │  ~2M nodes       │   │  HTTPS           │   │  REST API            │
   │  Bolt protocol   │   │                  │   │                      │
   └──────────────────┘   └──────────────────┘   └──────────────────────┘
```

## Why layered like this

The single most important architectural choice is the **agent ↔ tools boundary**. In Phase
1 the agent is `agent_native.py` (a hand-rolled Anthropic tool-use loop). In Phase 2 it
becomes `agent_graph.py` (LangGraph). The swap touches only that one file. Tools, data,
endpoints, SSE event shapes — all identical.

This boundary is exactly the question a Sayari interviewer will ask: *"what does an
orchestration framework like LangGraph give you that you can't get from raw API calls?"*
The honest answer is "graph-shaped control flow, retries, durable state, observability
hooks — but the agent's job is still calling tools you write." We get to demonstrate that
we understand both sides of the boundary because we built both halves cleanly.

## Request lifecycle (single investigation)

1. User types "Sergey Roldugin", clicks Search.
2. Browser → `POST /assess {"name": "Sergey Roldugin"}` → backend returns `{"session_id": "uuid"}`.
3. Backend stashes a job under that session in Upstash and kicks off the agent in a background task.
4. Browser → `GET /stream/{session_id}` (EventSource) → SSE connection opens.
5. Agent layer enters its loop:
   - Sends initial Anthropic request with system prompt + tools + user query.
   - Receives `tool_use: search_entity("Sergey Roldugin")` from Claude.
   - Emits `tool_call_start` SSE event → frontend logs it.
   - Executes `tools.search_entity(...)` → which calls `graph.search_entity(...)` → which runs Cypher against Neo4j → returns matched nodes.
   - Emits `tool_call_result` SSE event with nodes/edges → frontend merges into React Flow.
   - Sends tool result back to Claude.
   - Claude returns `tool_use: get_relationships(<roldugin_id>)`. Repeat.
   - Eventually Claude returns final text containing a JSON `RiskSummary`.
   - Backend validates with Pydantic, emits `summary` SSE event with the structured object → frontend renders the summary card.
   - Backend emits `done`, closes the SSE stream.
6. User sees: a graph that built up live, a tool-call feed showing the agent's reasoning, and a structured summary with citations.

## Why this scales (the part that matters for FDE)

- **Add a new data source** (e.g., LexisNexis, Refinitiv) → add a function to the data layer + a tool in the tools layer. Agent picks it up because tool descriptions are the API.
- **Replace the LLM** (Claude → GPT-5 → Gemini) → only the agent layer changes; tool I/O stays identical.
- **Add a new client deployment** → same backend, configure secrets and CORS for the new environment.
- **Productionize** → add auth on `/assess`, persistence on session state, structured tracing → LangSmith, rate limiting, multi-tenant. The structure already supports each of these — the boundaries are the right ones.
