---
name: tranchi-prequal-letter
description: |
  Send a Tranchi Non-QM DSCR pre-qualification letter as an HTML email with a
  matching PDF attached, in the exact format the production loan-agents
  pipeline emits. Use when the user says "send a prequal letter", "send a
  pre-approval email", "preapproval test", or wants to verify the
  Munoz/Ghezlan letterhead format end-to-end. Two paths: prod (POST the
  loan-agents endpoint with a real prequal_id) and ad-hoc test (render PDF
  locally via `render_letter_pdf` + send via Zapier MCP Gmail).
allowed-tools:
  - Bash(python3 *)
  - Bash(curl *)
---

# tranchi-prequal-letter

Sends the Tranchi pre-qualification letter. Body + PDF must match the
Munoz, Ghezlan & Co., Ltd. letterhead — the format is canonical as of
loan_agents commit `2e001d0` (May 19, 2026). The body HTML and the PDF
read identically by design.

## Production path (preferred)

Trigger the live system. Auto-handles DB audit row, S3 upload, presigned
URL, Zapier MCP Gmail send, and audit-row update.

```bash
curl -sS -X POST \
  "https://loan-agents.onrender.com/api/loan/prequal-letter/<prequal_id>" \
  -H "Authorization: Bearer $TRANCHI_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{}' | jq '.letter_id, .zap_fired, .mcp_send_status'
```

Optional body fields:
- `liquid_assets` — float, override the OCR-summed bank-stmt total (LO manual control)
- `monthly_rent` — float, override the property's monthly rent
- `skip_send` — bool, render + audit without firing the email

The route is `@require_tranchi_auth`-gated; the secret lives in Render envs.

## Ad-hoc test path (no DB, no auth)

Use when verifying the format from a dev machine. Renders the same PDF
+ HTML the prod pipeline would, sends via Zapier MCP directly.

```bash
cd ~/loan_agents
python3 <<'PY'
import base64, uuid, subprocess
from datetime import datetime, timezone, timedelta
from loan_officer.prequal_letter import render_letter_pdf, _render_email_html

letter_id = f"pql_{uuid.uuid4().hex[:14]}"
issued = datetime.now(timezone.utc)
expires = issued + timedelta(days=90)

pdf = render_letter_pdf(
    borrower_name="Marc Munoz",
    borrower_email="marc@munoz.ltd",
    max_pp_low=139000.0,
    max_pp_high=164000.0,
    issued_at=issued,
)
path = f"/tmp/{letter_id}.pdf"
open(path, "wb").write(pdf)

# Public URL: catbox.moe accepts unauthenticated uploads, no expiry.
# In prod the URL comes from S3 (path ends in `prequal-letters/<letter_id>.pdf`,
# which Gmail uses as the attachment filename).
url = subprocess.check_output(
    ["curl", "-sS",
     "-F", "reqtype=fileupload",
     "-F", f"fileToUpload=@{path}",
     "https://catbox.moe/user/api.php"],
).decode().strip()

html = _render_email_html(
    borrower_name="Marc Munoz",
    borrower_email="marc@munoz.ltd",
    max_pp_low=139000.0,
    max_pp_high=164000.0,
    issued_at=issued,
    expires_at=expires,
    pdf_url=url,
)
print("letter_id:", letter_id)
print("pdf_url:", url)
print("body_len:", len(html))
PY
```

Then send via Zapier MCP Gmail Send Email:

| Param | Value |
|---|---|
| `to` | `["marc@munoz.ltd"]` (or borrower email) |
| `subject` | `Your Pre-Qualification — Non-QM DSCR Loan` |
| `body` | HTML from `_render_email_html(...)` — **do not paraphrase** |
| `body_type` | `html` |
| `from_name` | `Marc Munoz` |
| `signature_delimiter` | `false` |
| `file` | the public URL from above |

The action key is `gmail.message:write`. Tool: `execute_zapier_write_action`.

## Format invariants

These MUST hold for the production pipeline. Check before shipping any
edit to `loan_officer/prequal_letter.py`.

- **Subject** — `Your Pre-Qualification — Non-QM DSCR Loan` (em-dash, not hyphen).
- **From name** — `Marc Munoz`. Gmail's default `From:` is `replies@munoz.ltd` (the alias on the connected account).
- **Body** — HTML, max-width 640px, Georgia serif, centered "MUNOZ, GHEZLAN & CO., LTD. / PRE-QUALIFICATION RESULTS" title block. `_render_email_html()` is the source of truth.
- **Conditions list** — six bullets in this order: Max PP range, title, appraisal, 20% down, rates 5.875%–8%, "Subject to loan program availability."
- **Signature** — Marc Munoz / Senior Loan Officer / `marc@munoz.ltd` / (917) 981-0032.
- **Expiration** — 90 days (`LETTER_VALIDITY_DAYS`).
- **Footer** — `Munoz & Co. Ltd  -  99 Wall Street, Suite 4041, New York, NY, 10005  -  Munoz.Ltd  -  Book a Call`.
- **Attachment filename** — `pql_<14-hex>.pdf` when S3 is configured (URL basename wins), `prequal_letter_pql_<14-hex>.pdf` when falling back to the self-hosted PDF endpoint (Content-Disposition wins). Both are acceptable.

The HTML body and the PDF content are deliberately kept identical so the
email reads the same with or without the attachment open.

## Verify the send

```text
Zapier MCP → gmail.message:read (Find Email)
  query: to:marc@munoz.ltd subject:"Pre-Qualification" newer_than:1d
  output: date, subject, from, attachment_filenames, body_snippet
```

Expect to see the rfc822 message id returned by Send Email reflected in
the inbox within seconds.

## Source files

- `loan_officer/prequal_letter.py` — PDF render + HTML body + send pipeline.
- `loan_officer/routes.py:921` — `POST /api/loan/prequal-letter/<prequal_id>`.
- `loan_officer/routes.py:984` — `GET /api/loan/prequal-letter/<letter_id>/pdf` (HMAC-tokenized public fallback).
- `shared/migrations/004_prequal_letters.sql` — audit table.
- `shared/zapier_mcp.py` — Zapier MCP transport.

## Last verified

2026-05-22 — sent test to marc@munoz.ltd via ad-hoc path (catbox URL),
Gmail msg id `19e5255c611128aa`. Body + attachment confirmed in inbox.
