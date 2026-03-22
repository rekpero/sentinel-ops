import React, { useState, useEffect, useCallback } from 'react'

const API = '/api'

const STATUS_COLORS = {
  discovered: '#8b5cf6',
  planning: '#f59e0b',
  planning_failed: '#ef4444',
  issue_created: '#3b82f6',
  writing: '#6366f1',
  pr_created: '#06b6d4',
  reviewing: '#f97316',
  ready: '#10b981',
  completed: '#22c55e',
  needs_human: '#ef4444',
}

const STATUS_LABELS = {
  discovered: 'Discovered',
  planning: 'Planning',
  planning_failed: 'Plan Failed',
  issue_created: 'Issue Created',
  writing: 'Writing',
  pr_created: 'PR Created',
  reviewing: 'Reviewing',
  ready: 'Ready to Merge',
  completed: 'Completed',
  needs_human: 'Needs Human',
}

function StatusBadge({ status }) {
  const color = STATUS_COLORS[status] || '#64748b'
  const label = STATUS_LABELS[status] || status
  return (
    <span style={{
      display: 'inline-block',
      padding: '2px 10px',
      borderRadius: '12px',
      fontSize: '12px',
      fontWeight: 600,
      background: color + '22',
      color: color,
      border: `1px solid ${color}44`,
    }}>
      {label}
    </span>
  )
}

function StatCard({ label, value, color }) {
  return (
    <div style={{
      background: '#1e1e3a',
      borderRadius: '12px',
      padding: '20px',
      textAlign: 'center',
      border: '1px solid #2d2d5e',
    }}>
      <div style={{ fontSize: '32px', fontWeight: 700, color: color || '#8b5cf6' }}>{value}</div>
      <div style={{ fontSize: '13px', color: '#94a3b8', marginTop: '4px' }}>{label}</div>
    </div>
  )
}

