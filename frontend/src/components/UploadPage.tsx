import { useEffect, useRef, useState } from 'react'

import {
  ApiError,
  getAttachments,
  getManufacturers,
  getRequirements,
  requirementsStreamUrl,
  startRun,
  uploadAttachment,
  uploadTender,
} from '../api'
import { errorMessage } from '../errors'
import type {
  AttachmentRow,
  ManufacturerRow,
  Manufacturers,
  TenderAttachments,
  TenderRequirements,
} from '../types'
import { Notice } from './Notice'

const POLL_MS = 1000

/** What a reviewer may hand over in place of a document that could not be
 * fetched — the same set the backend accepts. */
const ATTACHMENT_ACCEPT = 'application/pdf,.pdf,.docx,.csv'

function sleep(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

/**
 * `rate_limited` is the read stopping at the provider's limit, not a failure:
 * the requirements it did read are there, and the rest can be resumed by
 * uploading the same file again (D8, D-69).
 */
type Phase = 'idle' | 'uploading' | 'reading' | 'ready' | 'rate_limited' | 'failed'

/** The phase a payload means. Anything still being read is `reading`. */
function phaseOf(data: TenderRequirements): Phase {
  if (data.status === 'done') return 'ready'
  if (data.status === 'failed') return 'failed'
  if (data.status === 'rate_limited') return 'rate_limited'
  return 'reading'
}

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
  const source = useRef<EventSource | null>(null)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      // A stream outlives the component unless it is closed: the browser would
      // keep the connection open, and the server a thread with it.
      source.current?.close()
      source.current = null
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

  /** Show a payload, and say what phase it puts the page in. */
  function apply(data: TenderRequirements): Phase | null {
    if (!alive.current) return null
    setTender(data)
    const next = phaseOf(data)
    setPhase(next)
    return next
  }

  async function poll(tenderId: string) {
    while (alive.current) {
      try {
        const data = await getRequirements(tenderId)
        const next = apply(data)
        if (next === null) return
        // Every state but "still reading" is final for this upload: polling a
        // rate-limited tender would poll forever, because nothing will resume
        // it without the caller asking again.
        if (next !== 'reading') return
      } catch (cause) {
        if (!alive.current) return
        // A 409 is an older backend saying "not read yet"; the current one
        // answers with what it has read so far and `partial`.
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

  /**
   * Follow the tender as it is read.
   *
   * The requirements are pushed as each one is read (D-82), so the page fills
   * in instead of waiting for the last one. The plain route is the fallback:
   * a browser without `EventSource`, or a stream that breaks mid-read, still
   * answers with everything read so far.
   */
  async function watch(tenderId: string) {
    if (typeof EventSource === 'undefined') {
      await poll(tenderId)
      return
    }

    const stream = new EventSource(requirementsStreamUrl(tenderId))
    source.current = stream
    let settled = false

    const finish = () => {
      settled = true
      stream.close()
      if (source.current === stream) source.current = null
    }

    const onPayload = (event: Event) => {
      if (settled) return
      try {
        apply(JSON.parse((event as MessageEvent<string>).data) as TenderRequirements)
      } catch {
        // A frame the browser could not parse is not a requirement list.
        finish()
        void poll(tenderId)
      }
    }

    stream.addEventListener('snapshot', onPayload)
    stream.addEventListener('requirements', onPayload)
    stream.addEventListener('end', (event) => {
      onPayload(event)
      finish()
    })
    stream.addEventListener('error', () => {
      // The stream broke before the read finished. The route still answers,
      // so the page keeps what it has and asks it.
      if (settled) return
      finish()
      void poll(tenderId)
    })
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
      await watch(uploaded.tender_id)
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
    (phase === 'ready' || phase === 'rate_limited') &&
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

        {tender && <TenderSummary tender={tender} />}
      </section>

      {tender && phase !== 'uploading' && phase !== 'reading' && (
        <AttachmentsPanel tenderId={tender.tender_id} />
      )}

      {tender && phase === 'reading' && <LiveRequirements tender={tender} />}

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

      {(phase === 'ready' || phase === 'rate_limited') && tender && (
        <>
          {phase === 'rate_limited' && (
            <section className="panel">
              <Notice variant="warning" title="The reading stopped at the provider's limit">
                {tender.error ?? 'The model provider refused further calls.'}
              </Notice>
              <p className="muted">
                The requirements below are the ones that were read before the limit. Nothing was
                invented to fill the gap, and a requirement that was never read is not a
                requirement the tender does not state. Upload the same file again to resume the
                read where it stopped.
              </p>
            </section>
          )}
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

/** The two states a fetch ends in that a person can still fix. */
function isUnsupplied(row: AttachmentRow): boolean {
  return row.status === 'failed' || row.status === 'restricted'
}

/**
 * The documents the tender links to, and the way to supply one the backend
 * could not fetch (PRD 5.3.1, FR-T4).
 *
 * A link that moved is not a tender with fewer requirements. The reading
 * stopped at that page, so the reviewer — who can still open the buyer's
 * portal — hands the file over, and the same tender read again picks it up.
 */
function AttachmentsPanel({ tenderId }: { tenderId: string }) {
  const [data, setData] = useState<TenderAttachments | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [recorded, setRecorded] = useState<string | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    let live = true
    getAttachments(tenderId)
      .then((answer) => {
        if (live) {
          setData(answer)
          setError(null)
        }
      })
      .catch((cause) => {
        if (live) setError(errorMessage(cause))
      })
    return () => {
      live = false
    }
  }, [tenderId, reload])

  async function send(file: File, url: string | null) {
    setBusy(url ?? file.name)
    setError(null)
    try {
      const stored = await uploadAttachment(tenderId, file, url ?? undefined)
      setRecorded(stored.name)
      setReload((count) => count + 1)
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(null)
    }
  }

  const rows = data?.attachments ?? []
  const unsupplied = rows.filter(isUnsupplied)

  return (
    <section className="panel">
      <h2>Attachments</h2>
      <p className="lede">
        The tender only names its documents; the backend fetches each one. A link
        that could not be reached leaves the reading incomplete, it does not mean
        the tender states less.
      </p>

      {error && (
        <Notice variant="error" title="The attachments could not be read">
          {error}
        </Notice>
      )}
      {!data && !error && <p className="muted">Loading the attachments…</p>}
      {data && rows.length === 0 && (
        <p className="muted">
          No document is recorded for this tender, so there is nothing to supply.
        </p>
      )}
      {recorded && (
        <Notice variant="info" title="Supplied">
          {`“${recorded}” is stored against this tender. Upload the tender PDF again to read
           it with this document: the stages that do not depend on it are not run twice.`}
        </Notice>
      )}

      {unsupplied.length > 0 && (
        <p className="muted">
          {plural(unsupplied.length, 'document', 'documents')} could not be fetched.
          Choose the file you downloaded from the buyer to take its place.
        </p>
      )}

      {rows.length > 0 && (
        <ul className="compact-list attachment-list">
          {rows.map((row, index) => (
            <li key={row.url ?? `attachment-${index}`}>
              <div className="attachment-head">
                <strong>{row.name ?? row.url ?? 'an unnamed document'}</strong>
                <span className={`tag${isUnsupplied(row) ? ' tag-warn' : ''}`}>
                  {row.status ?? 'unknown'}
                </span>
                {row.kind && <span className="tag">{row.kind}</span>}
                {typeof row.pages === 'number' && (
                  <span className="muted">{plural(row.pages, 'page', 'pages')}</span>
                )}
              </div>
              {row.url && row.url !== row.name && (
                <p className="muted attachment-url">{row.url}</p>
              )}
              {row.note && <p className="muted">{row.note}</p>}
              {isUnsupplied(row) && (
                <label className="attachment-supply">
                  <span className="muted">
                    {busy === row.url ? 'Storing the file…' : 'Supply this document'}
                  </span>
                  <input
                    type="file"
                    accept={ATTACHMENT_ACCEPT}
                    disabled={busy !== null}
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      if (file) void send(file, row.url)
                      event.target.value = ''
                    }}
                  />
                </label>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/**
 * The requirements read so far, while the tender is still being read (D-82).
 *
 * The list is what the extraction has stored, not a prediction: it grows as
 * each requirement is read, and it is not the whole document until the
 * reading stops. That is why the heading says "so far" and not "the
 * requirements".
 */
function LiveRequirements({ tender }: { tender: TenderRequirements }) {
  const read = tender.counts.requirements
  return (
    <section className="panel">
      <h2>Requirements read so far</h2>
      <p className="muted">
        {read === 0
          ? 'The pages and the bill of quantities are being read. Requirements appear here as they are extracted.'
          : `${plural(read, 'requirement', 'requirements')} read so far. The list grows as the reading continues; it is complete only when the reading stops.`}
      </p>
      {read > 0 && (
        <ul className="compact-list live-list">
          {tender.requirements.map((row) => (
            <li key={row.id}>
              <span className="tag">{row.item_id ? `Item ${row.item_id}` : 'No item'}</span>{' '}
              <span className="tag">p.{row.page}</span> <span>{row.text}</span>
              {row.review_required && (
                <>
                  {' '}
                  <span className="flag">Needs review</span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
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
