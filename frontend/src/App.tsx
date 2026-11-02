import { useEffect, useState } from 'react'

import { RunPage } from './components/RunPage'
import { UploadPage } from './components/UploadPage'

type View = { kind: 'upload' } | { kind: 'run'; runId: string }

export default function App() {
  const [view, setView] = useState<View>({ kind: 'upload' })

  // A reviewer whose run takes minutes hits the browser's Back, not a button in
  // the page. The two views are therefore entered as history entries rather
  // than kept only in state.
  useEffect(() => {
    const onPop = (event: PopStateEvent) => {
      const state = event.state as { runId?: string } | null
      setView(state?.runId ? { kind: 'run', runId: state.runId } : { kind: 'upload' })
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  function go(next: View) {
    setView(next)
    window.history.pushState(next.kind === 'run' ? { runId: next.runId } : {}, '')
  }

  return (
    <div className="app">
      <header className="topbar">
        <button type="button" className="brand" onClick={() => go({ kind: 'upload' })}>
          {/* The verdict rail, in miniature: three clauses, three outcomes. */}
          <svg
            className="brand-mark"
            width="20"
            height="20"
            viewBox="0 0 20 20"
            aria-hidden="true"
            focusable="false"
          >
            <rect
              x="0.75"
              y="0.75"
              width="18.5"
              height="18.5"
              rx="4.5"
              fill="none"
              stroke="currentColor"
              strokeOpacity="0.3"
            />
            <rect x="3" y="3" width="2" height="14" rx="1" fill="currentColor" />
            <rect x="7" y="4.5" width="9.5" height="2" rx="1" fill="#0d6146" />
            <rect x="7" y="9" width="9.5" height="2" rx="1" fill="#8a5b06" />
            <rect x="7" y="13.5" width="9.5" height="2" rx="1" fill="#9d1b2e" />
          </svg>
          Tender matching
        </button>
        <span className="tagline">
          Requirements compared with the corpus, each claim backed by its quote.
        </span>
        {view.kind === 'run' && (
          <div className="actions actions-end">
            <button type="button" className="button" onClick={() => go({ kind: 'upload' })}>
              <span aria-hidden="true">←</span> Back to the tender
            </button>
          </div>
        )}
      </header>

      <main className="main">
        {view.kind === 'upload' ? (
          <UploadPage onStarted={(runId) => go({ kind: 'run', runId })} />
        ) : (
          <RunPage runId={view.runId} onNewTender={() => go({ kind: 'upload' })} />
        )}
      </main>
    </div>
  )
}
