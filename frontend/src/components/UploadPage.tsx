import { useEffect, useRef, useState } from 'react'

import {
  ApiError,
  getAttachments,
  getManufacturers,
  getRequirements,
  readAgain,
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
  RequirementRow,
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
 *
 * `incomplete` is the state between the two: the document opened and was read,
 * but a part of it never arrived, so its requirement list is short because of
 * what is missing rather than because the tender states less (PRD 4).
 */
type Phase =
  | 'idle'
  | 'uploading'
  | 'reading'
  | 'ready'
  | 'rate_limited'
  | 'incomplete'
  | 'failed'

/** What each way of falling short means, in the reviewer's terms (D-156).
 *
 * The three are different problems with different fixes: an attachment nobody
 * found, a scan nobody could read, and a document that was read and states
 * nothing. One sentence for all three would hide which one it is. */
const SHORT_READ: Record<string, { title: string; body: string }> = {
  no_specification_found: {
    title: 'No specification was among the documents',
    body: 'The tender links to forms or certificates, or to a bill of quantities and nothing else, so nothing states what the item has to do. Add the specification as an attachment above, then upload the same file again.',
  },
  specification_unreadable: {
    title: 'The specification is a scan nobody could read',
    body: 'A specification was found, but the text read out of it is empty or nearly so: its pages are images and OCR gave little or no text. This is not a document that states nothing. Upload a text-based copy, or a better scan.',
  },
  no_requirements_stated: {
    title: 'The specification was read and states no requirement',
    body: 'Every page was read and no clause states a technical requirement the catalogue could be matched against. Check that the right file was attached before running it.',
  },
}

/** The phase a payload means. Anything still being read is `reading`. */
function phaseOf(data: TenderRequirements): Phase {
  if (data.status === 'done') return 'ready'
  if (data.status === 'failed') return 'failed'
  if (data.status === 'rate_limited') return 'rate_limited'
  if (data.status === 'incomplete') return 'incomplete'
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

  // Following the read is a choice: the poll loop and the stream ask for it, so
  // a reviewer can rest the page without touching the read on the server.
  const [watching, setWatching] = useState(true)
  const follow = useRef(true)

  const alive = useRef(true)
  const source = useRef<EventSource | null>(null)
  const readId = useRef<string | null>(null)
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
    while (alive.current && follow.current) {
      try {
        const data = await getRequirements(tenderId)
        const next = apply(data)
        if (next === null || !follow.current) return
        // Every state but "still reading" is final for this upload: polling a
        // rate-limited tender would poll forever, because nothing will resume
        // it without the caller asking again.
        if (next !== 'reading') return
      } catch (cause) {
        if (!alive.current || !follow.current) return
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
   * The stream publishes the latest completed or paused checkpoint.
   * The plain route is the fallback:
   * a browser without `EventSource`, or a stream that breaks mid-read, still
   * answers with everything read so far.
   */
  async function watch(tenderId: string) {
    readId.current = tenderId
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
      if (settled || !follow.current) return
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
      if (settled || !follow.current) return
      finish()
      void poll(tenderId)
    })
  }

  /** Rest the page, not the read: the server finishes the tender either way. */
  function stopWatching() {
    follow.current = false
    setWatching(false)
    source.current?.close()
    source.current = null
  }

  function followAgain() {
    if (!readId.current) return
    follow.current = true
    setWatching(true)
    void watch(readId.current)
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
    follow.current = true
    setWatching(true)
    readId.current = null
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

  /**
   * Read the same tender again, under the limits in force now (step 5).
   *
   * A reading that stopped at a budget or a provider limit is resumed by
   * asking for it, not by re-uploading bytes. The new attempt supersedes the
   * saved requirements when it replaces them; the requirements, reports and
   * reading history of earlier attempts are kept.
   */
  async function readAgainAction() {
    if (!tender) return
    setError(null)
    follow.current = true
    setWatching(true)
    setPhase('reading')
    try {
      const answer = await readAgain(tender.tender_id)
      await watch(answer.tender_id)
    } catch (cause) {
      if (!alive.current) return
      setError(errorMessage(cause))
      setPhase('idle')
    }
  }

  const resumable =
    (phase === 'rate_limited' || phase === 'incomplete' || phase === 'failed') &&
    tender !== null

  const reading = phase === 'uploading' || phase === 'reading'
  /** The read is running *and* this page is still listening to it. */
  const following = reading && watching
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

        <label className={`dropzone${following ? ' dropzone-busy' : ''}`}>
          <input
            type="file"
            accept="application/pdf,.pdf"
            disabled={following}
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void choose(file)
              event.target.value = ''
            }}
          />
          {following ? (
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
              {/* A tender sheet with its clause rail: the same mark the header
                  carries, so the drop target asks for a document, not a file. */}
              <svg
                className="dropzone-mark"
                width="44"
                height="44"
                viewBox="0 0 44 44"
                aria-hidden="true"
                focusable="false"
              >
                <rect
                  x="11.5"
                  y="5.5"
                  width="21"
                  height="33"
                  rx="3"
                  fill="none"
                  stroke="currentColor"
                  strokeOpacity="0.45"
                />
                <rect x="15.5" y="12" width="2" height="20" rx="1" fill="currentColor" />
                <g
                  stroke="currentColor"
                  strokeOpacity="0.5"
                  strokeWidth="2"
                  strokeLinecap="round"
                  fill="none"
                >
                  <path d="M21.5 14h7" />
                  <path d="M21.5 20h7" />
                  <path d="M21.5 26h4" />
                </g>
              </svg>
              <strong>Drop a tender PDF here, or click to choose one</strong>
              <span className="muted">PDF only. The file is stored on the server, not in the browser.</span>
            </>
          )}
        </label>

        {reading &&
          (watching ? (
            <div className="actions">
              <button type="button" className="button" onClick={stopWatching}>
                Stop watching
              </button>
              <span className="muted">
                The read carries on either way; this only rests the page.
              </span>
            </div>
          ) : (
            <Notice variant="info" title="Not following this read">
              <p>
                The requirements below are what the server had read when this page stopped
                listening. Nothing was cancelled, and what was already read is still here.
              </p>
              <div className="actions">
                <button type="button" className="button button-primary" onClick={followAgain}>
                  Follow it again
                </button>
              </div>
            </Notice>
          ))}

        {duplicate && tender && (
          <Notice variant="info" title="Already uploaded">
            These exact bytes were uploaded before, so the existing reading was reused.
          </Notice>
        )}

        {tender && <TenderSummary tender={tender} />}
      </section>

      {!!tender?.context_clauses?.length && (
        <section className="panel">
          <details>
            <summary>Source context ({tender.context_clauses.length})</summary>
            <p>These clauses remain visible for review and do not block technical extraction.</p>
            <ul>{tender.context_clauses.map((row) => (
              <li key={row.id}>
                <strong>{row.source_role ?? 'unknown'} · {row.type} · page {row.page}</strong>
                <p>{row.source_text ?? row.text}</p>
              </li>
            ))}</ul>
          </details>
        </section>
      )}

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

      {phase === 'incomplete' && tender && (
        <section className="panel">
          <h2>{SHORT_READ[tender.reason_code ?? '']?.title ?? 'The tender was not read in full'}</h2>
          <Notice variant="warning" title="Incomplete reading">
            {SHORT_READ[tender.reason_code ?? '']?.body ??
              tender.error ??
              'A part of the tender was never read.'}
          </Notice>
          <p className="muted">
            Running it now would be refused: a requirement nobody read is not a requirement the
            manufacturer fails.
            {tender.reason_code === 'no_specification_found'
              ? ' Add the missing specification above, then upload the same tender again.'
              : tender.reason_code === 'specification_unreadable'
                ? ' Improve the scan or OCR reading, then retry the same tender.'
                : tender.reason_code === 'no_requirements_stated'
                  ? ''
                  : ' Review unresolved clauses, BOQ item assignments and extraction errors. After correcting the cause, upload the same tender to retry using valid cached answers.'}
          </p>
        </section>
      )}

      {(resumable || phase === 'ready') && (
        <div className="actions">
          <button type="button" className="button" onClick={() => void readAgainAction()}>
            Read this tender again
          </button>
          <span className="muted">
            A fresh reading under the current limits. It supersedes the saved
            requirements; earlier attempts, reports and reading history stay.
          </span>
        </div>
      )}

      {(phase === 'ready' || phase === 'rate_limited') && tender && (
        <>
          {phase === 'rate_limited' && (
            <section className="panel">
              <Notice variant="warning" title="The reading reached a processing limit">
                {tender.error ?? 'The configured budget or provider limit stopped further calls.'}
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
              <h2>{phase === 'rate_limited' ? 'No requirements have been published yet' : 'No technical requirements were extracted'}</h2>
              <p className="muted">
                {phase === 'rate_limited'
                  ? 'Reading stopped before any requirements were saved. Check the displayed budget or provider limit, then upload the same tender to resume.'
                  : 'The tender was read; the documents it contains state no technical requirement the pipeline could extract. There is nothing to match.'}
              </p>
              <button type="button" className="button" onClick={reset}>
                Upload another tender
              </button>
            </section>
          ) : phase === 'ready' ? (
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
                  focus={tender ? tenderCategory(tender.requirements) : null}
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
          ) : (
            <section className="panel">
              <h2>Saved requirements</h2>
              <p className="muted">Resume and complete extraction before starting manufacturer comparison.</p>
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
          ? 'The pages and bill of quantities are being read. Results appear when this reading attempt finishes or pauses.'
          : `${plural(read, 'requirement', 'requirements')} in the last saved reading attempt. Updated results appear when the current attempt finishes or pauses.`}
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

/** A category name as a heading: `valves` is a stored key, not a sentence. */
function titleOf(category: string): string {
  return category.charAt(0).toUpperCase() + category.slice(1)
}

/**
 * The category the tender's own items name (FR-16).
 *
 * The tender row carries a coarse category, and it is stored empty in practice,
 * so the answer comes from the items the tender was read into - each
 * requirement already ships with its item's category. Only a majority counts:
 * items that name no category, or two equally, leave the picker as it was
 * rather than narrowing it on a guess.
 */
function tenderCategory(rows: RequirementRow[]): string | null {
  const counts = new Map<string, number>()
  for (const row of rows) {
    // `unknown` is what an item stores when its words name no category
    // (FR-16): it is the absence of an answer, not a category of its own.
    if (!row.category || row.category === 'unknown') continue
    counts.set(row.category, (counts.get(row.category) ?? 0) + 1)
  }
  let best: string | null = null
  let top = 0
  let tied = false
  for (const [name, count] of counts) {
    if (count > top) {
      best = name
      top = count
      tied = false
    } else if (count === top) {
      tied = true
    }
  }
  return tied ? null : best
}

/** The rows of the picker, under the category each one is grouped by. */
function groupByCategory(
  rows: ManufacturerRow[],
  focus: string | null,
): { key: string; rows: ManufacturerRow[] }[] {
  const groups = new Map<string, ManufacturerRow[]>()
  for (const row of rows) {
    // A manufacturer that covers the tender's category is listed there even
    // when its documents name others too: that is the group the user is
    // looking for. The rest go under the first category they declare.
    const key = focus && row.categories.includes(focus) ? focus : (row.categories[0] ?? '')
    const list = groups.get(key) ?? []
    list.push(row)
    groups.set(key, list)
  }
  const keys = [...groups.keys()].sort((left, right) => {
    if (left === focus) return -1
    if (right === focus) return 1
    if (left === '') return 1
    if (right === '') return -1
    return left.localeCompare(right)
  })
  return keys.map((key) => ({ key, rows: groups.get(key) ?? [] }))
}

function ManufacturerPicker({
  rows,
  selected,
  focus,
  onChange,
}: {
  rows: ManufacturerRow[]
  selected: string[]
  focus: string | null
  onChange: (names: string[]) => void
}) {
  function toggle(name: string) {
    onChange(
      selected.includes(name)
        ? selected.filter((item) => item !== name)
        : [...selected, name],
    )
  }

  const groups = focus ? groupByCategory(rows, focus) : []
  const matching = focus ? (groups.find((group) => group.key === focus)?.rows ?? []) : []

  function checklist(list: ManufacturerRow[]) {
    return (
      <ul className="checklist">
        {list.map((row) => (
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
    )
  }

  return (
    <div className="picker">
      {focus && (
        <p className="muted">
          {`This tender's items are ${focus}, and ${plural(matching.length, 'manufacturer has', 'manufacturers have')} documents in that category. The list is grouped by the category each manufacturer's documents declare.`}
        </p>
      )}
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
        {focus && matching.length > 0 && (
          <button
            type="button"
            className="link"
            onClick={() => onChange(matching.map((row) => row.name))}
          >
            {`Only ${focus} (${matching.length})`}
          </button>
        )}
      </div>
      {focus ? (
        groups.map((group) => (
          <section key={group.key} className="picker-group">
            <h3>
              {group.key === '' ? 'No category recorded' : titleOf(group.key)}{' '}
              <span className="muted">{group.rows.length}</span>
            </h3>
            {checklist(group.rows)}
          </section>
        ))
      ) : (
        checklist(rows)
      )}
    </div>
  )
}
