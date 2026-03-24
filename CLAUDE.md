# Sentinel

Automated blog pipeline guardian. Discovers trending SEO topics, orchestrates writing via SwarmOps, reviews PRs with editorial/fact-check/pricing verification, and iterates until blogs are ready to merge.

Built with Python (FastAPI) backend and React (Vite) frontend.

## Architecture

```
sentinel/
  backend/
    agents/
      discovery.py      # Daily SEO topic discovery + SwarmOps plan/issue creation
      reviewer.py       # PR review: editorial, fact-check, pricing, iteration loop
    services/
      github_service.py     # GitHub REST + GraphQL API (issues, PRs, line comments)
      claude_service.py     # Claude Code CLI subprocess spawner
      pricing_service.py    # Spheron GPU pricing API client
      swarmops_service.py   # SwarmOps orchestrator API client
    db/
      database.py       # SQLite via aiosqlite (topics, reviews, pricing_checks, agent_runs)
    scheduler/
      cron.py           # APScheduler (daily discovery cron + PR poll interval)
    config.py           # All env-driven configuration - never hardcode values here
    main.py             # FastAPI app, lifespan, API routes, SPA serving
  frontend/
    src/
      App.jsx           # React dashboard (topics table, PR list, agent runs, triggers)
      main.jsx          # React entry point
    index.html          # SPA shell
    vite.config.js      # Vite config with API proxy to backend
  .env.example          # All configurable env vars with defaults
  run.sh                # CLI: install, build-ui, start, dev, stop, status, logs
```

## Tech Stack

- **Backend**: Python 3.10+, FastAPI, uvicorn, httpx, aiosqlite, APScheduler, python-dotenv
- **Frontend**: React 18, Vite 5 (install with `bun` when available, fallback to `npm`)
- **Database**: SQLite (file-based, path configurable via `DB_PATH` or `DATA_DIR` env var)
- **External integrations**: GitHub API, SwarmOps API, Spheron Pricing API, Claude Code CLI

## Critical Rules

### Content Rules
- NEVER use emdashes (-- or unicode em/en dashes) in any generated content, comments, or suggestions. Always use hyphens (-) instead. The `_strip_emdashes()` method in `reviewer.py` enforces this.
- All GPU pricing mentioned in blogs must be verified against the live Spheron pricing API at `https://app.spheron.ai/api/gpu-offers`. Always show both on-demand and spot pricing.
- Always include a disclaimer that pricing is based on the current date and can fluctuate based on GPU availability.
- Cross-link to `docs.spheron.ai` wherever technical concepts are mentioned.
- Cross-link to existing blog posts on `spheron.network/blog/` where relevant.

### Link Verification Rules (CRITICAL)
- The reviewer agent MUST NOT suggest a specific URL in review comments unless it has fetched that URL with WebFetch and confirmed: (a) the page exists, AND (b) the content is actually relevant to the context where it would be inserted.
- If a link cannot be verified, the reviewer must flag it with: "NOTE TO AGENT: Before adding this link, use WebFetch to fetch [URL] and confirm the page exists and is relevant to the surrounding content."
- The reviewer must never fabricate or guess doc/blog URLs. If unsure of the exact URL, describe the concept to link to and let the writing agent find the right URL.
- Existing Spheron blog slugs in `existing_blogs_context` are candidates only - they must still be WebFetched to confirm relevance before being suggested as cross-links.
- Rationale: the SwarmOps writing agent blindly trusts review suggestions. A broken or irrelevant link in a review comment will be inserted into the published blog.

### GitHub Comment Rules
- **Resolvable line comments** (via `create_pr_review_comment`): Used for actionable feedback that SwarmOps should fix. These are tied to specific file lines and can be resolved after fixes are applied. This is how SwarmOps picks up work.
- **General PR comments** (via `add_pr_comment`): Used for tagging humans (e.g., `@rekpero`) and status updates. These cannot be resolved. Never use these for SwarmOps-actionable feedback.
- Line comments MUST target an actual file line in the PR diff. Use the first blog file's path and a valid line number.

