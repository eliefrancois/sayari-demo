# Backend

FastAPI service that runs the investigation agent and streams its reasoning to the UI over SSE.

## How a turn flows

1. **API** (`app/main.py`): endpoints, SSE plumbing, CORS, conversation routes.
2. **Agent dispatch** (`app/agent.py`): picks the implementation per `AGENT_IMPL`.
   - `agent_graph.py`: the LangGraph agent. Supports branching, per-request model selection, and LangSmith tracing. This is what the evals and the live demo run on.
   - `agent_native.py`: the original hand-rolled Anthropic tool-use loop. Still here as a fallback.
   - `agent_common.py`: shared pieces both paths use, the model allowlist, context block assembly, memory projection.
3. **Tools** (`app/tools.py`, `app/tools_lc.py`): the things the agent can do, resolve, ownership, sanctions, leak search, fetch record, recall memory.
4. **Data** (`app/sayari.py`, `app/graph.py`, `app/sanctions.py`): the Sayari Graph API, the ICIJ Offshore Leaks graph in Neo4j, and OpenSanctions.
5. **Memory** (`app/conversations.py`, `app/episodic.py`): L1 structured working memory and branching state in Upstash Redis, plus the flag-gated L2 episodic vector layer.

The intent router (`app/intent.py`) runs a cheap Haiku classification before the main loop to label the turn and narrow the bound toolset.

## Run locally

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in real values
uvicorn app.main:app --reload --port 8080
```

Smoke test:

```bash
curl http://localhost:8080/health
```

## Env vars

See `.env.example` for the full list. The ones you need to fill in:

- `ANTHROPIC_API_KEY`: the agent and the intent router.
- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`: the ICIJ Offshore Leaks graph.
- `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN`: session and investigation state.
- `SAYARI_CLIENT_ID`, `SAYARI_CLIENT_SECRET`: the Sayari Graph API.
- `OPENSANCTIONS_API_URL` (and optionally a key for higher rate limits).
- `ALLOWED_ORIGINS`: CORS allowlist.

Optional: `AGENT_IMPL` (`graph` or `native`), `INTENT_ROUTER_ENABLED`, the `LANGCHAIN_*` tracing vars, and the `UPSTASH_VECTOR_*` plus `EPISODIC_MEMORY_ENABLED` flags for the L2 layer.

## Deploy to Cloud Run

```bash
cd backend
gcloud config set project sayari-demo-elie
gcloud run deploy sayari-demo-backend \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 1Gi \
  --timeout 300 \
  --min-instances 0 \
  --max-instances 5
```

For the demo window, set `--min-instances 1` to kill cold starts. The service runs as the project's default compute service account; a dedicated least-privilege runtime account isn't provisioned yet.

## Evals

See [`evals/README.md`](evals/README.md). The short version: `python -m evals.run_evals --deterministic-only` is the no-spend regression gate (67/67).
