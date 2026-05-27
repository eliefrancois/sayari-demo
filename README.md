# Entity Risk Resolver

> Read-only investigation agent that traverses the ICIJ Offshore Leaks corporate-ownership graph,
> cross-references entities against global sanctions lists, and produces structured, sourced risk
> summaries with a live UI of the agent's reasoning.

A miniature of the Sayari Graph + Graph AI loop, built end-to-end as a portfolio project.

**Status:** under active construction. Final README, demo URL, screenshots, and walkthrough land Thursday night.

## Stack

- **Backend:** Python 3.11, FastAPI, Anthropic Claude Sonnet 4 (native tool-use API), `neo4j` driver, `httpx`, Pydantic
- **Frontend:** Next.js 14 (App Router), TypeScript, Tailwind, React Flow
- **Data:** Neo4j 4.4 with the full ICIJ Offshore Leaks database (~810K nodes)
- **Sanctions:** OpenSanctions `/match/default` API
- **Session state:** Upstash Redis
- **Hosting:** Cloud Run (backend), Vercel (frontend), DigitalOcean droplet (Neo4j)

## Architecture (placeholder — full diagram + walkthrough below)

```
Browser  ──POST /assess──▶  FastAPI  ──tool-use loop──▶  Neo4j (ICIJ graph)
       ◀──SSE events────                ──HTTP─────────▶  OpenSanctions
                                       ──KV──────────▶   Upstash Redis
```

Four layers in the backend, with a deliberate seam between agent and tools so the orchestrator
is swappable (native Anthropic tool-use today → LangGraph in Phase 2) without touching tools,
data, or the SSE contract.

## Local dev

See `backend/README.md` and `frontend/README.md`.

---

*Built for the Sayari interview process. Not affiliated with Sayari.*
