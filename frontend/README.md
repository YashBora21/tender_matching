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

## Container

`Dockerfile` builds the app with Node and serves the static result from nginx
(the compose stack publishes it on <http://localhost:3000>). The backend address
is compiled into the bundle, so pass it as a build argument — and use the
address the browser will use, not `http://backend:8000`:

```bash
docker build --build-arg VITE_API_URL=http://localhost:8000 -t tender-frontend .
```

`nginx.conf` serves `index.html` for unknown paths so client-side routes work on
a refresh, and answers `/healthz` for the container healthcheck.
