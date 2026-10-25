/** A report that exercises every status, used by the render tests.
 *
 * Synthetic, like every test fixture in this project: the documents, quotes
 * and pages are made up and say so.
 */

import type { Report, ReportRequirement, ResultRow } from './types'

export function result(overrides: Partial<ResultRow> = {}): ResultRow {
  return {
    manufacturer_id: 'm1',
    manufacturer: 'Acme',
    status: 'full_match',
    score: 1,
    verification_outcome: 'passed',
    applicability: 'model_level',
    capability_text: 'Body: ASTM A351 CF8M (SS316)',
    confidence: 'high',
    review_required: false,
    review_reason: null,
    evidence: {
      fact_id: 'fact-1',
      page: 12,
      quote: 'Body: ASTM A351 CF8M (SS316)',
      document_id: 'doc-1',
      document: 'acme-d-series.pdf',
      doc_type: 'catalogue',
      sha256: 'b'.repeat(64),
      url: 'https://manufacturer.test/acme-d-series.pdf',
    },
    ...overrides,
  }
}

export function requirement(
  overrides: Partial<ReportRequirement> = {},
): ReportRequirement {
  return {
    requirement_id: 'r1',
    item_id: '1',
    item_title: 'Gate valve',
    category: 'valves',
    text: 'The body shall be stainless steel',
    source_text: null,
    type: 'material',
    mandatory: true,
    page: 2,
    value: null,
    unit: null,
    results: [result()],
    ...overrides,
  }
}

export function report(overrides: Partial<Report> = {}): Report {
  return {
    run: {
      id: 'run-1',
      tender_id: 't1',
      status: 'done',
      started_at: '2026-10-25T10:00:00+00:00',
      error: null,
      manufacturers: ['Acme', 'Beta'],
      corpus_hash: 'c'.repeat(64),
    },
    tender: {
      id: 't1',
      filename: 'synthetic-tender.pdf',
      sha256: 'd'.repeat(64),
      page_count: 4,
      pages: {
        declared: 4,
        accounted: 4,
        unaccounted: 0,
        counts: { parsed: 4 },
        unresolved: [],
        skipped: [],
        incomplete_ingestion: false,
      },
    },
    config: {
      llm_model: 'synthetic-model',
      embedding_model: 'synthetic-embedding',
      prompt_version: '1',
      schema_version: '1',
      scoring_policy_version: '1',
    },
    results: { stored: 6, expected: 6, complete: true },
    manufacturers: [
      {
        manufacturer_id: 'm1',
        manufacturer: 'Acme',
        rank: 1,
        compatibility_percent: 80,
        coverage_percent: 75,
        status_counts: {
          full_match: 1,
          no_evidence_found: 1,
          unsupported_requirement: 1,
        },
        fails_mandatory: false,
        review_count: 1,
      },
      {
        manufacturer_id: 'm2',
        manufacturer: 'Beta',
        rank: 2,
        compatibility_percent: 40,
        coverage_percent: 50,
        status_counts: {
          full_match: 1,
          missing_capability: 1,
          unsupported_requirement: 1,
        },
        fails_mandatory: true,
        review_count: 1,
      },
    ],
    requirements: [
      requirement({
        results: [
          result(),
          result({
            manufacturer_id: 'm2',
            manufacturer: 'Beta',
            status: 'missing_capability',
            score: 0,
            review_required: true,
            review_reason: 'The quoted body material is brass, not stainless steel',
          }),
        ],
      }),
      requirement({
        requirement_id: 'r2',
        text: 'The end connection shall be flanged',
        results: [
          result({
            status: 'no_evidence_found',
            score: null,
            review_required: true,
            review_reason: 'The corpus says nothing about the end connection',
            evidence: null,
          }),
          result({
            manufacturer_id: 'm2',
            manufacturer: 'Beta',
            status: 'full_match',
          }),
        ],
      }),
    ],
    unsupported_requirements: [
      requirement({
        requirement_id: 'r3',
        text: 'Warranty shall be 24 months',
        type: 'commercial',
        results: [
          result({
            status: 'unsupported_requirement',
            score: 0,
            evidence: null,
          }),
          result({
            manufacturer_id: 'm2',
            manufacturer: 'Beta',
            status: 'unsupported_requirement',
            score: 0,
            evidence: null,
          }),
        ],
      }),
    ],
    review_queue: [
      {
        requirement_id: 'r1',
        manufacturer: 'Beta',
        status: 'missing_capability',
        reason: 'The quoted body material is brass, not stainless steel',
      },
      {
        requirement_id: 'r2',
        manufacturer: 'Acme',
        status: 'no_evidence_found',
        reason: 'The corpus says nothing about the end connection',
      },
    ],
    status_meanings: {
      full_match: 'The documented capability meets or exceeds the requirement, with a verified quote.',
      partial_match: 'Close but not meeting it, or met only under a condition.',
      missing_capability: 'The documents state a failing value, or that it is not offered.',
      unsupported_requirement: 'Not technically verifiable; excluded from the score.',
      no_evidence_found: 'Technical and verifiable, but the corpus is silent.',
    },
    ...overrides,
  }
}