### Configuration Rules
- All settings come from environment variables via `config.py`. Never hardcode repo names, URLs, tokens, or usernames in agent code.
- The project is designed to be general-purpose. `GITHUB_REPO` can point to any repository.
- Tokens: `GITHUB_TOKEN`, `CLAUDE_SETUP_TOKEN`, `SWARMOPS_API_KEY` - all required for full operation.

### SwarmOps API Integration
- Auth: `SWARMOPS_API_KEY` is sent as a `Bearer` token in the `Authorization` header.
- Planning flow: `POST /api/planning` with `{ workspace_id, message }` -> poll `GET /api/planning/{session_id}` until `generating==false` -> `POST /api/planning/{session_id}/create-issue` to get `issue_number`.
- The plan content is the last message with `role=="assistant"` in the `messages` array. Only read it when `generating==false`.
- `session.status` stays `"active"` after plan generation. It only becomes `"completed"` after `create-issue` succeeds. Use `generating==false` to know when the plan is ready.
- Workspaces: `GET /api/workspaces` returns `{ "workspaces": [...] }` with `id` and `github_repo` fields.
- Issue creation returns `{ issue_number, issue_url, title }`. The issue gets the configured `ISSUE_LABEL` (default: `agent`) automatically.
- Refinement: `POST /api/planning/{id}/messages` with `{ message }` re-triggers generation. Returns 409 if already generating.
- Cancel: `POST /api/planning/{id}/cancel`. Delete: `DELETE /api/planning/{id}`.

### Claude Code Integration
- Claude Code is spawned via CLI subprocess (`claude --print --max-turns N --output-format json`).
- The `CLAUDE_SETUP_TOKEN` env var is passed to the subprocess environment.
- Always specify `--allowedTools` to restrict what tools Claude Code can use for each task type.
- Discovery agent tools: `WebFetch`, `WebSearch`
- Review agent tools: `Read`, `Glob`, `Grep`, `WebFetch`, `WebSearch`
- Timeout is 600 seconds (10 minutes) per Claude Code invocation.

## Pipeline Flow

1. **Discovery** (daily cron): Claude Code searches for trending SEO topics related to GPU cloud, LLM deployment, frameworks, competitor gaps. Deduplicates against existing blogs in the repo and database. Creates planning sessions in SwarmOps, then creates GitHub issues with the `agent` label, then comments the trigger mention to start writing.

2. **Writing** (handled by SwarmOps): SwarmOps picks up the triggered issue and writes the blog. Creates a PR labeled `blog` when done.

3. **Review** (polls every `PR_POLL_INTERVAL_SECONDS`): Detects new/updated PRs with the `blog` label. Runs three checks in parallel: editorial review (5-category scoring), fact-check (web search verification), pricing validation (Spheron API comparison). Posts resolvable line comments for SwarmOps to act on.

4. **Iteration**: Monitors commits on the PR. After SwarmOps pushes fixes, re-runs the review. Continues until the blog scores above `MIN_ACCEPTABLE_SCORE` (7.5/10) and has no outstanding issues, or hits `MAX_REVIEW_ITERATIONS` (5) and escalates to human.

5. **Completion**: Resolves all review comments, tags `@GITHUB_REVIEWER_USERNAME` in a general PR comment that the blog is ready to merge.

## Database Schema

Four tables in SQLite:
- `blog_topics`: id, title, target_keywords (JSON), status, issue_number, pr_number, review_score, review_iterations, created_at, updated_at
- `review_logs`: id, blog_topic_id, pr_number, iteration, review_type, score, review_data (JSON), comment_ids (JSON)
- `pricing_checks`: id, blog_topic_id, pr_number, pricing_data (JSON), mismatches (JSON)
- `agent_runs`: id, agent_type (discovery|reviewer), status, started_at, finished_at, result (JSON), error

Topic statuses: `discovered` -> `planning` -> `issue_created` -> `writing` -> `pr_created` -> `reviewing` -> `ready` -> `completed` (or `needs_human` / `planning_failed`)

