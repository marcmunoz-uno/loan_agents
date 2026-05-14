# Render Deployment Guide

## Prerequisites

1. A [Render](https://render.com) account
2. This repo pushed to GitHub (private repo OK)
3. `ANTHROPIC_API_KEY` from console.anthropic.com
4. `TRANCHI_API_SECRET` — same value as in `tranchi-outbound-agent`

---

## Steps

### 1. Connect the repo to Render

From the Render dashboard: New → Web Service → Connect Repository → select `tranchi-deal-flow-agents`.

Or use the CLI:
```bash
render deploy --yes
```

### 2. Set required environment variables

In Render dashboard → Environment tab, set:

| Variable | Value | Notes |
|----------|-------|-------|
| `TRANCHI_API_SECRET` | (your secret) | Must match main app and outbound-agent |
| `ANTHROPIC_API_KEY` | (your key) | Required for AI endpoints |
| `DB_PATH` | `/opt/render/project/data/dealflow.db` | Auto-set by render.yaml |
| `OUTBOUND_AGENT_URL` | `https://tranchi-outbound-agent.onrender.com` | Auto-set |
| `OPENAI_API_KEY` | (optional) | Only needed if Anthropic fails |
| `LENDER_WEBHOOK_SECRET` | (your secret) | Share with lender partners |

### 3. Deploy

Render auto-deploys from `main`. To deploy manually:

```bash
git push origin main
```

### 4. Verify

```bash
curl https://tranchi-deal-flow-agents.onrender.com/health
```

Expected response:
```json
{
  "service": "tranchi-deal-flow-agents",
  "status": "ok",
  "agents": ["loan_officer", "tx_coordinator"]
}
```

### 5. Seed production data (optional)

```bash
# SSH into Render shell (available on paid plans)
python ops/seed_data.py
```

---

## Disk persistence

The `render.yaml` mounts a persistent disk at `/opt/render/project/data`. SQLite file lives there. This survives deploys but not instance replacement. For production, migrate to the MySQL instance already used by the Tranchi web app — the schema is MySQL-compatible.

---

## MySQL migration (production)

When ready to move from SQLite to MySQL:

1. Point `DB_PATH` to `mysql://user:pass@host:3306/tranchi_deal_flow`
2. Update `shared/db.py` to use `mysql-connector-python` instead of `sqlite3`
3. Run `001_initial.sql` against the MySQL instance
4. Match the `user_id` foreign key references to Tranchi's `users.id` column

---

## Integration with tranchi-outbound-agent

The `shared/tranchi_client.py` calls:
- `POST /api/outreach/nurture` — to send follow-up messages on loan status changes
- `POST /api/outreach/call` — to initiate voice calls for time-sensitive deadlines

Set `OUTBOUND_AGENT_URL` in Render env vars to enable this.

---

## Integration with tranchi-ui (web app)

Call these endpoints from the Tranchi web app backend:
- `POST /api/loan/prequal` — when investor requests financing
- `POST /api/tx/open` — when PSA is uploaded/executed
- `GET /api/tx/:id` — for transaction status panel
- `POST /api/loan/chat` + `POST /api/tx/:id/chat` — for in-app AI chat
