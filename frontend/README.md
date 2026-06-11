# Frontend

Next.js app that renders the investigation as a branching canvas. Each turn is a node you can fork from, and the agent's reasoning, tool calls, and evidence graph build up live as the backend streams them over SSE.

## Run locally

```bash
cd frontend
cp .env.local.example .env.local  # set NEXT_PUBLIC_BACKEND_URL
npm install
npm run dev
```

Open `http://localhost:3000`. Point `NEXT_PUBLIC_BACKEND_URL` at your local backend (`http://localhost:8080`) or the Cloud Run URL.

## Key directories

- `app/`: App Router entry (`page.tsx`, `layout.tsx`, global styles).
- `components/canvas/`: the React Flow investigation canvas, turn nodes, tool-call blocks, group overlays.
- `components/`: the cards and panels, answer card, risk summary, entity detail, evidence graph, trade-routes map.
- `components/manager/`: the conversation history menu (list, search, delete).
- `components/ui/`: shadcn and prompt-kit primitives.
- `lib/`: the non-UI logic. `sse-client.ts` (SSE consumption), `conversation-store.ts` (state), `canvas-layout.ts` (graph layout), `report-pdf.ts` (PDF export), `types.ts`.

## Env vars

See `.env.local.example`. Only one:

- `NEXT_PUBLIC_BACKEND_URL`: the backend base URL.

## Deploy to Vercel

Imported from GitHub, root directory `frontend/`. Set `NEXT_PUBLIC_BACKEND_URL` to the Cloud Run URL. Pushes to `main` deploy automatically.

## Stack

- Next.js 16 (App Router), TypeScript, React 19
- Tailwind CSS v4
- React Flow (`@xyflow/react`) for the canvas, d3-force for layout
- Native `EventSource` for SSE
