import { useEffect, useRef, useState } from 'react'

import { ApiError, getManufacturers, getRequirements, startRun, uploadTender } from '../api'
import { errorMessage } from '../errors'
import type { ManufacturerRow, Manufacturers, TenderRequirements } from '../types'
import { Notice } from './Notice'

const POLL_MS = 1000

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

type Phase = 'idle' | 'uploading' | 'reading' | 'ready' | 'failed'

export function UploadPage({ onStarted }: { onStarted: (runId: string) => void }) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [tender, setTender] = useState<TenderRequirements | null>(null)
  const [duplicate, setDuplicate] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [corpus, setCorpus] = useState<Manufacturers | null>(null)
  const [corpusError, setCorpusError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [starting, setStarting] = useState(false)

  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  useEffect(() => {
    getManufacturers()
      .then((data) => {
        if (!alive.current) return
        setCorpus(data)
        setSelected(data.manufacturers.map((row) => row.name))
      })
      .catch((cause) => {
        if (alive.current) setCorpusError(errorMessage(cause))
      })
  }, [])

  async function poll(tenderId: string) {
    while (alive.current) {
      try {
        const data = await getRequirements(tenderId)
        if (!alive.current) return
        setTender(data)
        setPhase(data.status === 'done' ? 'ready' : data.status === 'failed' ? 'failed' : 'reading')
        if (data.status === 'done' || data.status === 'failed') return
      } catch (cause) {
        if (!alive.current) return
        // 409 while queued or processing is the API saying "not read yet".
        if (cause instanceof ApiError && cause.status === 409) {
          await sleep(POLL_MS)
          continue
        }
        setError(errorMessage(cause))
        setPhase('idle')
        return
      }
      await sleep(POLL_MS)
    }
  }

  async function choose(file: File) {
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      setError('A tender must be a PDF. Other documents are added as attachments.')
      return
    }
    setError(null)
    setTender(null)
    setDuplicate(false)
    setPhase('uploading')
    try {
      const uploaded = await uploadTender(file)
      if (!alive.current) return
      setDuplicate(uploaded.duplicate)
      setPhase('reading')
      await poll(uploaded.tender_id)
    } catch (cause) {
      if (!alive.current) return
      setError(errorMessage(cause))
      setPhase('idle')
    }
  }

  async function run() {
    if (!tender) return
    setStarting(true)
    setError(null)
    try {
      const started = await startRun(tender.tender_id, selected)
      onStarted(started.run_id)
    } catch (cause) {
      setError(errorMessage(cause))
      setStarting(false)
    }
  }

  function reset() {
    setPhase('idle')
    setTender(null)
    setDuplicate(false)
    setError(null)
  }

  const reading = phase === 'uploading' || phase === 'reading'
  const canRun =
    phase === 'ready' &&
    tender !== null &&
    tender.requirements.length > 0 &&
    selected.length > 0 &&
    (corpus?.manufacturers.length ?? 0) > 0

  return (
    <div className="stack">
      <section className="panel">
        <h1>Upload a tender</h1>
        <p className="lede">
          The backend parses the document, extracts its requirements and records the page
          each one came from. Nothing is matched until you choose manufacturers and run it.
        </p>

        {error && (
          <Notice variant="error" title="Could not continue">
            {error}
          </Notice>
        )}

        <label className={`dropzone${reading ? ' dropzone-busy' : ''}`}>
          <input
            type="file"
            accept="application/pdf,.pdf"
            disabled={reading}
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void choose(file)
              event.target.value = ''
            }}
          />
          {reading ? (
            <>
              <span className="spinner" aria-hidden="true" />
              <strong>
                {phase === 'uploading' ? 'Uploading the tender…' : 'Reading the tender…'}
              </strong>
              <span className="muted">
                Parsing pages, items and requirements. A long tender can take a few minutes.
              </span>
            </>
          ) : (
            <>
              <strong>Drop a tender PDF here, or click to choose one</strong>
              <span className="muted">PDF only. The file is stored on the server, not in the browser.</span>
            </>
          )}
        </label>

        {duplicate && tender && (
          <Notice variant="info" title="Already uploaded">
            These exact bytes were uploaded before, so the existing reading was reused.
          </Notice>
        )}

        {tender && phase !== 'reading' && <TenderSummary tender={tender} />}
      </section>

      {phase === 'failed' && tender && (
        <section className="panel">
          <h2>The tender could not be read</h2>
          <Notice variant="error" title="Ingestion failed">
            {tender.error ?? 'The API reported a failure without a reason.'}
          </Notice>
          <p className="muted">
            Running it now would report requirements nobody managed to read. Upload a readable
            copy, or add the missing documents as attachments to it first.
          </p>
          <button type="button" className="button" onClick={reset}>
            Upload another tender
          </button>
        </section>
      )}

      {phase === 'ready' && tender && (
        <>
          {tender.requirements.length === 0 ? (
            <section className="panel">
              <h2>No technical requirements were extracted</h2>
              <p className="muted">
                The tender was read; the documents it contains state no technical requirement
                the pipeline could extract. There is nothing to match.
              </p>
              <button type="button" className="button" onClick={reset}>
                Upload another tender
              </button>
            </section>
          ) : (
            <section className="panel">
              <h2>Choose manufacturers</h2>
              {corpusError && (
                <Notice variant="error" title="The corpus could not be read">
                  {corpusError}
                </Notice>
              )}
              {!corpus && !corpusError && <p className="muted">Loading the corpus…</p>}
              {corpus && corpus.manufacturers.length === 0 && (
                <Notice variant="warning" title="The corpus is empty">
                  No manufacturer has been ingested, so there is nothing to match against.
                  Ingest the corpus first.
                </Notice>
              )}
              {corpus && corpus.manufacturers.length > 0 && (
                <ManufacturerPicker
                  rows={corpus.manufacturers}
                  selected={selected}
                  onChange={setSelected}
                />
              )}
              <div className="actions">
                <button
                  type="button"
                  className="button button-primary"
                  disabled={!canRun || starting}
                  onClick={() => void run()}
                >
                  {starting ? 'Starting…' : 'Run the match'}
                </button>
                {selected.length === 0 && (
                  <span className="muted">Choose at least one manufacturer.</span>
                )}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  )
}

function TenderSummary({ tender }: { tender: TenderRequirements }) {
  return (
    <dl className="summary-grid">
      <div>
        <dt>File</dt>
        <dd>{tender.filename ?? '—'}</dd>
      </div>
      <div>
        <dt>Pages</dt>
        <dd>{tender.page_count ?? '—'}</dd>
      </div>
      <div>
        <dt>BOQ items</dt>
        <dd>{tender.counts.items}</dd>
      </div>
      <div>
        <dt>Requirements</dt>
        <dd>{tender.counts.requirements}</dd>
      </div>
      <div>
        <dt>Flagged for review</dt>
        <dd>{tender.counts.flagged}</dd>
      </div>
    </dl>
  )
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`
}

function ManufacturerPicker({
  rows,
  selected,
  onChange,
}: {
  rows: ManufacturerRow[]
  selected: string[]
  onChange: (names: string[]) => void
}) {
  function toggle(name: string) {
    onChange(
      selected.includes(name)
        ? selected.filter((item) => item !== name)
        : [...selected, name],
    )
  }

  return (
    <div className="picker">
      <div className="picker-actions">
        <button
          type="button"
          className="link"
          onClick={() => onChange(rows.map((row) => row.name))}
        >
          Select all
        </button>
        <button type="button" className="link" onClick={() => onChange([])}>
          Clear
        </button>
      </div>
      <ul className="checklist">
        {rows.map((row) => (
          <li key={row.id}>
            <label>
              <input
                type="checkbox"
                checked={selected.includes(row.name)}
                onChange={() => toggle(row.name)}
              />
              <span>
                <strong>{row.name}</strong>
                <span className="muted">
                  {plural(row.documents, 'document', 'documents')} ·{' '}
                  {plural(row.categories.length, 'category', 'categories')}
                </span>
              </span>
            </label>
          </li>
        ))}
      </ul>
    </div>
  )
}
