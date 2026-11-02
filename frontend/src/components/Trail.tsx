import { Fragment } from 'react'

type Stage = 'match' | 'report'

const STOPS = ['Tender', 'Match', 'Report'] as const

/** Where the reader is, and the one-click way out of it.
 *
 * A finished step that the app can return to is a button; one it cannot is a
 * plain label. The page only ever holds one run, so the way out leads to the
 * tender - and saying so is better than a crumb that goes nowhere. The steps
 * ahead are listed too, so the flow reads as three stages rather than as a page
 * that happened to load.
 */
export function Trail({ stage, onBack }: { stage: Stage; onBack: () => void }) {
  const current = stage === 'match' ? 1 : 2
  return (
    <nav className="trail" aria-label="Where you are">
      {STOPS.map((stop, index) => {
        const isPast = index < current
        const reachable = isPast && index === 0
        return (
          <Fragment key={stop}>
            {index > 0 && (
              <span className="trail-sep" aria-hidden="true">
                ›
              </span>
            )}
            {index === current ? (
              <span className="trail-current" aria-current="step">
                {stop}
              </span>
            ) : reachable ? (
              <button
                type="button"
                className="trail-past"
                title="Back to the tender: upload another one"
                onClick={onBack}
              >
                {stop}
              </button>
            ) : (
              <span className={isPast ? 'trail-done' : 'trail-soon'}>{stop}</span>
            )}
          </Fragment>
        )
      })}
    </nav>
  )
}
