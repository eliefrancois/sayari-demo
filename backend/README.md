# Backend — Entity Risk Resolver

FastAPI service exposing the investigation agent over SSE.

## Layers

1. **API layer** (`app/main.py`) — endpoints, SSE plumbing, CORS
2. **Agent layer** (`app/agent_native.py` today, `app/agent_graph.py` in Phase 2) — orchestration loop
3. **Tools layer** (`app/tools.py`) — the 5 things the agent can do
4. **Data layer** (`app/graph.py`, `app/sanctions.py`) — Neo4j Cypher + OpenSanctions HTTP

The agent layer is the only thing that changes between Phase 1 and Phase 2. Tools, data,
endpoints, and the SSE event contract are identical.

## Local run

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

## Deploy to Cloud Run

```bash
cd backend
gcloud run deploy sayari-demo-backend \
  --source . \
  --region us-central1 \
  --service-account erre-runtime@sayari-demo.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --memory 1Gi \
  --timeout 300 \
  --min-instances 0 \
  --max-instances 5
```

For the demo window, set `--min-instances 1` to eliminate cold starts.
