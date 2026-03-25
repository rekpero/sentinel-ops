import React, { useState, useEffect, useCallback, useRef } from 'react'

const API = '/api'
const PAGE_SIZE = 20

// ── Status maps ──────────────────────────────────────────────────────────────
const STATUS_COLORS = {
  discovered:      '#9b78e4',
  planning:        '#e8a530',
  planning_failed: '#e04848',
  issue_created:   '#4494dd',
  writing:         '#7b7aff',
  pr_created:      '#28b4b4',
  reviewing:       '#e07830',
  ready:           '#2db882',
  completed:       '#22c55e',
  needs_human:     '#e04848',
}
const STATUS_LABELS = {
  discovered:      'Discovered',
  planning:        'Planning',
  planning_failed: 'Plan Failed',
  issue_created:   'Issue Created',
  writing:         'Writing',
  pr_created:      'PR Created',
  reviewing:       'Reviewing',
  ready:           'Ready',
  completed:       'Completed',
  needs_human:     'Needs Human',
}
const EVENT_TYPE_COLOR = {
  assistant:        '#9b78e4',
  tool_use:         '#e8a530',
  tool_result:      '#384458',
  result:           '#2db882',
  error:            '#e04848',
  system:           '#4494dd',
  user:             '#384458',
  rate_limit_event: '#e8a530',
  pipeline:         '#4494dd',
}

const PIPELINE_PHASE_CONFIG = {
  start:    { label: 'START',    color: '#4494dd' },
  search:   { label: 'SEARCH',   color: '#9b78e4' },
  topics:   { label: 'TOPICS',   color: '#e8a530' },
  planning: { label: 'PLANNING', color: '#7b7aff' },
  issue:    { label: 'ISSUE',    color: '#28b4b4' },
  trigger:  { label: 'TRIGGER',  color: '#2db882' },
  done:     { label: 'DONE',     color: '#22c55e' },
  error:    { label: 'ERROR',    color: '#e04848' },
}

// Pipeline stages shown in the top flow bar
const PIPELINE_STAGES = [
  { key: 'discovered', label: 'Discovered', color: '#9b78e4' },
  { key: 'writing',    label: 'Writing',    color: '#7b7aff' },
  { key: 'reviewing',  label: 'Reviewing',  color: '#e07830', pulse: true },
  { key: 'ready',      label: 'Ready',      color: '#2db882' },
  { key: 'completed',  label: 'Completed',  color: '#22c55e' },
]

// ── Utilities ────────────────────────────────────────────────────────────────
function formatToolUse(b) {
  const tool = b.name || 'unknown'
  const input = b.input || {}
  if (tool === 'Bash')      return `$ ${(input.command || '').substring(0, 120)}`
  if (tool === 'Read')      return `Read ${input.file_path || '?'}`
  if (tool === 'Edit' || tool === 'Write') return `${tool} ${input.file_path || '?'}`
  if (tool === 'Grep')      return `Grep "${input.pattern || ''}"`
  if (tool === 'Glob')      return `Glob ${input.pattern || ''}`
  if (tool === 'Skill')     return `Skill: ${input.skill || '?'}`
  if (tool === 'WebSearch') return `WebSearch: ${input.query || '?'}`
  if (tool === 'WebFetch')  return `WebFetch: ${input.url || '?'}`
  if (tool === 'Agent')     return `Agent: ${input.description || '?'}`
  return `${tool}`
}

