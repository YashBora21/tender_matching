import { useEffect, useMemo, useState } from 'react'

import { getReport, reportUrl } from '../api'
import { errorMessage } from '../errors'
import {
  NO_FILTERS,
  assessments,
  countStatuses,
  filterAssessments,
  formatDate,
  formatPercent,
  label,
  statusesPresent,
} from '../report'
import type { Filters } from '../report'
import type { Assessment, Report, ReviewEntry } from '../types'
import { AssessmentTable } from './AssessmentTable'
import { EvidenceDrawer } from './EvidenceDrawer'
import { Notice } from './Notice'
import { ReviewQueue } from './ReviewQueue'
import { Trail } from './Trail'

type Tab = 'requirements' | 'review'

export function ReportPage({
  runId,
  onNewTender,
}: {
  runId: string
  onNewTender: () => void
}) {
  const [report, setReport] = useState<Report | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [attempt, setAttempt] = useState(0)
  const [tab, setTab] = useState<Tab>('requirements')
  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const [openRow, setOpenRow] = useState<Assessment | null>(null)

  useEffect(() => {
    let cancelled = false
    getReport(runId)
      .then((data) => {
        if (cancelled) return
        setReport(data)
        setError(null)
      })
      .catch((cause) => {
        if (!cancelled) setError(errorMessage(cause))
      })
    return () => {
      cancelled = true
    }
  }, [runId, attempt])

  const rows = useMemo(() => (report ? assessments(report) : []), [report])
  const counts = useMemo(() => countStatuses(rows), [rows])
  const statuses = useMemo(() => statusesPresent(counts), [counts])
  const visible = useMemo(() => filterAssessments(rows, filters), [rows, filters])

  if (error) {
    return (
      <section className="panel">
        <h1>Report</h1>
        <Notice variant="error" title="Could not read the report">
          {error}
          <div className="actions">
            <button type="button" className="button" onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </button>
          </div>
        </Notice>
      </section>
    )
  }

  if (!report) {
    return (
      <section className="panel">
        <h1>Report</h1>
        <div className="loading-row">
          <span className="spinner" aria-hidden="true" /> Loading the report…
        </div>
      </section>
    )
  }

  const pages = report.tender.pages
  const flagged = report.review_queue.length

  function showReview(entry: ReviewEntry) {
    setFilters({ status: entry.status, manufacturer: entry.manufacturer, reviewOnly: false })
    setTab('requirements')
  }

  return (
    <div className="stack">
      <Trail stage="report" onBack={onNewTender} />

      <section className="panel">
        <div className="report-head">
          <div>
            <span className="eyebrow">Report</span>
            <h1>{report.tender.filename ?? 'Tender'}</h1>
            <p className="muted">
              {report.run.manufacturers.length} manufacturers · started{' '}
              {formatDate(report.run.started_at)} · run{' '}
              <span className="mono">{report.run.id.slice(0, 8)}</span>
            </p>
          </div>
          <div className="actions actions-end">
            <a
              className="button"
              href={reportUrl(runId, 'html')}
              target="_blank"
              rel="noreferrer noopener"
            >
              Open HTML report
            </a>
            <a className="button" href={reportUrl(runId, 'csv')}>
              Download CSV
            </a>
            <button type="button" className="button" onClick={onNewTender}>
              New tender
            </button>
          </div>
        </div>

        {report.run.error && (
          <Notice variant="error" title="The run stopped with an error">
            {report.run.error}
          </Notice>
        )}

        {!report.results.complete && (
          <Notice variant="warning" title="This report is partial">
            {report.results.stored} of {report.results.expected} assessments were stored. Missing
            pairs are not findings; they are work that did not finish.
          </Notice>
        )}

        {pages.incomplete_ingestion && (
          <Notice variant="warning" title="Incomplete ingestion">
            <p>
              The tender was not fully read, so a requirement may be missing from this report.
              That is not evidence that a manufacturer lacks a capability.
            </p>
            <ul className="compact-list">
              {pages.unaccounted > 0 && (
                <li>{pages.unaccounted} declared page(s) were never accounted for.</li>
              )}
              {pages.unresolved.length > 0 && (
                <li>
                  Pages that could not be read: {pages.unresolved.map((page) => page.page).join(', ')}.
                </li>
              )}
              {pages.skipped.length > 0 && (
                <li>
                  Pages skipped: {pages.skipped.map((page) => page.page).join(', ')}.
                </li>
              )}
              {report.tender.extraction && !report.tender.extraction.complete && (
                <li>
                  The extraction of requirements stopped short
                  {report.tender.extraction.detail
                    ? `: ${report.tender.extraction.detail}`
                    : '.'}
                </li>
              )}
            </ul>
          </Notice>
        )}

        {statuses.length > 0 && (
          <div className="status-bar" aria-label="Status counts">
            <span className="eyebrow">Statuses</span>
            {statuses.map((status) => (
              <button
                key={status}
                type="button"
                className={`status-chip${filters.status === status ? ' is-active' : ''}`}
                aria-pressed={filters.status === status}
                title={report.status_meanings[status] ?? label(status)}
                onClick={() =>
                  setFilters((current) => ({
                    ...current,
                    status: current.status === status ? null : status,
                  }))
                }
              >
                <span className={`dot dot-${status}`} aria-hidden="true" />
                {label(status)}
                <strong>{counts[status] ?? 0}</strong>
              </button>
            ))}
            <button
              type="button"
              className={`status-chip${filters.reviewOnly ? ' is-active' : ''}`}
              aria-pressed={filters.reviewOnly}
              onClick={() =>
                setFilters((current) => ({ ...current, reviewOnly: !current.reviewOnly }))
              }
            >
              <span className="dot dot-review" aria-hidden="true" />
              Needs review
              <strong>{flagged}</strong>
            </button>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Ranking</h2>
        {report.manufacturers.length === 0 ? (
          <div className="empty">
            <strong>No manufacturer summary was produced.</strong>
            <span className="muted">The run stored no results to summarise.</span>
          </div>
        ) : (
          <div className="rank-grid">
            {report.manufacturers.map((summary) => (
              <button
                key={summary.manufacturer_id}
                type="button"
                className={`rank-card${
                  filters.manufacturer === summary.manufacturer ? ' is-active' : ''
                }`}
                onClick={() =>
                  setFilters((current) => ({
                    ...current,
                    manufacturer:
                      current.manufacturer === summary.manufacturer
                        ? null
                        : summary.manufacturer,
                  }))
                }
              >
                <span className="rank-number">#{summary.rank}</span>
                <h3>{summary.manufacturer}</h3>
                <div className="rank-metrics">
                  <div>
                    <span className="metric">{formatPercent(summary.compatibility_percent)}</span>
                    <span className="metric-label">compatibility</span>
                  </div>
                  <div>
                    <span className="metric metric-secondary">
                      {formatPercent(summary.coverage_percent)}
                    </span>
                    <span className="metric-label">coverage</span>
                  </div>
                </div>
                {summary.fails_mandatory && (
                  <span className="flag">Fails a mandatory requirement</span>
                )}
                <span className="muted">{summary.review_count} flagged for review</span>
              </button>
            ))}
          </div>
        )}
        <p className="muted scoring-note">
          Compatibility is the weighted average of the scores of requirements with evidence
          (mandatory ×2, optional ×1). Coverage is the share of verifiable requirements for which
          the corpus had evidence at all. <em>No evidence found</em> counts towards coverage but
          not against the score; unsupported clauses are excluded from both.
        </p>
      </section>

      <section className="panel">
        <div className="tabs" role="tablist" aria-label="Report views">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'requirements'}
            className={tab === 'requirements' ? 'tab is-active' : 'tab'}
            onClick={() => setTab('requirements')}
          >
            Requirements <span className="count">{visible.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'review'}
            className={tab === 'review' ? 'tab is-active' : 'tab'}
            onClick={() => setTab('review')}
          >
            Review queue <span className="count">{flagged}</span>
          </button>
        </div>

        {tab === 'requirements' ? (
          <>
            <div className="toolbar">
              <label>
                <span className="muted">Manufacturer</span>
                <select
                  value={filters.manufacturer ?? ''}
                  onChange={(event) =>
                    setFilters((current) => ({
                      ...current,
                      manufacturer: event.target.value || null,
                    }))
                  }
                >
                  <option value="">All manufacturers</option>
                  {report.manufacturers.map((summary) => (
                    <option key={summary.manufacturer_id} value={summary.manufacturer}>
                      {summary.manufacturer}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span className="muted">Status</span>
                <select
                  value={filters.status ?? ''}
                  onChange={(event) =>
                    setFilters((current) => ({
                      ...current,
                      status: event.target.value || null,
                    }))
                  }
                >
                  <option value="">All statuses</option>
                  {statuses.map((status) => (
                    <option key={status} value={status}>
                      {label(status)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={filters.reviewOnly}
                  onChange={(event) =>
                    setFilters((current) => ({
                      ...current,
                      reviewOnly: event.target.checked,
                    }))
                  }
                />
                Needs review only
              </label>
              {(filters.status || filters.manufacturer || filters.reviewOnly) && (
                <button type="button" className="link" onClick={() => setFilters(NO_FILTERS)}>
                  Clear filters
                </button>
              )}
            </div>
            <AssessmentTable
              rows={visible}
              meanings={report.status_meanings}
              onOpen={setOpenRow}
            />
          </>
        ) : (
          <ReviewQueue report={report} onShow={showReview} />
        )}
      </section>

      <section className="panel">
        <details>
          <summary>What produced this report</summary>
          <dl className="detail-list detail-columns">
            <div>
              <dt>Model</dt>
              <dd>{stringValue(report.config.llm_model)}</dd>
            </div>
            <div>
              <dt>Embedding model</dt>
              <dd>{stringValue(report.config.embedding_model)}</dd>
            </div>
            <div>
              <dt>Prompt version</dt>
              <dd>{stringValue(report.config.prompt_version)}</dd>
            </div>
            <div>
              <dt>Schema version</dt>
              <dd>{stringValue(report.config.schema_version)}</dd>
            </div>
            <div>
              <dt>Scoring policy</dt>
              <dd>{stringValue(report.config.scoring_policy_version)}</dd>
            </div>
            <div>
              <dt>Corpus hash</dt>
              <dd className="mono">{report.run.corpus_hash?.slice(0, 16) ?? '—'}…</dd>
            </div>
            <div>
              <dt>Tender pages</dt>
              <dd>
                {pages.accounted} accounted of {pages.declared ?? '—'} declared
              </dd>
            </div>
            <div>
              <dt>Stored assessments</dt>
              <dd>
                {report.results.stored} of {report.results.expected}
              </dd>
            </div>
          </dl>
        </details>
      </section>

      {openRow && (
        <EvidenceDrawer
          row={openRow}
          meanings={report.status_meanings}
          onClose={() => setOpenRow(null)}
        />
      )}
    </div>
  )
}

function stringValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}
