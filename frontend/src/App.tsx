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
          {/* TenderMatch mark: the clause under the lens, swept into a match. */}
          <svg
            className="brand-mark"
            width="30"
            height="30"
            viewBox="0 0 32 32"
            aria-hidden="true"
            focusable="false"
          >
            <path
              d="M6.5 11.5 C3 15.5 3.6 21.5 8.5 24.5"
              fill="none"
              stroke="var(--brand)"
              strokeWidth="2.6"
              strokeLinecap="round"
            />
            <path
              d="M10 4.5 h8 L23.5 10 V25.5 a2 2 0 0 1 -2 2 H10 a2 2 0 0 1 -2 -2 V6.5 a2 2 0 0 1 2 -2 Z"
              fill="none"
              stroke="var(--seal)"
              strokeWidth="2"
              strokeLinejoin="round"
            />
            <path d="M18 4.5 V10 H23.5" fill="none" stroke="var(--seal)" strokeWidth="2" strokeLinejoin="round" />
            <path d="M11.5 11 H17 M11.5 14.5 H16 M11.5 18 H13.5" fill="none" stroke="var(--seal)" strokeWidth="2" strokeLinecap="round" />
            <rect x="25.5" y="2" width="2.6" height="2.6" rx="0.6" fill="var(--brand)" />
            <rect x="28.6" y="4.8" width="2.2" height="2.2" rx="0.5" fill="var(--brand)" />
            <rect x="23.6" y="6.2" width="2.2" height="2.2" rx="0.5" fill="var(--v-silent)" />
            <rect x="26.6" y="8.8" width="1.8" height="1.8" rx="0.4" fill="var(--v-silent)" />
            <circle cx="20" cy="20" r="6" fill="var(--surface)" stroke="var(--seal)" strokeWidth="2.4" />
            <path d="M24.4 24.4 L28.2 28.2" fill="none" stroke="var(--seal)" strokeWidth="2.8" strokeLinecap="round" />
            <path d="M17.3 20.2 L19.4 22.3 L23 18.2" fill="none" stroke="var(--brand)" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="brand-name">Tender<span className="brand-accent">Match</span></span>
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
