# Sentinel

Automated blog pipeline guardian for GPU cloud content. Discovers trending SEO topics, orchestrates writing via SwarmOps, reviews PRs with editorial/fact-check/pricing verification, and iterates until blogs are ready to merge.

## How it works

```
Discovery (daily cron)
  └── Claude Code searches for trending GPU/LLM topics
  └── Deduplicates against existing blogs + DB
  └── Creates SwarmOps planning session + GitHub issue

Writing (SwarmOps)
  └── Picks up issue, writes blog, opens PR with `blog` label

Review (polls every 2 min)
  └── Runs 3 parallel checks: editorial scoring, fact-check, pricing validation
  └── Posts resolvable line comments for SwarmOps to act on

Iteration
  └── SwarmOps pushes fixes
  └── Re-reviews until score > 8/10 or 5 iterations hit

Completion
  └── Resolves all comments, tags human reviewer for final merge
```

## Stack

- **Backend** - Python 3.10+, FastAPI, APScheduler, aiosqlite
- **Frontend** - React 18, Vite 5
- **Database** - SQLite (file-based)
- **Integrations** - GitHub API, SwarmOps, Spheron Pricing API, Claude Code CLI

## Prerequisites

- Python 3.10+
- Node.js + [bun](https://bun.sh) (preferred) or npm
- [Claude Code CLI](https://claude.ai/code) installed and available as `claude` in PATH
- GitHub personal access token with repo permissions
- SwarmOps instance + API key

## Setup

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your tokens (see Configuration below)

# 2. Install dependencies
./run.sh install

# 3. Build frontend
./run.sh build-ui

# 4. Start
./run.sh start
```

Open `http://localhost:8500` for the dashboard.

## Configuration

All settings are driven by environment variables. Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `GITHUB_TOKEN` | Yes | GitHub PAT with repo read/write |
| `CLAUDE_SETUP_TOKEN` | Yes | Token passed to Claude Code subprocess |
| `SWARMOPS_API_KEY` | Yes | Bearer token for SwarmOps API |
| `GITHUB_REPO` | Yes | Target repo in `owner/repo` format |
| `SWARMOPS_URL` | Yes | SwarmOps instance URL |
| `GITHUB_BOT_USERNAME` | Yes | GitHub username of the writing bot |
| `GITHUB_REVIEWER_USERNAME` | Yes | Human reviewer to tag on completion |
| `DISCOVERY_CRON_HOUR` | No | Hour to run daily discovery (default: 9) |
| `PR_POLL_INTERVAL_SECONDS` | No | Review poll interval (default: 120) |
| `MAX_REVIEW_ITERATIONS` | No | Max review loops before escalating (default: 5) |
| `MAX_CONCURRENT_REVIEWS` | No | Parallel PR reviews (default: 1) |
| `SERVER_PORT` | No | Server port (default: 8500) |
| `DB_PATH` | No | SQLite path (default: `./sentinel.db`) |
| `CLAUDE_CMD` | No | Path to claude CLI (default: `claude`) |

## CLI

```bash
./run.sh install      # Install Python + frontend deps
./run.sh build-ui     # Build React frontend
./run.sh start        # Start server (foreground, hot reload)
./run.sh start-bg     # Start server (background)
./run.sh restart      # Rebuild + restart (background)
./run.sh stop         # Stop background server
./run.sh status       # Check if running
./run.sh logs         # Tail server logs
./run.sh dev          # Backend + frontend dev servers with hot reload
```

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| GET | `/api/stats` | Pipeline statistics |
| GET | `/api/topics` | List topics (optional `?status=` filter) |
| GET | `/api/topics/{id}` | Single topic detail |
| POST | `/api/topics` | Manually create a topic |
| GET | `/api/prs` | List open blog PRs |
| GET | `/api/reviews/{pr_number}` | Review logs for a PR |
| GET | `/api/pricing` | Current Spheron GPU pricing |
| GET | `/api/runs` | Recent agent run history |
| POST | `/api/trigger/discovery` | Manually trigger discovery |
| POST | `/api/trigger/review/{pr_number}` | Manually trigger review for a PR |
| POST | `/api/trigger/review-poll` | Manually trigger PR polling |

## Project structure

```
sentinel/
  backend/
    agents/
      discovery.py        # SEO topic discovery + SwarmOps plan/issue creation
      reviewer.py         # PR review: editorial, fact-check, pricing, iteration
    services/
      github_service.py   # GitHub REST + GraphQL (issues, PRs, line comments)
      claude_service.py   # Claude Code CLI subprocess spawner
      pricing_service.py  # Spheron GPU pricing API client
      swarmops_service.py # SwarmOps orchestrator API client
    db/
      database.py         # SQLite schema + queries
    scheduler/
      cron.py             # APScheduler (daily discovery + PR poll)
    config.py             # All env-driven config
    main.py               # FastAPI app, routes, SPA serving
  frontend/
    src/
      App.jsx             # React dashboard
      main.jsx            # Entry point
    vite.config.js        # Vite config (proxies /api to backend)
  .env.example
  run.sh
```

## Troubleshooting

**SQLite disk I/O error** - Set `DATA_DIR` or `DB_PATH` to a writable path (e.g., `/tmp/sentinel/`).

**Claude Code not found** - Ensure `claude` is in PATH, or set `CLAUDE_CMD` to its full path.

**GitHub rate limiting** - Increase `PR_POLL_INTERVAL_SECONDS` (default 120s).

**SwarmOps auth errors** - Verify `SWARMOPS_API_KEY` matches the `API_KEYS` configured on the SwarmOps side.
