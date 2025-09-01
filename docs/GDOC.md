# Google Docs ↔ Cast (pull-only)

This document explains how to use the `cast gdoc` commands to keep a local Cast note linked to a Google Doc.

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
- Create `Cast/Notes/Decisions/Decision: Deprecate Legacy Parser.md` with YAML:
  ```yaml
  ---
  title: "Decision: Deprecate Legacy Parser"
  cast-id: "<generated>"
  cast-version: 1
  source:
    kind: google_doc
    url: "https://docs.google.com/document/d/<DocID>/edit"
    document_id: "<DocID>"
    pull_mode: "drive.export:text/markdown"
    media_dir: "media/gdoc/<DocID>"
    revision_id: null
    pulled_at: null
    do_not_edit: true
  ---
  _This file is generated from Google Docs..._
  ```

Teammates click `source.url` to edit the Doc.

## Pull updates

```bash
cast gdoc pull Cast/Notes/Decisions/Decision: Deprecate Legacy Parser.md
```

- Exports the Doc as Markdown and overwrites the **body** of the note (YAML preserved).
- Embedded images are extracted from data URIs to `media/gdoc/<DocID>/...` and links are rewritten
  to **relative** paths from the note.
- Updates `source.revision_id` and `source.pulled_at`.

Disable image extraction (keep data URIs):
```bash
cast gdoc pull Cast/foo.md --no-extract-images
```

## Notes / constraints

- Drive export responses are capped in size; for very large Docs consider splitting or using multiple notes.
- Comments/suggestions in Docs are not pulled (we only pull the final rendered content).
- This integration is **pull-only** by design. Edit in Google Docs; commit pulled Markdown to Git.
- `cast-id` is generated on `new` so the file participates in sync immediately.