<p align="center">
  <img src="assets/sayari-logo.png" alt="Sayari" width="200" />
</p>

# Entity Risk Resolver

An investigative copilot. You give it a person or a company, it investigates across three data sources, and it hands back a sourced risk picture on a branching canvas you can keep pulling on.

The data spine is Sayari for authoritative ownership and UBO traversal, ICIJ Offshore Leaks for leak provenance, and OpenSanctions for watchlist screening. The UX is a branching investigation canvas instead of a linear chat, because real investigations fork and double back. Built end-to-end as a Sayari FDE take-home.

For the full writeup (design decisions, the memory bug I found and fixed, and the two-model eval results) see [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md).

## Architecture

<img width="2680" height="2717" alt="Architecture diagram" src="https://github.com/user-attachments/assets/4425fb24-7516-48c6-89b5-70093519f460" />

**Request lifecycle.** A message comes in from the Next.js canvas and hits the FastAPI backend on Cloud Run. A Haiku intent router classifies the turn and narrows which tools are bound. A Sonnet 4.5 LangGraph agent then runs the investigation loop, calling tools and observing results, with its reasoning streaming back to the UI over SSE. The turn ends on a structured terminator: `submit_answer` for a conversational reply, `submit_summary` for the formal risk report.

**Three data sources.** Sayari (ownership, UBO, identifiers, risk factors) is the authoritative layer. ICIJ Offshore Leaks, served from Neo4j, adds leak provenance. OpenSanctions confirms watchlist hits against OFAC, BIS, EU, UN, and PEP lists, reporting the program verbatim so a non-SDN hit never gets promoted to SDN.

**Memory.** State lives in Upstash Redis. L1 is per-conversation structured working memory, merged each turn, with a deterministic projection that deposits only from structured tool output and the validated terminator schema, never the prose answer. Branching keeps path-scoped state so sibling branches stay isolated. A flag-gated L2 episodic vector layer (Upstash Vector) can recall prior investigations, off by default.

**Evals.** A deterministic regression suite (67/67, no API spend) gates every change, and a 12-case golden dataset runs the live agent through LangSmith with reference evaluators plus recall-over-distance and token-budget guardrails.

## Project tree

```
sayari-demo/
├── assets/
│   └── sayari-logo.png
├── PROJECT_SUMMARY.md            # full design + eval writeup
├── backend/                      # FastAPI + agent
│   ├── app/
│   │   ├── main.py               # endpoints, SSE, CORS, conversation routes
│   │   ├── agent.py              # dispatches a turn to the active impl
│   │   ├── agent_graph.py        # LangGraph agent (branching, model select, tracing)
│   │   ├── agent_native.py       # original hand-rolled tool-use loop (fallback)
│   │   ├── agent_common.py       # shared: model allowlist, context, memory projection
│   │   ├── intent.py             # Haiku intent router, narrows the toolset
│   │   ├── tools.py              # the agent's tools (resolve, ownership, sanctions, ...)
│   │   ├── tools_lc.py           # LangChain tool adapters
│   │   ├── prompts.py            # system + tool prompts
│   │   ├── schema.py             # Pydantic request/response + terminator schemas
│   │   ├── sayari.py             # Sayari Graph API client
│   │   ├── graph.py              # Neo4j / ICIJ Offshore Leaks queries
│   │   ├── sanctions.py          # OpenSanctions screening
│   │   ├── conversations.py      # Redis state, branching, turn tree
│   │   ├── episodic.py           # flag-gated L2 vector memory
│   │   └── config.py             # env-driven settings
│   ├── evals/
│   │   ├── run_evals.py          # deterministic suite + live scoring
│   │   ├── langsmith_eval.py     # LangSmith dataset runner + upload
│   │   ├── sayari-demo-golden.jsonl  # 12-case golden dataset
│   │   ├── source_mix.py         # data-source coverage check
│   │   ├── branching.py          # branch-isolation checks
│   │   ├── multiturn.py          # multi-turn recall checks
│   │   └── README.md
│   ├── Dockerfile
│   ├── requirements.txt
│   └── README.md
└── frontend/                     # Next.js canvas UI
    ├── app/                      # App Router entry
    ├── components/
    │   ├── canvas/               # React Flow investigation canvas
    │   ├── manager/              # conversation history menu
    │   ├── ui/                   # shadcn / prompt-kit primitives
    │   ├── GraphPanel.tsx        # evidence graph
    │   ├── AnswerCard.tsx        # conversational reply card
    │   └── RiskSummaryCard.tsx   # formal risk report card
    ├── lib/
    │   ├── sse-client.ts         # SSE consumption
    │   ├── conversation-store.ts # client state
    │   ├── canvas-layout.ts      # graph layout
    │   ├── groupClustering.ts    # subject grouping / convex hulls
    │   ├── report-pdf.ts         # PDF export
    │   └── types.ts
    └── README.md
```

## Setup

Run the backend and frontend in two terminals.

```bash
# backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in real values
uvicorn app.main:app --reload --port 8080
```

```bash
# frontend
cd frontend
cp .env.local.example .env.local  # set NEXT_PUBLIC_BACKEND_URL=http://localhost:8080
npm install
npm run dev
```

Open `http://localhost:3000`. See [`backend/README.md`](backend/README.md) and [`frontend/README.md`](frontend/README.md) for env vars and details.

## Deploy

```bash
# backend → Cloud Run
cd backend
gcloud config set project sayari-demo-elie
gcloud run deploy sayari-demo-backend \
  --source . --region us-central1 --allow-unauthenticated \
  --memory 1Gi --timeout 300 --min-instances 0 --max-instances 5
```

Frontend deploys to Vercel from GitHub (root directory `frontend/`, env var `NEXT_PUBLIC_BACKEND_URL` pointing at the Cloud Run URL). Pushes to `main` deploy automatically.
