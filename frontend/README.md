# Frontend — Entity Risk Resolver

Next.js 16 (App Router) UI for the investigation agent. Three panels:
- **Chat** — user input + summary card
- **Graph** — React Flow canvas, custom nodes per ICIJ type, live build-up as the agent traverses
- **Tool-call feed** — live event log of every tool call, sanctions hit, etc.

## Local run

```bash
cd frontend
cp .env.local.example .env.local  # set NEXT_PUBLIC_BACKEND_URL
npm install
npm run dev
```

Open `http://localhost:3000`.

## Deploy to Vercel

Imported from GitHub. Root directory: `frontend/`. Env var: `NEXT_PUBLIC_BACKEND_URL` pointing at the Cloud Run URL.

## Stack

- Next.js 16 (App Router), TypeScript, React 19
- Tailwind CSS v4
- React Flow (`@xyflow/react`) for the graph canvas
- Native `EventSource` for SSE consumption
