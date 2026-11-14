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
      <a className="skip-link" href="#workspace">Skip to workspace</a>
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
        <span className="tagline">Engineering review workspace</span>
        <a className="workspace-link" href="#workspace">{view.kind === 'upload' ? 'Tender workspace' : 'Comparison workspace'} <span aria-hidden="true">↗</span></a>
        {view.kind === 'run' && (
          <div className="actions actions-end">
            <button type="button" className="button" onClick={() => go({ kind: 'upload' })}>
              <span aria-hidden="true">←</span> Back to the tender
            </button>
          </div>
        )}
      </header>

      {view.kind === 'upload' && (
        <section className="workspace-hero" aria-labelledby="workspace-title">
          <div className="hero-inner">
            <div>
              <span className="eyebrow">From specification to evidence</span>
              <h1 id="workspace-title">Every requirement.<br /><span>Evidence in view.</span></h1>
              <p>Bring your tender and catalogue evidence together. Review technical requirements, compare manufacturers and inspect the source behind each finding.</p>
              <a className="button button-primary" href="#workspace">Open tender workspace <span aria-hidden="true">↓</span></a>
            </div>
            <ol className="workflow-outline" aria-label="Review workflow">
              <li><span className="workflow-number">01</span><div><strong>Read the tender</strong><p>Specifications, BOQ items and source pages.</p></div></li>
              <li><span className="workflow-number">02</span><div><strong>Compare the evidence</strong><p>Requirements alongside catalogue capabilities.</p></div></li>
              <li><span className="workflow-number">03</span><div><strong>Review each finding</strong><p>Verified citations, uncertainty and deviations.</p></div></li>
            </ol>
          </div>
        </section>
      )}
      <main className="main" id="workspace" tabIndex={-1}>
        {view.kind === 'upload' ? (
          <UploadPage onStarted={(runId) => go({ kind: 'run', runId })} />
        ) : (
          <RunPage runId={view.runId} onNewTender={() => go({ kind: 'upload' })} />
        )}
      </main>
    </div>
  )
}
