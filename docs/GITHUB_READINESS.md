# GitHub readiness — 2026-11-11

This phase separates credentials, removes proven unused code and excludes local
experiments from publication. It does not establish matching accuracy.

## Credentials

Keep actual values only in the ignored `.env`. The local provider source keys
are preserved. A two-provider setup uses the following variable relationships;
replace placeholders privately, never in committed code:

```dotenv
LLM_API_KEY=<legacy Groq credential>
GEMINI_API_KEY=<Gemini credential>
LLM_TEXT_API_KEY=${GEMINI_API_KEY}
OCR_VISION_API_KEY=${LLM_API_KEY}
```

Define source values before references. `LLM_MODEL`/`LLM_BASE_URL` select the
text target; the independent OCR vision variables select a vision experiment.
Production OCR is Tesseract/Paddle. Passing another endpoint to `LLMClient`
requires an explicit `api_key`, so it cannot inherit a different provider key.
No provider request is needed to test routing. Restart the backend after editing
private configuration; source auto-reload is not a reliable environment reload.

## Cleanup evidence

Four private helpers had no callers: the old alias exact lookup, corpus directory
scanner, report page-count fallback and unused whole-table Markdown renderer.
They were removed together with an obsolete fake override, eleven unused imports
and two unused constants. Current row-aware parsing, shared page accounting,
normalization and ambiguity-safe alias resolution remain the active paths.

The reference scan found no runtime source file safe to delete after checking
imports, tests, CLI entries and dynamic use. The Paddle worker is launched as a
subprocess and is retained. Re-export modules,
migrations, synthetic tests, gold labels and historical audits are required
maintenance material, not unused production code. No runtime source file was
proven safe to delete. Seven generated pytest directories (179 temporary files)
were deleted. Local scratch, generated audit output and unseen tender inputs are
ignored; already tracked audit/gold files remain tracked.

## Checks and publication

Validation results and the tested commit will be recorded after this cleanup.
The existing RC tag is immutable; source changes need a new tested candidate.

From PowerShell at the repository root, review only committed source for push:

```powershell
git status --short
git diff --check
git ls-files .env backend/.env  # must print nothing
git log -5 --oneline
git remote -v                 # inspect locally; never share embedded credentials
```

Use an existing reviewed GitHub remote, or add your own repository as `origin`.
Then push the reviewed branch and its new RC tag explicitly. This phase does not
publish to GitHub or Docker Hub. Untracked local notes are not pushed by Git.
Do not use `git add .` to include unreviewed local artifacts. New generated audits
are ignored by default; use `git add -f <reviewed-audit-path>` after checking them.

## Acceptance still open

1. Intended pump input and confirmed full-tender labels, then valve/pump evaluation
   on the tested RC: false-confident claims, abstentions by cause, stage times,
   tokens and cost. Grouping stays off.
2. Vendor evidence/alias corrections driven by those frozen results, preserving
   historical data and never treating silence as a failed capability.
3. Persisted monthly/daily spend ledger and caps before unattended paid workloads.
   Per-run call/token limits alone do not enforce INR 1,000 per month.
4. Image builds, runtime smoke and deployment OCR/GPU validation. Published images
   and production acceptance are not implied by unit-test success.