function App() {
  const [stats, setStats] = useState({})
  const [topics, setTopics] = useState([])
  const [prs, setPrs] = useState([])
  const [runs, setRuns] = useState([])
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState('overview')
  const [triggerMsg, setTriggerMsg] = useState('')

  const fetchData = useCallback(async () => {
    try {
      const [statsRes, topicsRes, prsRes, runsRes] = await Promise.all([
        fetch(`${API}/stats`).then(r => r.json()),
        fetch(`${API}/topics?limit=50`).then(r => r.json()),
        fetch(`${API}/prs`).then(r => r.json()),
        fetch(`${API}/runs?limit=20`).then(r => r.json()),
      ])
      setStats(statsRes)
      setTopics(topicsRes)
      setPrs(prsRes)
      setRuns(runsRes)
    } catch (e) {
      console.error('Failed to fetch data:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 30000)
    return () => clearInterval(interval)
  }, [fetchData])

  const trigger = async (endpoint, label) => {
    setTriggerMsg(`Starting ${label}...`)
    try {
      const res = await fetch(`${API}/trigger/${endpoint}`, { method: 'POST' })
      const data = await res.json()
      setTriggerMsg(data.message || `${label} triggered`)
      setTimeout(() => setTriggerMsg(''), 5000)
      setTimeout(fetchData, 3000)
    } catch (e) {
      setTriggerMsg(`Failed: ${e.message}`)
      setTimeout(() => setTriggerMsg(''), 5000)
    }
  }

  if (loading) {
    return (
      <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', height: '100vh' }}>
        <div style={{ color: '#8b5cf6', fontSize: '18px' }}>Loading dashboard...</div>
      </div>
    )
  }

  return (
    <div style={{ maxWidth: '1200px', margin: '0 auto', padding: '24px' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '32px' }}>
        <div>
          <h1 style={{ fontSize: '24px', fontWeight: 700, color: '#f1f5f9' }}>Sentinel</h1>
          <p style={{ fontSize: '14px', color: '#64748b', marginTop: '4px' }}>Automated blog discovery, writing & review</p>
        </div>
        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={() => trigger('discovery', 'Discovery')}
            style={{
              padding: '8px 16px', borderRadius: '8px', border: '1px solid #8b5cf6',
              background: '#8b5cf622', color: '#8b5cf6', cursor: 'pointer', fontSize: '13px', fontWeight: 600,
            }}
          >
            Run Discovery
          </button>
          <button
            onClick={() => trigger('review-poll', 'Review Poll')}
            style={{
              padding: '8px 16px', borderRadius: '8px', border: '1px solid #06b6d4',
              background: '#06b6d422', color: '#06b6d4', cursor: 'pointer', fontSize: '13px', fontWeight: 600,
            }}
          >
            Poll Reviews
          </button>
        </div>
      </div>

      {triggerMsg && (
        <div style={{
          padding: '12px 16px', borderRadius: '8px', background: '#1e3a5f',
          color: '#7dd3fc', marginBottom: '16px', fontSize: '14px',
        }}>
          {triggerMsg}
        </div>
      )}

      {/* Stats Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '12px', marginBottom: '32px' }}>
        <StatCard label="Total Topics" value={stats.total || 0} color="#8b5cf6" />
        <StatCard label="Writing" value={stats.writing || 0} color="#6366f1" />
        <StatCard label="Reviewing" value={stats.reviewing || 0} color="#f97316" />
        <StatCard label="Ready" value={stats.ready || 0} color="#10b981" />
        <StatCard label="Completed" value={stats.completed || 0} color="#22c55e" />
        <StatCard label="Avg Score" value={stats.avg_review_score || '-'} color="#f59e0b" />
      </div>

      {/* Tabs */}
      <div style={{ display: 'flex', gap: '4px', marginBottom: '20px', borderBottom: '1px solid #2d2d5e', paddingBottom: '8px' }}>
        {['overview', 'prs', 'runs'].map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            style={{
              padding: '8px 16px', borderRadius: '6px 6px 0 0', border: 'none',
              background: activeTab === tab ? '#1e1e3a' : 'transparent',
              color: activeTab === tab ? '#f1f5f9' : '#64748b',
              cursor: 'pointer', fontSize: '14px', fontWeight: activeTab === tab ? 600 : 400,
            }}
          >
            {tab === 'overview' ? 'Blog Topics' : tab === 'prs' ? 'Open PRs' : 'Agent Runs'}
          </button>
        ))}
      </div>

      {/* Blog Topics Tab */}
      {activeTab === 'overview' && (
        <div style={{ background: '#1e1e3a', borderRadius: '12px', overflow: 'hidden', border: '1px solid #2d2d5e' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: '#16162e' }}>
                <th style={thStyle}>Title</th>
                <th style={thStyle}>Status</th>
                <th style={thStyle}>Score</th>
                <th style={thStyle}>Issue</th>
                <th style={thStyle}>PR</th>
                <th style={thStyle}>Iterations</th>
                <th style={thStyle}>Created</th>
              </tr>
            </thead>
            <tbody>
              {topics.length === 0 ? (
                <tr><td colSpan={7} style={{ ...tdStyle, textAlign: 'center', color: '#64748b' }}>
                  No topics yet. Click "Run Discovery" to find new blog topics.
                </td></tr>
              ) : topics.map(t => (
                <tr key={t.id} style={{ borderBottom: '1px solid #2d2d5e' }}>
                  <td style={{ ...tdStyle, maxWidth: '320px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {t.title}
                  </td>
                  <td style={tdStyle}><StatusBadge status={t.status} /></td>
                  <td style={tdStyle}>
                    {t.review_score ? (
                      <span style={{ color: t.review_score >= 7.5 ? '#10b981' : t.review_score >= 5 ? '#f59e0b' : '#ef4444' }}>
                        {t.review_score}/10
                      </span>
                    ) : '-'}
                  </td>
                  <td style={tdStyle}>
                    {t.issue_number ? (
                      <a href={`https://github.com/${window.__REPO || 'spheron-core/landing-site'}/issues/${t.issue_number}`}
                         target="_blank" rel="noopener" style={{ color: '#3b82f6', textDecoration: 'none' }}>
                        #{t.issue_number}
                      </a>
                    ) : '-'}
                  </td>
                  <td style={tdStyle}>
                    {t.pr_number ? (
                      <a href={`https://github.com/${window.__REPO || 'spheron-core/landing-site'}/pull/${t.pr_number}`}
                         target="_blank" rel="noopener" style={{ color: '#3b82f6', textDecoration: 'none' }}>
                        #{t.pr_number}
                      </a>
                    ) : '-'}
                  </td>
                  <td style={tdStyle}>{t.review_iterations || 0}</td>
                  <td style={{ ...tdStyle, color: '#64748b', fontSize: '12px' }}>
                    {t.created_at ? new Date(t.created_at).toLocaleDateString() : '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Open PRs Tab */}
      {activeTab === 'prs' && (
        <div style={{ background: '#1e1e3a', borderRadius: '12px', overflow: 'hidden', border: '1px solid #2d2d5e' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: '#16162e' }}>
                <th style={thStyle}>PR</th>
                <th style={thStyle}>Title</th>
                <th style={thStyle}>Author</th>
                <th style={thStyle}>Updated</th>
                <th style={thStyle}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {prs.length === 0 ? (
                <tr><td colSpan={5} style={{ ...tdStyle, textAlign: 'center', color: '#64748b' }}>
                  No open blog PRs found.
                </td></tr>
              ) : prs.map(pr => (
                <tr key={pr.number} style={{ borderBottom: '1px solid #2d2d5e' }}>
                  <td style={tdStyle}>
                    <a href={pr.html_url} target="_blank" rel="noopener" style={{ color: '#3b82f6', textDecoration: 'none' }}>
                      #{pr.number}
                    </a>
                  </td>
                  <td style={{ ...tdStyle, maxWidth: '400px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {pr.title}
                  </td>
                  <td style={tdStyle}>{pr.user}</td>
                  <td style={{ ...tdStyle, color: '#64748b', fontSize: '12px' }}>
                    {new Date(pr.updated_at).toLocaleString()}
                  </td>
                  <td style={tdStyle}>
                    <button
                      onClick={() => trigger(`review/${pr.number}`, `Review PR #${pr.number}`)}
                      style={{
                        padding: '4px 12px', borderRadius: '6px', border: '1px solid #f97316',
                        background: '#f9731622', color: '#f97316', cursor: 'pointer', fontSize: '12px',
                      }}
                    >
                      Review
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Agent Runs Tab */}
      {activeTab === 'runs' && (
        <div style={{ background: '#1e1e3a', borderRadius: '12px', overflow: 'hidden', border: '1px solid #2d2d5e' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ background: '#16162e' }}>
                <th style={thStyle}>ID</th>
                <th style={thStyle}>Type</th>
                <th style={thStyle}>Status</th>
                <th style={thStyle}>Started</th>
                <th style={thStyle}>Finished</th>
              </tr>
            </thead>
            <tbody>
              {runs.length === 0 ? (
                <tr><td colSpan={5} style={{ ...tdStyle, textAlign: 'center', color: '#64748b' }}>
                  No agent runs recorded yet.
                </td></tr>
              ) : runs.map(run => (
                <tr key={run.id} style={{ borderBottom: '1px solid #2d2d5e' }}>
                  <td style={tdStyle}>{run.id}</td>
                  <td style={tdStyle}>
                    <span style={{
                      padding: '2px 8px', borderRadius: '6px', fontSize: '12px',
                      background: run.agent_type === 'discovery' ? '#8b5cf622' : '#f9731622',
                      color: run.agent_type === 'discovery' ? '#8b5cf6' : '#f97316',
                    }}>
                      {run.agent_type}
                    </span>
                  </td>
                  <td style={tdStyle}>
                    <span style={{
                      color: run.status === 'completed' ? '#10b981' : run.status === 'running' ? '#f59e0b' : '#ef4444'
                    }}>
                      {run.status}
                    </span>
                  </td>
                  <td style={{ ...tdStyle, color: '#64748b', fontSize: '12px' }}>
                    {run.started_at ? new Date(run.started_at).toLocaleString() : '-'}
                  </td>
                  <td style={{ ...tdStyle, color: '#64748b', fontSize: '12px' }}>
                    {run.finished_at ? new Date(run.finished_at).toLocaleString() : '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Footer */}
      <div style={{ marginTop: '32px', textAlign: 'center', color: '#475569', fontSize: '12px' }}>
        Sentinel v1.0.0 - Auto-refreshes every 30s
      </div>
    </div>
  )
}

const thStyle = {
  padding: '12px 16px',
  textAlign: 'left',
  fontSize: '12px',
  fontWeight: 600,
  color: '#94a3b8',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
}

const tdStyle = {
  padding: '12px 16px',
  fontSize: '14px',
}

export default App