function tryParseEventData(raw, eventType) {
  try {
    const d = typeof raw === 'string' ? JSON.parse(raw) : raw

    if (eventType === 'assistant' || d?.type === 'assistant') {
      const blocks = d.message?.content || []
      const parts = []
      for (const b of blocks) {
        if (b.type === 'text' && b.text)              parts.push(b.text)
        else if (b.type === 'tool_use')               parts.push(formatToolUse(b))
        else if (b.type === 'thinking' && b.thinking) parts.push('(thinking) ' + b.thinking)
        else if (typeof b === 'string')               parts.push(b)
      }
      return parts.join(' ') || null
    }
    if (eventType === 'user' || d?.type === 'user') return null
    if (eventType === 'tool_use' || d?.type === 'tool_use') {
      const tool = d.tool || d.name || 'unknown'
      const input = d.input || {}
      if (tool === 'Bash')      return `$ ${input.command || ''}`
      if (tool === 'Read')      return `Read ${input.file_path || '?'}`
      if (tool === 'Edit' || tool === 'Write') return `${tool} ${input.file_path || '?'}`
      if (tool === 'Grep')      return `Grep "${input.pattern || ''}"`
      if (tool === 'Glob')      return `Glob ${input.pattern || ''}`
      if (tool === 'WebSearch') return `WebSearch: ${input.query || '?'}`
      if (tool === 'WebFetch')  return `WebFetch: ${input.url || '?'}`
      if (tool === 'Agent')     return `Agent: ${input.description || '?'}`
      return `${tool}: ${JSON.stringify(input)}`
    }
    if (eventType === 'tool_result' || d?.type === 'tool_result') return null
    if (eventType === 'result' || d?.type === 'result') {
      const r = d.result
      // Try to extract structured review JSON from markdown code block
      if (typeof r === 'string') {
        const jsonMatch = r.match(/```json\s*([\s\S]*?)```/)
        if (jsonMatch) {
          try {
            const review = JSON.parse(jsonMatch[1])
            if (review.overall_score !== undefined) {
              return { __reviewResult: true, review }
            }
          } catch { /* fall through */ }
        }
        // Try to extract topic discovery JSON array
        const arrMatch = r.match(/\[[\s\S]*\]/)
        if (arrMatch) {
          try {
            const topics = JSON.parse(arrMatch[0])
            if (Array.isArray(topics) && topics[0]?.title) {
              return { __discoveryResult: true, topics }
            }
          } catch { /* fall through */ }
        }
        return r
      }
      if (r && typeof r === 'object') return JSON.stringify(r)
      return 'Agent finished'
    }
    if (eventType === 'error' || d?.type === 'error') {
      const err = d.error
      if (typeof err === 'string') return err
      if (err && err.message) return err.message
      return 'Error occurred'
    }
    if (eventType === 'system' || d?.type === 'system') {
      if (d.subtype === 'init') return `Session started in ${d.cwd || '?'}`
      return d.message || d.text || null
    }
    if (eventType === 'rate_limit_event') return 'Rate limit event'
    if (eventType === 'pipeline' || d?.type === 'pipeline') {
      // For topic discovery results with structured data, append topic list
      if (d.data?.topics && Array.isArray(d.data.topics)) {
        const topicLines = d.data.topics.map(
          (t, i) => `  ${i + 1}. ${t.title} [${(t.keywords || []).join(', ')}]`
        ).join('\n')
        return `${d.message}\n${topicLines}`
      }
      return d.message || 'Pipeline step'
    }
    return null
  } catch {
    return raw || null
  }
}

const SCORE_LABEL = {
  content_quality: 'Content',
  seo_optimization: 'SEO',
  technical_accuracy: 'Technical',
  readability: 'Readability',
  internal_linking: 'Linking',
}

function scoreColor(s) {
  if (s >= 8.5) return '#2db882'
  if (s >= 7.5) return '#e8a530'
  return '#e04848'
}

