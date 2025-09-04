# Google Docs ↔ Cast (pull-only)

This document explains how to use the `cast gdoc` commands to keep a local Cast note linked to a Google Doc. The note stores only the Google **Doc URL**; the Doc ID is derived from the URL at runtime.

> **Model:** Google Doc is the source of truth. Cast pulls on demand.

## Install

Google Docs integration is included by default when installing cast-cli:

```bash
# Install with uv (recommended)
uv tool install --editable ./apps/cast-cli

# Or with pip
pip install -e apps/cast-cli
```

## Auth options

**Service Account (recommended for automation)**
1. Create a service account in your GCP project.
2. Download the JSON key and set:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
   ```
3. Share any target Drive folder/docs with the service account email.

**OAuth (user flow)**
1. Place your OAuth client at: `.cast/google/client_secret.json` (in your Cast root).
2. The first `cast gdoc` command will launch a browser to authorize and will save
   `.cast/google/token.json` for future runs.

## Create and link

```bash
cast gdoc new "Decision: Deprecate Legacy Parser" \
  --dir Notes/Decisions \
  --folder-id <DriveFolderID> \
  --share-with teammate@example.com --share-with other@example.com
```

This will:
- Create an empty Google Doc titled `Decision: Deprecate Legacy Parser`
- Create `Cast/Notes/Decisions/(GDoc) Decision: Deprecate Legacy Parser.md` with YAML:
  ```yaml
  ---
  title: "Decision: Deprecate Legacy Parser"
  cast-id: "<generated>"
  cast-version: 1
  url: "https://docs.google.com/document/d/<DocID>/edit"
  last-updated: "YYYY-MM-DDTHH:MM±TZ"
  ---
  _This file is generated from Google Docs..._
  ```

Teammates click `url` to edit the Doc.

## Pull updates

```bash
cast gdoc pull Cast/Notes/Decisions/Decision: Deprecate Legacy Parser.md
```

- Exports the Doc as Markdown and overwrites the **body** of the note (YAML preserved).
- Updates `last-updated` and may record the Doc `revision_id` internally (best effort; not stored in YAML).

### Legacy notes

Older notes may still contain a `document_id` field. On the next pull:

- If `url` is missing but `document_id` exists, the CLI synthesizes a canonical `url`.
- `document_id` is removed from YAML.

## Notes / constraints

- Drive export responses are capped in size; for very large Docs consider splitting or using multiple notes.
- Comments/suggestions in Docs are not pulled (we only pull the final rendered content).
- This integration is **pull-only** by design. Edit in Google Docs; commit pulled Markdown to Git.
- `cast-id` is generated on `new` so the file participates in sync immediately.
- Only the Doc `url` is stored; the Doc ID is parsed from the URL each time.