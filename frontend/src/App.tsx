import { useState } from 'react'

import { RunPage } from './components/RunPage'
import { UploadPage } from './components/UploadPage'

type View = { kind: 'upload' } | { kind: 'run'; runId: string }

export default function App() {
  const [view, setView] = useState<View>({ kind: 'upload' })

  return (
    <div className="app">
      <header className="topbar">
        <button type="button" className="brand" onClick={() => setView({ kind: 'upload' })}>
          Tender matching
        </button>
        <span className="tagline">
          Requirements compared with the corpus, each claim backed by its quote.
        </span>
      </header>

      <main className="main">
        {view.kind === 'upload' ? (
          <UploadPage onStarted={(runId) => setView({ kind: 'run', runId })} />
        ) : (
          <RunPage runId={view.runId} onNewTender={() => setView({ kind: 'upload' })} />
        )}
      </main>
    </div>
  )
}
