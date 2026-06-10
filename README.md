# Entity Risk Resolver

> Read-only investigation agent that traverses the ICIJ Offshore Leaks corporate-ownership graph,
> cross-references entities against global sanctions lists, and produces structured, sourced risk
> summaries with a live UI of the agent's reasoning.

A miniature of the Sayari Graph + Graph AI loop, built end-to-end as a portfolio project.

**Status:** under active construction. Final README, demo URL, screenshots, and walkthrough land Thursday night.

## Architecture

<img width="2680" height="2717" alt="image" src="https://github.com/user-attachments/assets/4425fb24-7516-48c6-89b5-70093519f460" />


A request starts as an analyst prompt in the Next.js frontend, where a React Flow canvas and a prompt-kit chat UI render the investigation as it happens. The frontend calls a FastAPI backend on Cloud Run over HTTP, with progress streamed back as SSE events. Each prompt first passes through a Haiku 4.5 intent router that classifies the request and narrows the toolset before the main model sees it. The investigation itself runs on a Sonnet 4.5 LangGraph agent, looping over a tool layer that hits the Sayari Graph API, the ICIJ Offshore Leaks graph in Neo4j, and OpenSanctions. Conversation state and structured investigation memory live in Upstash Redis with a 24-hour TTL, and a flag-gated episodic memory layer in Upstash Vector can recall prior investigations. The agent ends every run by calling one of two terminator tools, `submit_answer` or `submit_summary`, whose structured payloads drive the answer cards and evidence graph in the UI. LangSmith captures traces and powers the eval suite. Deploys flow through Cloud Build into Artifact Registry and out to Cloud Run, with credentials supplied by Secret Manager.

## Stack

- **Backend:** Python 3.11, FastAPI, Anthropic Claude Sonnet 4 (native tool-use API), `neo4j` driver, `httpx`, Pydantic
- **Frontend:** Next.js 14 (App Router), TypeScript, Tailwind, React Flow
- **Data:** Neo4j 4.4 with the full ICIJ Offshore Leaks database (~810K nodes)
- **Sanctions:** OpenSanctions `/match/default` API
- **Session state:** Upstash Redis
- **Hosting:** Cloud Run (backend), Vercel (frontend), DigitalOcean droplet (Neo4j)

## Local dev

See `backend/README.md` and `frontend/README.md`.

---

*Built for the Sayari interview process. Not affiliated with Sayari.*