## API Endpoints

- `GET /api/health` - Health check
- `GET /api/stats` - Pipeline statistics
- `GET /api/topics` - List topics (optional `?status=` filter)
- `GET /api/topics/{id}` - Single topic
- `POST /api/topics` - Manually create a topic
- `GET /api/reviews/{pr_number}` - Review logs for a PR
- `GET /api/pricing` - Current Spheron GPU pricing
- `POST /api/trigger/discovery` - Manually trigger discovery
- `POST /api/trigger/review/{pr_number}` - Manually trigger review for a PR
- `POST /api/trigger/review-poll` - Manually trigger PR polling
- `GET /api/prs` - List open blog PRs
- `GET /api/runs` - Recent agent run history

## Development

```bash
# Setup
cp .env.example .env    # Configure tokens
./run.sh install        # Install Python + frontend deps
./run.sh dev            # Start backend (port 8500) + frontend dev server (port 5173)

# Production
./run.sh build-ui       # Build React frontend
./run.sh start          # Start server on port 8500
./run.sh start-bg       # Start in background
./run.sh stop           # Stop background server
./run.sh logs           # Tail server logs
```

## Testing Changes

When modifying the review agent:
- Verify line comments post correctly by testing against an actual PR with `POST /api/trigger/review/{pr_number}`
- Check that emdash stripping works on all generated content
- Verify pricing API parsing handles edge cases (missing GPU types, null prices)

When modifying the discovery agent:
- Verify deduplication works against both the database and repo filesystem
- Test SwarmOps planning session creation and polling
- Verify the trigger comment format matches what SwarmOps expects (`@claude-swarmops work on this`)

When modifying the frontend:
- Run `bun run build` (or `npx vite build`) and verify the built output serves correctly from the FastAPI backend
- Test the API proxy in dev mode (`vite.config.js` proxies `/api` to `localhost:8500`)

## Common Issues

- **SQLite disk I/O error**: The `DB_PATH` must point to a writable filesystem. Set `DATA_DIR` or `DB_PATH` env var to a writable location (e.g., `/tmp/sentinel/`).
- **Claude Code not found**: Ensure the `claude` CLI is installed and available in PATH, or set `CLAUDE_CMD` to the full path.
- **GitHub rate limiting**: The PR poll interval defaults to 120 seconds. Increase `PR_POLL_INTERVAL_SECONDS` if hitting rate limits.
- **SwarmOps auth**: If SwarmOps requires authentication, set `SWARMOPS_API_KEY` or use the authenticate method with username/password.

## Design Context

### Users
Solo founder / operator running an automated blog pipeline. Opens the dashboard to get full situational awareness: where is each piece of content in the pipeline, are agents running, are there PRs waiting review. Emotional goal: **control and confidence** - pipeline health visible within 2 seconds.

### Brand Personality
Precise · Operational · Minimal

### Aesthetic Direction
Clean SaaS / Vercel-like. Very dark almost-black backgrounds with subtle cool-blue tint. Amber/gold (`#e8a530`) as the single accent color - reserved exclusively for interactive and active states, never decorative. Status colors (success/warning/error/info) are separate from the brand accent.

Anti-patterns to avoid:
- No purple-to-cyan gradients or neon accents
- No glassmorphism, glow effects
- No identical stat cards with big numbers (hero metric layout)
- No rounded elements with colored border on one side

Fonts: **Syne** (brand/display) · **DM Sans** (UI body) · **JetBrains Mono** (terminal/logs only)
Theme: Dark only.

### Design Principles
1. **Density with clarity** - Maximum useful information without confusion. Every element earns its place.
2. **Amber as signal** - Accent reserved for interactive/active states only. Never decorative.
3. **Flow visibility** - Pipeline status always visible at the top of the page.
4. **Quiet by default** - UI is calm/understated. Animations and colors activate only when relevant.
5. **Trust through precision** - Every value, timestamp, and status is exact. No vague states.
