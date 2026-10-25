# Tender frontend (PRD 12)

React + Vite. The backend does the reading, matching and scoring; this app
uploads a tender, polls the run and renders the report the API already stores.

```bash
npm install
cp .env.example .env    # optional; the default is http://localhost:8000
npm run dev
```

- `npm run dev` — development server on <http://localhost:5173>
- `npm test` — the pure report helpers (labels, counts, filters)
- `npm run lint` — oxlint
- `npm run build` — type-check and production build in `dist/`

`VITE_API_URL` points the client at the backend. The backend's
`CORS_ORIGINS` must include the address this app is served from
(`http://localhost:5173` by default).

The screens are upload → run progress → report (ranking, status filters,
requirement table, evidence drawer, review queue). A run is only rendered when
the API says it has stopped; a partial report says it is partial.
