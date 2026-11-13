import type { ContextClause } from '../types'

export function ContextClauses({ rows }: { rows: ContextClause[] }) {
  return (
    <>
      {[true, false].map((fragments) => {
        const group = rows.filter((row) => Boolean(row.fragment_reason) === fragments)
        if (!group.length) return null
        return (
          <section className="panel" key={String(fragments)}>
            <details>
              <summary>{fragments ? 'Fragments' : 'Source context'} ({group.length})</summary>
              <p>Retained with their sources and reasons; excluded from capability scoring.</p>
              <ul>{group.map((row, index) => (
                <li key={row.id ?? row.requirement_id ?? index}>
                  <strong>{row.source_role ?? 'unknown'} · {row.type} · page {row.page}</strong>
                  <p>{row.source_text ?? row.text}</p>
                  <p className="muted">{row.fragment_reason ?? row.reason ?? row.review_reason}</p>
                </li>
              ))}</ul>
            </details>
          </section>
        )
      })}
    </>
  )
}
