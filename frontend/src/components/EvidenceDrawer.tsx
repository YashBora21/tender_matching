import { useEffect, useRef } from 'react'

import { formatScore, label } from '../report'
import type { Assessment } from '../types'

export function EvidenceDrawer({
  row,
  meanings,
  onClose,
}: {
  row: Assessment
  meanings: Record<string, string>
  onClose: () => void
}) {
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    closeRef.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const { requirement, result, unsupported } = row
  const evidence = result.evidence
  const source = evidence?.url && /^https?:\/\//i.test(evidence.url) ? evidence.url : null

  return (
    <div className="drawer-layer">
      <button
        type="button"
        className="drawer-backdrop"
        aria-label="Close the evidence panel"
        onClick={onClose}
      />
      <aside
        className={`drawer drawer-${result.status}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
      >
        <div className="drawer-head">
          <div>
            <span className="eyebrow">{result.manufacturer}</span>
            <h2 id="drawer-title">{label(result.status)}</h2>
          </div>
          <button ref={closeRef} type="button" className="button" onClick={onClose}>
            Close
          </button>
        </div>

        <p className="muted">{meanings[result.status] ?? ''}</p>

        <section className="drawer-section">
          <h3>Requirement</h3>
          <p>{requirement.text}</p>
          <p className="muted">
            Item {requirement.item_id ?? '—'}
            {requirement.item_title ? ` · ${requirement.item_title}` : ''}
            {requirement.page !== null ? ` · page ${requirement.page}` : ''}
          </p>
        </section>

        {result.capability_text && (
          <section className="drawer-section">
            <h3>What the documents say</h3>
            <p>{result.capability_text}</p>
          </section>
        )}

        <section className="drawer-section">
          <h3>Evidence</h3>
          {evidence ? (
            <>
              <blockquote className="quote">{evidence.quote ?? 'The fact has no quote.'}</blockquote>
              <dl className="detail-list">
                <div>
                  <dt>Document</dt>
                  <dd>{evidence.document ?? '—'}</dd>
                </div>
                <div>
                  <dt>Page</dt>
                  <dd>{evidence.page ?? '—'}</dd>
                </div>
                <div>
                  <dt>Type</dt>
                  <dd>{evidence.doc_type ?? '—'}</dd>
                </div>
                {evidence.sha256 && (
                  <div>
                    <dt>Document hash</dt>
                    <dd className="mono">{evidence.sha256.slice(0, 16)}…</dd>
                  </div>
                )}
                <div>
                  <dt>Source</dt>
                  <dd>
                    {source ? (
                      <a href={source} target="_blank" rel="noreferrer noopener">
                        Open the source document
                      </a>
                    ) : (
                      <span className="muted">
                        No source address is recorded. The quote and page above identify it.
                      </span>
                    )}
                  </dd>
                </div>
              </dl>
              <p className="muted">
                The link opens the document; the page number above is where the quote was read.
              </p>
            </>
          ) : (
            <p className="muted">
              No verified quote backs this row. That is recorded as it is, and is not read as a
              capability the manufacturer lacks.
            </p>
          )}
        </section>

        <section className="drawer-section">
          <h3>Assessment</h3>
          <dl className="detail-list">
            <div>
              <dt>Score</dt>
              <dd>{unsupported ? 'Excluded from the score' : formatScore(result.score)}</dd>
            </div>
            <div>
              <dt>Verification</dt>
              <dd>{label(result.verification_outcome)}</dd>
            </div>
            <div>
              <dt>Applicability</dt>
              <dd>{label(result.applicability)}</dd>
            </div>
            <div>
              <dt>Confidence</dt>
              <dd>{label(result.confidence)}</dd>
            </div>
            <div>
              <dt>Review</dt>
              <dd>
                {result.review_required
                  ? result.review_reason ?? 'Flagged, with no reason recorded'
                  : 'Not flagged'}
              </dd>
            </div>
          </dl>
        </section>
      </aside>
    </div>
  )
}
