import { useEffect, useState } from 'react'

import { getRun } from '../api'
import { errorMessage } from '../errors'
import { label } from '../report'
import type { RunProgress } from '../types'
import { Notice } from './Notice'
import { ReportPage } from './ReportPage'

const POLL_MS = 1000
const TERMINAL = new Set(['done', 'failed'])

export function RunPage({
  runId,
  onNewTender,
}: {
  runId: string
  onNewTender: () => void
}) {
  const [run, setRun] = useState<RunProgress | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const next = await getRun(runId)
        if (cancelled) return
        setRun(next)
        setError(null)
        if (!TERMINAL.has(next.status)) {
          timer = window.setTimeout(tick, POLL_MS)
        }
      } catch (cause) {
        if (!cancelled) setError(errorMessage(cause))
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [runId, attempt])

  if (run && TERMINAL.has(run.status)) {
    return <ReportPage runId={runId} onNewTender={onNewTender} />
  }

  return (
    <section className="panel">
      <h1>Matching the tender</h1>
      <p className="lede">
        Every requirement is compared against the manufacturers' documents. The report is
        served when the run stops, so a half-finished run is never read as an answer.
      </p>

      {error && (
        <Notice variant="error" title="Could not read the run">
          {error}
          <div className="actions">
            <button type="button" className="button" onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </button>
          </div>
        </Notice>
      )}

      {!run && !error && (
        <div className="loading-row">
          <span className="spinner" aria-hidden="true" /> Waiting for the first status…
        </div>
      )}

      {run && (
        <div className="stack">
          <div className="run-head">
            <span className={`pill pill-${run.status}`}>{label(run.status)}</span>
            <span className="muted">
              {run.results} of {run.expected_results} assessments
            </span>
          </div>

          <div
            className="progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(run.progress_percent)}
            aria-label="Run progress"
          >
            <div className="progress-fill" style={{ width: `${Math.min(100, run.progress_percent)}%` }} />
          </div>

          <dl className="summary-grid">
            <div>
              <dt>Progress</dt>
              <dd>{run.progress_percent.toFixed(1)}%</dd>
            </div>
            <div>
              <dt>Requirements</dt>
              <dd>{run.requirements}</dd>
            </div>
            <div>
              <dt>Manufacturers</dt>
              <dd>{run.manufacturers.join(', ')}</dd>
            </div>
          </dl>

          {run.error && (
            <Notice variant="error" title="The run stopped with an error">
              {run.error}
            </Notice>
          )}

          <p className="muted">
            This page polls the API. You can leave it open; the run continues on the server.
          </p>
        </div>
      )}
    </section>
  )
}
