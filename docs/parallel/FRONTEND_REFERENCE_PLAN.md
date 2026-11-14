# Tender Genie reference and frontend proposal - 2026-11-14

Reference inspected with the reverse-engineer-anything skill. REA launched an
owned headless Chrome profile at https://www.tendergenie.ai/, waited 1500ms and
captured screenshot, DOM and accessibility at 1440x1000. The provider reports
cleanup terminated-owned-process. Evidence identity:
ev_96fcd0f6831255863c15effc5b00f90d1cd97c283cec62975480a5851ad86c70.
Screenshot SHA256:
1cbf713984412ce55ca2a033ed49fde1dc0dafe5abfe897d0cdc9ed52858bf9d.

Observed: light navigation, dark homepage hero, orange accents/buttons, large
sans-serif headings, generous spacing and staged workflow illustrations. The
public homepage illustrates source-linked review and BOQ rows. Public valve page
https://www.tendergenie.ai/industries/valve-manufacturers advertises a specification
correlation matrix with requirement/catalogue columns. Public descriptions are
marketing evidence, not proof of private dashboard behavior, accuracy or speed.
No login, upload, demo booking or private API action was performed.

## Implementation approved and completed - 2026-11-14

Keep the existing React/Vite app, routes and backend contracts. Adapt visual
language to a working review workspace rather than building the marketing site.
Use original project branding. Add no authentication, chat, team permissions,
drawing recognition or other advertised capabilities outside this assignment.

1. Shared shell: compact navigation, neutral backgrounds, orange primary actions,
   sans-serif hierarchy, accessible focus/contrast and responsive spacing. Keep
   review/error/status colors semantically distinct from brand orange.
2. Upload/reading: upload and source list, stage/page progress, clear attachment
   roles, effective limits, separate extraction completeness and review counts.
   Preserve resume/read-again, context clauses and failed-page visibility.
3. Requirement review: item grouping and source labels with original values,
   ranges, units and source links. Do not infer support for editing/review actions
   that the API cannot persist.
4. Comparison/report: manufacturer summary, searchable/filterable requirement
   matrix, requirement versus capability and verification status; reuse the
   EvidenceDrawer for document/page/quote/model/conditions. Keep no-evidence,
   failing documented capability, unsupported and review states separate.
5. Verify existing frontend tests, build and lint; add focused interaction checks
   where controls change. Check desktop/mobile, keyboard and long tables. Run
   live end-to-end verification separately; styling never implies accuracy passed.

Current implementation candidates: frontend/src/App.tsx, styles.css and the
existing UploadPage, RunPage, ReportPage, AssessmentTable, EvidenceDrawer and
ReviewQueue components. Reuse these rather than introducing another framework.
Inspection itself changed no application code. The approved implementation now
adapts the existing shell/theme, adds the catalogue capability column and local
search, clears search on review-queue navigation, and labels unverified readings.
Unsupported rows show no technical assessment. The upload target now supports
keyboard file selection and actual drag/drop through the existing PDF validator.
No backend contract, evidence gate, score or database change.

Validation:42 frontend tests,production build and lint passed. REA observed the
live upload UI at1440x1000 and390x844,plus a synthetic report at1440x1000.
Temporary synthetic visual fixtures were removed. No paid call or tender read.
Private dashboard equivalence and live tender-to-report correctness remain
unverified. Source inspection evidence is recorded above;implementation capture
identities:ev_aecb15591a7f76696cca8e4fe82c178511a1bde406218831b249af1b40e4c425,
ev_69093387ea5a85c464b7df6655755f0e21029aa3b9edb5d957b7b1c7d622635b,
ev_2bc72365eff29e4cca2a023f0bb4f883d7153c523db633dae63b908fa5e84e56.
