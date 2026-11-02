import { label, requirementText } from '../report'
import type { Report, ReviewEntry } from '../types'

export function ReviewQueue({
  report,
  onShow,
}: {
  report: Report
  onShow: (entry: ReviewEntry) => void
}) {
  if (report.review_queue.length === 0) {
    return (
      <div className="empty">
        <strong>Nothing is waiting for review.</strong>
        <span className="muted">
          Every assessment either passed its verification or was recorded as no evidence found.
        </span>
      </div>
    )
  }

  return (
    <>
      <p className="muted">
        A flag says where to look, not what to decide. This tool does not record the decision.
      </p>
      <div className="table-wrap">
        <table className="report-table">
          <thead>
            <tr>
              <th scope="col">Requirement</th>
              <th scope="col">Manufacturer</th>
              <th scope="col">Status</th>
              <th scope="col">Why it needs review</th>
              <th scope="col">
                <span className="visually-hidden">Open</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {report.review_queue.map((entry, index) => (
              <tr key={`${entry.requirement_id}-${entry.manufacturer}-${index}`}>
                <td>{requirementText(report, entry) ?? '(requirement not in this report)'}</td>
                <td>{entry.manufacturer}</td>
                <td>
                  <span className={`pill pill-${entry.status}`}>{label(entry.status)}</span>
                </td>
                <td>{entry.reason ?? 'No reason recorded'}</td>
                <td>
                  <button type="button" className="link" onClick={() => onShow(entry)}>
                    Show in table
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}
