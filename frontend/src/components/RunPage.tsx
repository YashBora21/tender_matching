import { useEffect, useState } from 'react'

import { getRun } from '../api'
import { errorMessage } from '../errors'
import { label } from '../report'
import type { RunProgress } from '../types'
import { Notice } from './Notice'
import { ReportPage } from './ReportPage'
import { Trail } from './Trail'

const POLL_MS = 1000
// The states in which the API serves a report. `rate_limited` is one of them:
// the run stopped short, but the pairs it did answer are stored and the report
// says how far it got, so a limit is a result to read rather than a wait.
const TERMINAL = new Set(['done', 'failed', 'rate_limited'])

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
  const [watching, setWatching] = useState(true)

  useEffect(() => {
    // Stopping closes the loop that asks the API; the run itself is on the
    // server and carries on regardless of this page.
    if (!watching) return
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
  }, [runId, attempt, watching])

  if (run && TERMINAL.has(run.status)) {
    return <ReportPage runId={runId} onNewTender={onNewTender} />
  }

  const stages = Object.entries(run?.stage_seconds ?? {})

  return (
    <div className="stack">
      <Trail stage="match" onBack={onNewTender} />

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
              <div
                className="progress-fill"
                style={{ width: `${Math.min(100, run.progress_percent)}%` }}
              />
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

            {/* The run's own measurements, written as each pair lands (D-134).
                The stage names and the figures are the backend's; this page
                shows them or says they are not there yet. Nothing is shown as a
                zero, because a run that has answered no pair has recorded no
                spend either, and a column of zeros reads as a fast run. */}
            {stages.length === 0 ? (
              <dl className="summary-grid">
                <div>
                  <dt>Measured so far</dt>
                  <dd>Nothing yet: the first pair is still being read.</dd>
                </div>
              </dl>
            ) : (
              <dl className="summary-grid">
                {stages.map(([stage, seconds]) => (
                  <div key={stage}>
                    <dt>{stage}</dt>
                    <dd>{seconds.toFixed(3)} s</dd>
                  </div>
                ))}
                <div>
                  <dt>Model calls</dt>
                  <dd>{run.llm_calls}</dd>
                </div>
                <div>
                  <dt>Prompt tokens</dt>
                  <dd>{run.prompt_tokens}</dd>
                </div>
                <div>
                  <dt>Completion tokens</dt>
                  <dd>{run.completion_tokens}</dd>
                </div>
                <div>
                  <dt>Retries</dt>
                  <dd>{run.llm_retries}</dd>
                </div>
              </dl>
            )}

            {run.error && (
              <Notice variant="error" title="The run stopped with an error">
                {run.error}
              </Notice>
            )}
          </div>
        )}

        {watching ? (
          <div className="actions">
            <button type="button" className="button" onClick={() => setWatching(false)}>
              Stop watching
            </button>
            <span className="muted">This page asks the API once a second; stopping only rests it.</span>
          </div>
        ) : (
          <Notice variant="info" title="Not following this run">
            <p>
              The status above is where the run stood when this page stopped asking. Nothing was
              cancelled: the run carries on, and the report is served when it stops.
            </p>
            <div className="actions">
              <button type="button" className="button button-primary" onClick={() => setWatching(true)}>
                Watch it again
              </button>
              <button type="button" className="button" onClick={onNewTender}>
                Back to the tender
              </button>
            </div>
          </Notice>
        )}
      </section>
    </div>
  )
}
