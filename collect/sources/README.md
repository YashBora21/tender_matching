# Source configs

One JSON file per category. Adding a category is adding a file here — no code
changes (FR-6).

```json
{
  "category": "gate_valves",
  "entries": [
    {
      "manufacturer": "Acme Valves",
      "url": "https://acme.example/gv-datasheet.pdf",
      "doc_type": "datasheet",
      "model_or_family": "GV series"
    }
  ]
}
```

Entry fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `manufacturer` | yes | The manufacturer name. The slug of this name is the stable key in the manifest. |
| `url` | yes | Where the document is fetched from. |
| `doc_type` | yes | One of the types in `backend/data/doc_types.json`. An unknown type is not collected. |
| `model_or_family` | yes | The model or family this document covers. |
| `models` | no | Specific models covered, when the document covers more than the family. |
| `secondary` | no | `true` for a distributor or reseller document. Never overrides a manufacturer document (FR-7). |
| `manual` | no | `true` for a document obtained by hand. It is recorded but not downloaded (FR-3). |
| `filename` | no | The local file name for a manual document. Defaults to the URL's last path part. |
| `version_date` | no | The document's own revision date (`YYYY-MM-DD`). Not the retrieval date. |
| `note` | no | Anything worth recording: "gated", "scanned", "superseded". |

`*.example.json` files are templates and are skipped by the collector.

Then run it:

```
python collect/collect.py --dry-run          # report only, writes nothing
python collect/collect.py                    # fetch and write the manifest
python collect/collect.py --category gate_valves
```

## What the manifest records

Each document gets one record, keyed by `(category, url)`:

| Field | Meaning |
| --- | --- |
| `status` | `new`, `unchanged`, `changed`, `restricted`, `manual` or `failed`. |
| `sha256` | The hash of the bytes on disk. `changed` also records `previous_sha256`. |
| `retrieved_at` | When the file was fetched. Not the document's own date. |
| `version_date` | The document's own revision date, from the config. |
| `is_secondary` | A distributor document. It never overrides a manufacturer document. |
| `restriction_note` | Why a fetch was refused, when it was. |

Re-running is safe. A document whose hash has not changed is `unchanged` and
is not re-ingested; one whose hash has changed is `changed`, with the
previous hash kept so the ingestion phase can tell what moved. A document
whose file has been deleted from disk is fetched again, so the manifest never
claims a file is there when it is not.

The collector respects `robots.txt`. A path the site's `robots.txt` disallows
is recorded as `restricted` and is not fetched. A `robots.txt` that cannot be
read is not a restriction — a server error is a broken site, not a policy —
and the fetch proceeds with the failure recorded as a note.
