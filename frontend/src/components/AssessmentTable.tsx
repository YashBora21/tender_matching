import { formatScore, label } from '../report'
import type { Assessment } from '../types'

export function AssessmentTable({
  rows,
  meanings,
  onOpen,
}: {
  rows: Assessment[]
  meanings: Record<string, string>
  onOpen: (row: Assessment) => void
}) {
  if (rows.length === 0) {
    return (
      <div className="empty">
        <strong>No rows match these filters.</strong>
        <span className="muted">Clear a filter to see the rest of the report.</span>
      </div>
    )
  }

  return (
    <div className="table-wrap">
      <table className="report-table">
        <thead>
          <tr>
            <th scope="col">Item</th>
            <th scope="col">Requirement</th>
            <th scope="col">Manufacturer</th>
            <th scope="col">Status</th>
            <th scope="col">Score</th>
            <th scope="col">Review</th>
            <th scope="col">
              <span className="visually-hidden">Evidence</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const { requirement, result, unsupported } = row
            return (
              <tr key={`${requirement.requirement_id}-${result.manufacturer_id}`}>
                <td>
                  <div className="item-cell">
                    <strong>{requirement.item_id ?? '—'}</strong>
                    <span className="muted">{requirement.item_title ?? 'No BOQ item'}</span>
                  </div>
                </td>
                <td>
                  <div className="requirement-cell">
                    <span>{requirement.text}</span>
                    <span className="row-meta">
                      {requirement.type && <span className="tag">{requirement.type}</span>}
                      {requirement.mandatory ? (
                        <span className="tag tag-strong">Mandatory</span>
                      ) : (
                        <span className="tag">Optional</span>
                      )}
                      {unsupported && <span className="tag">Excluded from score</span>}
                      {requirement.category && (
                        <span className="tag">{requirement.category}</span>
                      )}
                    </span>
                  </div>
                </td>
                <td>{result.manufacturer}</td>
                <td>
                  <span
                    className={`pill pill-${result.status}`}
                    title={meanings[result.status] ?? label(result.status)}
                  >
                    {label(result.status)}
                  </span>
                </td>
                <td>{unsupported ? '—' : formatScore(result.score)}</td>
                <td>
                  {result.review_required ? (
                    <span className="flag" title={result.review_reason ?? 'Flagged for review'}>
                      Needs review
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <button type="button" className="link" onClick={() => onOpen(row)}>
                    {result.evidence ? 'Evidence' : 'Details'}
                  </button>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