function ReviewResultCard({ review }) {
  const score = review.overall_score ?? 0
  const scores = review.scores || {}
  const improvements = review.improvements || []
  const factFlags = review.fact_check_flags || []
  const pricingIssues = review.pricing_issues || []
  const links = review.link_audit || []
  const validLinks = links.filter(l => l.verdict === 'VALID').length
  const hasIssues = improvements.length > 0 || factFlags.length > 0 || pricingIssues.length > 0

  return (
    <div className="result-card">
      <div className="result-card-header">
        <span className="result-card-title">Review Result</span>
        <span className="result-card-score" style={{ color: scoreColor(score) }}>
          {score.toFixed(1)}/10
        </span>
      </div>

      <div className="result-card-scores">
        {Object.entries(scores).map(([k, v]) => (
          <span key={k} className="result-card-score-item">
            <span className="result-card-score-label">{SCORE_LABEL[k] || k}</span>
            <span style={{ color: scoreColor(v) }}>{v}</span>
          </span>
        ))}
      </div>

      {review.summary && (
        <div className="result-card-summary">{review.summary}</div>
      )}

      {!hasIssues ? (
        <div className="result-card-clear">No issues found - blog is ready to merge</div>
      ) : (
        <div className="result-card-issues">
          {improvements.map((imp, i) => (
            <div key={`imp-${i}`} className="result-card-issue">
              <span className={`result-card-severity sev-${imp.severity || 'low'}`}>
                {(imp.severity || 'low').toUpperCase()}
              </span>
              <span>{imp.description}</span>
            </div>
          ))}
          {factFlags.map((ff, i) => (
            <div key={`ff-${i}`} className="result-card-issue">
              <span className="result-card-severity sev-fact">FACT</span>
              <span>{ff.claim || ff.concern || ff.description}</span>
            </div>
          ))}
          {pricingIssues.map((pi, i) => (
            <div key={`pi-${i}`} className="result-card-issue">
              <span className="result-card-severity sev-pricing">PRICE</span>
              <span>{pi.description || pi.claim || JSON.stringify(pi)}</span>
            </div>
          ))}
        </div>
      )}

      {links.length > 0 && (
        <div className="result-card-links">
          Links verified: {validLinks}/{links.length} valid
        </div>
      )}
    </div>
  )
}

function DiscoveryResultCard({ topics }) {
  return (
    <div className="result-card">
      <div className="result-card-header">
        <span className="result-card-title">Discovery Result</span>
        <span style={{ color: '#e8a530' }}>{topics.length} topic(s)</span>
      </div>
      {topics.map((t, i) => (
        <div key={i} className="result-card-topic">
          <span className="result-card-topic-num">{i + 1}.</span>
          <div>
            <div className="result-card-topic-title">{t.title}</div>
            {t.target_keywords?.length > 0 && (
              <div className="result-card-topic-kw">
                {t.target_keywords.join(' / ')}
              </div>
            )}
            {t.why_it_ranks && (
              <div className="result-card-topic-why">{t.why_it_ranks}</div>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

// SQLite stores datetimes as "2026-03-22 23:10:02" (UTC, no timezone suffix).
function parseUTC(ts) {
  if (!ts) return null
  const s = String(ts).trim()
  if (s.endsWith('Z') || s.includes('+') || /[+-]\d{2}:\d{2}$/.test(s)) return new Date(s)
  return new Date(s.replace(' ', 'T') + 'Z')
}
function fmtDateTime(ts) {
  if (!ts) return '-'
  try { return parseUTC(ts).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) } catch { return '-' }
}
function fmtDate(ts) {
  if (!ts) return '-'
  try { return parseUTC(ts).toLocaleDateString() } catch { return '-' }
}
function formatLogTime(ts) {
  if (!ts) return ''
  try { return parseUTC(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) } catch { return '' }
}

// ── StatusBadge ──────────────────────────────────────────────────────────────
function StatusBadge({ status }) {
  const color   = STATUS_COLORS[status] || '#64748b'
  const label   = STATUS_LABELS[status] || status
  const pulsing = ['reviewing', 'writing', 'planning'].includes(status)
  return (
    <span className="status-badge">
      <span
        className={`status-dot${pulsing ? ' is-pulsing' : ''}`}
        style={{ background: color }}
      />
      <span style={{ color }}>{label}</span>
    </span>
  )
}

// ── PipelineFlow ─────────────────────────────────────────────────────────────
function PipelineFlow({ stats }) {
  return (
    <div className="pipeline">
      {PIPELINE_STAGES.map((stage, i) => {
        const count  = stats[stage.key] || 0
        const active = count > 0
        return (
          <React.Fragment key={stage.key}>
            <div className="pipeline-stage" style={{ opacity: active ? 1 : 0.3 }}>
              <div
                className="pipeline-dot"
                style={{
                  background: active ? stage.color : '#384458',
                  animation: stage.pulse && active ? 'pulse 2s ease infinite' : 'none',
                }}
              />
              <span className="pipeline-label">{stage.label}</span>
              <span className="pipeline-count" style={{ color: active ? stage.color : '#384458' }}>
                {count}
              </span>
            </div>
            {i < PIPELINE_STAGES.length - 1 && (
              <span className="pipeline-divider">·</span>
            )}
          </React.Fragment>
        )
      })}
    </div>
  )
}

// ── ReviewLogViewer ──────────────────────────────────────────────────────────
function ReviewLogViewer({ runId, agentType, prNumber, onClose }) {
  const [allEvents, setAllEvents]         = useState([])
  const allEventsRef                      = useRef([])
  const [runStatus, setRunStatus]         = useState('running')
  const [finishedAt, setFinishedAt]       = useState(null)
  const [isInitialLoading, setInitialLoading] = useState(true)
  const cursorRef                         = useRef(0)
  const containerRef                      = useRef(null)
  const isRunning                         = runStatus === 'running'

  const fetchLogs = useCallback(async () => {
    try {
      const res  = await fetch(`${API}/runs/${runId}/logs?since=${cursorRef.current}`)
      const data = await res.json()
      if (data.events?.length > 0) {
        const existingIds = new Set(allEventsRef.current.map(e => e.id))
        const newEvents   = data.events.filter(e => !existingIds.has(e.id))
        if (newEvents.length > 0) {
          const merged = [...allEventsRef.current, ...newEvents]
          allEventsRef.current = merged
          setAllEvents(merged)
          cursorRef.current = data.events[data.events.length - 1].id
        }
      }
      setRunStatus(data.run_status || 'running')
      setFinishedAt(data.finished_at)
    } catch (e) {
      console.error('Failed to fetch logs:', e)
    } finally {
      setInitialLoading(false)
    }
  }, [runId])

  useEffect(() => {
    cursorRef.current = 0
    allEventsRef.current = []
    setAllEvents([])
    setRunStatus('running')
    setFinishedAt(null)
    setInitialLoading(true)
    fetchLogs()
  }, [runId])

  useEffect(() => {
    if (!isRunning) return
    const interval = setInterval(fetchLogs, 2000)
    return () => clearInterval(interval)
  }, [fetchLogs, isRunning])

  useEffect(() => {
    const el = containerRef.current
    if (!isRunning || !el) return
    const nearBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 50
    if (nearBottom) el.scrollTop = el.scrollHeight
  }, [allEvents, isRunning])

  const formattedEvents = allEvents.slice(-500)
    .map(event => {
      const summary = tryParseEventData(event.event_data, event.event_type)
      if (!summary) return null
      return { ...event, summary }
    })
    .filter(Boolean)

  const statusColor = runStatus === 'completed' ? '#2db882'
    : runStatus === 'error' ? '#e04848'
    : runStatus === 'cancelled' || runStatus === 'stopped' ? '#8591a8'
    : '#e8a530'

  const titleLabel = prNumber
    ? `PR #${prNumber}`
    : agentType === 'discovery'
      ? 'Discovery'
      : `Run #${runId}`

  return (
    <div className="log-panel">
      <div className="log-panel-header">
        <div className="log-panel-meta">
          <span className="log-panel-title">{titleLabel}</span>
          <span className={`type-tag ${agentType}`}>{agentType}</span>
          <span className="log-panel-status" style={{ color: statusColor }}>
            {isRunning ? '● running' : runStatus}
          </span>
          <span className="log-panel-count">{formattedEvents.length} events</span>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={onClose}>
          ✕ Close
        </button>
      </div>

      <div ref={containerRef} className="log-body">
        {isInitialLoading ? (
          <span style={{ color: 'var(--t3)' }}>Loading...</span>
        ) : formattedEvents.length === 0 ? (
          <span style={{ color: 'var(--t3)' }}>
            {isRunning ? 'Waiting for events...' : 'No events recorded.'}
          </span>
        ) : (
          formattedEvents.map(event => {
            const pipelineConf = event.event_type === 'pipeline'
              ? PIPELINE_PHASE_CONFIG[event.phase] || { label: event.phase?.toUpperCase(), color: '#4494dd' }
              : null
            const reviewPhaseColor = event.phase === 'editorial' ? '#e07830'
              : event.phase === 'fact_check' ? '#28b4b4'
              : event.phase === 'discovery' ? '#9b78e4'
              : null

            // Structured result cards
            if (event.summary?.__reviewResult) {
              return (
                <div key={event.id} className="log-entry log-entry-result">
                  <span className="log-time">{formatLogTime(event.created_at)}</span>
                  <ReviewResultCard review={event.summary.review} />
                </div>
              )
            }
            if (event.summary?.__discoveryResult) {
              return (
                <div key={event.id} className="log-entry log-entry-result">
                  <span className="log-time">{formatLogTime(event.created_at)}</span>
                  <DiscoveryResultCard topics={event.summary.topics} />
                </div>
              )
            }

            return (
              <div key={event.id} className={`log-entry${pipelineConf ? ' log-entry-pipeline' : ''}`}>
                <span className="log-time">{formatLogTime(event.created_at)}</span>
                {pipelineConf ? (
                  <span className="log-phase-tag" style={{ color: pipelineConf.color }}>
                    {pipelineConf.label}
                  </span>
                ) : event.phase && event.phase !== 'general' && reviewPhaseColor ? (
                  <span className="log-phase" style={{ color: reviewPhaseColor }}>
                    [{event.phase}]
                  </span>
                ) : null}
                {!pipelineConf && (
                  <span
                    className="log-type"
                    style={{ color: EVENT_TYPE_COLOR[event.event_type] || '#384458' }}
                  >
                    {event.event_type}
                  </span>
                )}
                <span className={pipelineConf ? 'log-pipeline-msg' : ''}>
                  {typeof event.summary === 'string' ? event.summary : JSON.stringify(event.summary)}
                </span>
              </div>
            )
          })
        )}
        {isRunning && formattedEvents.length > 0 && (
          <div className="log-cursor">▋</div>
        )}
      </div>

      <div className="log-panel-footer">
        <span>
          {isRunning
            ? 'Polling every 2s...'
            : finishedAt
              ? `Finished ${fmtDateTime(finishedAt)}`
              : `Status: ${runStatus}`}
        </span>
        <span>run #{runId}</span>
      </div>
    </div>
  )
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function Sidebar({ activeTab, setActiveTab, onTrigger, topics, runs }) {
  const activeTopicCount = topics.filter(t => !t.pr_number).length
  const blogPrCount  = topics.filter(t => t.pr_number).length
  const runningCount = runs.filter(r => r.status === 'running').length

  const navItems = [
    { key: 'overview', label: 'Blog Topics', count: activeTopicCount },
    { key: 'prs',      label: 'Blog PRs',    count: blogPrCount },
    { key: 'runs',     label: 'Agent Runs',  count: runningCount },
  ]

  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <div className="sidebar-brand-mark">
          <svg viewBox="0 0 16 16">
            <path d="M8 1L2 3.5V8c0 3.2 2.4 5.6 6 6.5 3.6-.9 6-3.3 6-6.5V3.5L8 1z" />
          </svg>
        </div>
        <div className="sidebar-brand-text">
          <h1>Sentinel</h1>
          <p>Blog Pipeline</p>
        </div>
      </div>

      <nav className="sidebar-nav">
        <div className="nav-label">Views</div>
        {navItems.map(item => (
          <button
            key={item.key}
            className={`nav-item${activeTab === item.key ? ' active' : ''}`}
            onClick={() => setActiveTab(item.key)}
          >
            <span>{item.label}</span>
            {item.count > 0 && (
              <span className="nav-count">{item.count}</span>
            )}
          </button>
        ))}
      </nav>

      <div className="sidebar-actions">
        <div className="sidebar-actions-label">Actions</div>
        <button
          className="btn btn-amber btn-full"
          onClick={() => onTrigger('discovery', 'Discovery')}
        >
          Run Discovery
        </button>
        <button
          className="btn btn-ghost btn-full"
          onClick={() => onTrigger('review-poll', 'Review Poll')}
        >
          Poll Reviews
        </button>
      </div>
    </aside>
  )
}

// ── App ───────────────────────────────────────────────────────────────────────
function App() {
  const [stats, setStats]               = useState({})
  const [topics, setTopics]             = useState([])
  const [runs, setRuns]                 = useState([])
  const [runsTotal, setRunsTotal]       = useState(0)
  const [runsPage, setRunsPage]         = useState(0)
  const runsPageRef                     = useRef(0)
  const [loading, setLoading]           = useState(true)
  const [activeTab, setActiveTab]       = useState('overview')
  const [triggerMsg, setTriggerMsg]     = useState('')
  const [activeReview, setActiveReview] = useState(null)

  const fetchRunsForPage = useCallback(async (page) => {
    try {
      const data = await fetch(`${API}/runs?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`).then(r => r.json())
      setRuns(data.runs || [])
      setRunsTotal(data.total || 0)
    } catch (e) {
      console.error('Failed to fetch runs:', e)
    }
  }, [])

  const fetchData = useCallback(async () => {
    try {
      const [statsRes, topicsRes] = await Promise.all([
        fetch(`${API}/stats`).then(r => r.json()),
        fetch(`${API}/topics?limit=50`).then(r => r.json()),
      ])
      setStats(statsRes)
      setTopics(topicsRes)
      fetchRunsForPage(runsPageRef.current)
    } catch (e) {
      console.error('Failed to fetch data:', e)
    } finally {
      setLoading(false)
    }
  }, [fetchRunsForPage])

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 30000)
    return () => clearInterval(interval)
  }, [fetchData])

  // Fetch runs when page changes (skip on initial render since fetchData handles it)
  const isFirstRender = useRef(true)
  useEffect(() => {
    if (isFirstRender.current) { isFirstRender.current = false; return }
    runsPageRef.current = runsPage
    fetchRunsForPage(runsPage)
  }, [runsPage, fetchRunsForPage])

  const trigger = async (endpoint, label) => {
    setTriggerMsg(`Starting ${label}...`)
    try {
      const res  = await fetch(`${API}/trigger/${endpoint}`, { method: 'POST' })
      const data = await res.json()
      setTriggerMsg(data.message || `${label} triggered`)
      setTimeout(() => setTriggerMsg(''), 5000)
      setTimeout(fetchData, 3000)
      return data
    } catch (e) {
      setTriggerMsg(`Failed: ${e.message}`)
      setTimeout(() => setTriggerMsg(''), 5000)
      return null
    }
  }

  const triggerReview = async (prNumber) => {
    const data = await trigger(`review/${prNumber}`, `Review PR #${prNumber}`)
    if (data?.run_id) {
      setActiveReview({ pr_number: prNumber, run_id: data.run_id, agent_type: 'reviewer' })
      setActiveTab('prs')
    }
  }

  if (loading) {
    return (
      <div className="loading-screen">
        <div className="loading-spinner" />
        <span>Loading Sentinel...</span>
      </div>
    )
  }

  const GITHUB_REPO = window.__REPO || 'spheron-core/landing-site'

  // Topics without a PR yet - discovered by the agent, waiting to become blogs
  const discoveryTopics = topics.filter(t => !t.pr_number)
  // Topics that have a PR number - active in write/review pipeline
  const blogPrTopics = topics.filter(t => t.pr_number)

  const totalRunPages = Math.ceil(runsTotal / PAGE_SIZE)

  return (
    <div className="layout">
      <Sidebar
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        onTrigger={trigger}
        topics={topics}
        runs={runs}
      />

      <main className="main">
        <PipelineFlow stats={stats} />

        {triggerMsg && <div className="toast">{triggerMsg}</div>}

        {/* ── Blog Topics ─────────────────────────────────────────── */}
        {activeTab === 'overview' && (
          <>
            <div className="page-header">
              <div>
                <div className="page-title">Blog Topics</div>
                <div className="page-subtitle">
                  {discoveryTopics.length} topic{discoveryTopics.length !== 1 ? 's' : ''} pending - discovered, not yet written
                </div>
              </div>
            </div>
            <div className="table-wrap">
              <div className="table-outer">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Title</th>
                      <th>Status</th>
                      <th>Issue</th>
                      <th>Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {discoveryTopics.length === 0 ? (
                      <tr>
                        <td className="empty-cell" colSpan={4}>
                          <div className="empty-state">
                            <div className="empty-title">No pending topics</div>
                            <div className="empty-body">
                              Click "Run Discovery" to find new blog topics. Topics move to Blog PRs once writing begins.
                            </div>
                          </div>
                        </td>
                      </tr>
                    ) : discoveryTopics.map(t => (
                      <tr key={t.id}>
                        <td className="col-clamp-wide">{t.title}</td>
                        <td><StatusBadge status={t.status} /></td>
                        <td>
                          {t.issue_number
                            ? <a className="data-link" href={`https://github.com/${GITHUB_REPO}/issues/${t.issue_number}`} target="_blank" rel="noopener">#{t.issue_number}</a>
                            : <span className="col-muted">-</span>}
                        </td>
                        <td className="col-muted">{fmtDate(t.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}

        {/* ── Blog PRs ────────────────────────────────────────────── */}
        {activeTab === 'prs' && (
          <>
            <div className="page-header">
              <div>
                <div className="page-title">Blog PRs</div>
                <div className="page-subtitle">
                  {blogPrTopics.length} PR{blogPrTopics.length !== 1 ? 's' : ''} tracked
                </div>
              </div>
            </div>
            <div className="table-wrap">
              <div className="table-outer">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Title</th>
                      <th>Status</th>
                      <th>Score</th>
                      <th>PR</th>
                      <th>Iter.</th>
                      <th>Created</th>
                      <th>Updated</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {blogPrTopics.length === 0 ? (
                      <tr>
                        <td className="empty-cell" colSpan={8}>
                          <div className="empty-state">
                            <div className="empty-title">No blog PRs yet</div>
                            <div className="empty-body">
                              Topics with open PRs will appear here once writing begins.
                            </div>
                          </div>
                        </td>
                      </tr>
                    ) : blogPrTopics.map(t => {
                      const isActive = activeReview?.pr_number === t.pr_number
                      return (
                        <tr key={t.id} className={isActive ? 'is-active' : ''}>
                          <td className="col-clamp-wide">{t.title}</td>
                          <td><StatusBadge status={t.status} /></td>
                          <td>
                            {t.review_score ? (
                              <span className={`score ${
                                t.review_score >= 7.5 ? 'score-hi' :
                                t.review_score >= 5   ? 'score-mid' : 'score-lo'
                              }`}>
                                {t.review_score}/10
                              </span>
                            ) : <span className="col-muted">-</span>}
                          </td>
                          <td>
                            <a className="data-link" href={`https://github.com/${GITHUB_REPO}/pull/${t.pr_number}`} target="_blank" rel="noopener">
                              #{t.pr_number}
                            </a>
                          </td>
                          <td className="col-muted">{t.review_iterations || 0}</td>
                          <td className="col-muted">{fmtDate(t.created_at)}</td>
                          <td className="col-muted">{fmtDateTime(t.updated_at)}</td>
                          <td>
                            <div style={{ display: 'flex', gap: 6 }}>
                              <button
                                onClick={() => triggerReview(t.pr_number)}
                                className={`btn btn-review btn-sm${isActive ? ' is-active' : ''}`}
                              >
                                {isActive ? '● Reviewing' : 'Review'}
                              </button>
                              {isActive && (
                                <button
                                  onClick={() => setActiveReview(null)}
                                  className="btn btn-ghost btn-sm"
                                >
                                  Hide
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}

        {/* ── Agent Runs ──────────────────────────────────────────── */}
        {activeTab === 'runs' && (
          <>
            <div className="page-header">
              <div>
                <div className="page-title">Agent Runs</div>
                <div className="page-subtitle">{runsTotal} total executions</div>
              </div>
            </div>
            <div className="table-wrap">
              <div className="table-outer">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>Type</th>
                      <th>Status</th>
                      <th>PR</th>
                      <th>Started</th>
                      <th>Finished</th>
                      <th>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.length === 0 ? (
                      <tr>
                        <td className="empty-cell" colSpan={7}>
                          <div className="empty-state">
                            <div className="empty-title">No agent runs yet</div>
                            <div className="empty-body">
                              Discovery and review runs will appear here.
                            </div>
                          </div>
                        </td>
                      </tr>
                    ) : runs.map(run => {
                      const isActive = activeReview?.run_id === run.id
                      let prNum = null
                      try { prNum = run.result ? JSON.parse(run.result).pr_number : null } catch {}
                      const statusClass = run.status === 'completed' ? 'run-completed'
                        : run.status === 'running' ? 'run-running'
                        : run.status === 'stopped' || run.status === 'cancelled' ? 'run-stopped'
                        : 'run-error'
                      return (
                        <tr key={run.id} className={isActive ? 'is-active' : ''}>
                          <td className="col-muted">#{run.id}</td>
                          <td>
                            <span className={`type-tag ${run.agent_type}`}>{run.agent_type}</span>
                          </td>
                          <td>
                            <span className={statusClass}>
                              {run.status === 'running' ? '● ' : ''}{run.status}
                            </span>
                          </td>
                          <td>
                            {prNum
                              ? <a className="data-link" href={`https://github.com/${GITHUB_REPO}/pull/${prNum}`} target="_blank" rel="noopener">#{prNum}</a>
                              : <span className="col-muted">-</span>}
                          </td>
                          <td className="col-muted">{fmtDateTime(run.started_at)}</td>
                          <td className="col-muted">{fmtDateTime(run.finished_at)}</td>
                          <td>
                            <div style={{ display: 'flex', gap: 6 }}>
                              <button
                                onClick={() => setActiveReview(
                                  isActive
                                    ? null
                                    : { pr_number: prNum, run_id: run.id, agent_type: run.agent_type }
                                )}
                                className={`btn btn-sm${isActive ? ' btn-logs is-active' : ' btn-logs'}`}
                              >
                                {isActive ? 'Viewing' : 'Logs'}
                              </button>
                              {run.status === 'running' && (
                                <button
                                  className="btn btn-sm btn-stop"
                                  onClick={async () => {
                                    try {
                                      await fetch(`${API}/runs/${run.id}/stop`, { method: 'POST' })
                                      fetchData()
                                    } catch (e) { console.error('Stop failed:', e) }
                                  }}
                                >
                                  Stop
                                </button>
                              )}
                              {['completed', 'error', 'stopped', 'cancelled'].includes(run.status) && (
                                <button
                                  className="btn btn-sm btn-restart"
                                  onClick={async () => {
                                    try {
                                      const res = await fetch(`${API}/runs/${run.id}/restart`, { method: 'POST' })
                                      const data = await res.json()
                                      if (data.run_id) {
                                        setActiveReview({ pr_number: prNum, run_id: data.run_id, agent_type: run.agent_type })
                                      }
                                      setTimeout(fetchData, 2000)
                                    } catch (e) { console.error('Restart failed:', e) }
                                  }}
                                >
                                  Restart
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>

            {totalRunPages > 1 && (
              <div className="pagination">
                <span className="pagination-info">
                  {runsPage * PAGE_SIZE + 1}-{Math.min((runsPage + 1) * PAGE_SIZE, runsTotal)} of {runsTotal}
                </span>
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={runsPage === 0}
                  onClick={() => setRunsPage(p => p - 1)}
                >
                  Prev
                </button>
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={runsPage >= totalRunPages - 1}
                  onClick={() => setRunsPage(p => p + 1)}
                >
                  Next
                </button>
              </div>
            )}
          </>
        )}

        <div className="app-footer">
          <span>Sentinel v1.0.0</span>
          <span>Auto-refreshes every 30s</span>
        </div>
      </main>

      {activeReview && (
        <ReviewLogViewer
          runId={activeReview.run_id}
          agentType={activeReview.agent_type}
          prNumber={activeReview.pr_number}
          onClose={() => setActiveReview(null)}
        />
      )}
    </div>
  )
}

export default App
