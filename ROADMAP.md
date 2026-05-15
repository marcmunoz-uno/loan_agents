# Tranchi Deal Flow Agents — Roadmap

10 agents to fully automate the deal lifecycle for real estate investors on Tranchi.ai.

---

## ✅ Agent 1: AI Loan Officer
**Gap:** "Automated Funding System" on tranchi.ai homepage leads to a Typeform. No backend.
**What it does:** Pre-qualifies borrowers, routes them to the right loan product (DSCR / Fix & Flip / BRRRR / Conventional / Private), collects documents, submits to lender partners, tracks underwriting through to funding.
**Complexity:** M
**Integrations:** 6 lender partner APIs (Lima One, Kiavi, New Silver, LendingOne, Roc Capital, Anchor Loans), Tranchi web app user DB, tranchi-outbound-agent for comms
**Status:** Scaffolded in this repo

---

## ✅ Agent 2: Transaction Coordinator
**Gap:** "Transaction Automation Layer" on tranchi.ai homepage. Partial implementation in main app — no milestone tracking, no contingency deadline management, no party coordination.
**What it does:** Opens a deal file on PSA execution, generates a 16-milestone timeline, tracks all contingency deadlines with warning levels, coordinates buyer/seller/agents/title/inspector/lender, synthesizes status updates.
**Complexity:** M
**Integrations:** Tranchi web app (PSA data), tranchi-outbound-agent (voice/iMessage escalations), title companies, lenders
**Status:** Scaffolded in this repo

---

## ✅ Agent 3 (new): Loan Processor
**What it does:** Pre-underwrites every file before it hits a lender's desk. Reads lender guidelines (9 products across 6 lenders), runs DSCR/LTV/FICO checks, generates condition lists, flags red flags, drafts credit memos via LLM, and fires Arive/Zapier `ready_for_underwriting` event on clean files.
**Complexity:** M
**Integrations:** Tranchi - Loan Officer, Arive LOS via Zapier (auto-pushes clean files), 9 lender guideline docs + `guidelines_index.json`
**Status:** Scaffolded in this repo
**To go live:** Add real Zapier webhook URLs to `.env`, tune DSCR thresholds if lenders change programs, connect to Arive inbound webhook from Zapier

---

## Agent 4 (was 3): Title & Escrow Coordinator
**Priority:** 1 (next to build — blocking TX Coordinator for full close automation)
**Gap:** Title ordering and escrow management are manual. No integration with title companies.
**What it does:** Submits title order to title company API, tracks title search status, flags liens/encumbrances, coordinates escrow account funding, monitors wire instructions, confirms recording.
**Complexity:** M
**Integrations:** Title company APIs (Doma, Qualia, SoftPro, Ramquest), escrow wire APIs, county recorder feeds
**Why next:** Title is the single most common close delay. Automating it unlocks real close-day automation.

---

## Agent 4: Inspection Coordinator
**Priority:** 2
**Gap:** Inspections are scheduled manually. Inspection reports are uploaded manually and never parsed.
**What it does:** Finds and schedules a licensed inspector near the property, sends calendar invites to buyer and listing agent, receives the PDF inspection report, parses it to extract major issues (roof, HVAC, foundation, plumbing), auto-generates a repair request letter based on negotiation strategy, tracks seller response.
**Complexity:** M
**Integrations:** HomeAdvisor/Thumbtack/Angi APIs for inspector sourcing, Google Calendar, PDF parsing (pdfplumber or Claude with vision), email/SMS via outbound agent
**Why next:** Inspection coordination is the highest-volume manual task for a TX coordinator during the first 10 days.

---

## Agent 5: Property Manager Sourcer
**Priority:** 3
**Gap:** Beyond Google Places — no RFP flow, no scoring, no recommendation.
**What it does:** For a given property, finds 10+ property management companies in the market, sends a standardized RFP (management fee, leasing fee, vacancy policy, tenant screening criteria, software used, references), collects responses, scores them on a rubric, recommends the top 3 with a comparison matrix.
**Complexity:** L
**Integrations:** Google Places API, email (SendGrid), PM company websites (web scraping), NARPM directory
**Why:** Buy-and-hold investors need a PM before or immediately after close. This is currently a time-consuming manual research task.

---

## Agent 6: Listing Agent Liaison
**Priority:** 4
**Gap:** All back-and-forth with the seller's listing agent is manual. No structured offer tracking.
**What it does:** Handles offer submission and counter-offer negotiation with the listing agent, tracks offer history, suggests counter strategies based on deal economics, drafts addendums and cover letters, escalates to human investor at decision points.
**Complexity:** L
**Integrations:** Email/SMS/iMessage via outbound agent, Dotloop/DocuSign for e-signatures (TODO), Tranchi's `placed_offers` table
**Why:** Investors making 10+ offers/month spend hours on listing agent emails. Full automation here would be a major differentiator.

---

## Agent 7: Cash Buyer Recruiter
**Priority:** 5
**Gap:** Tranchi has a `cash_buyers` table but no autonomous outreach to grow it.
**What it does:** Identifies cash buyers from public records (recent cash purchases, repeat buyer patterns), reaches out via LinkedIn, Facebook, and direct mail, qualifies them (proof of funds, buy box criteria, timeline), adds qualified leads to the Tranchi cash buyer network.
**Complexity:** L
**Integrations:** PropStream/BatchData (public records), LinkedIn API or browser automation, Facebook Marketplace, direct mail API (Lob), Tranchi's `cash_buyers` and `deal_blasts` tables
**Why:** A bigger cash buyer list = faster wholesale assignments. This is a pure growth lever.

---

## Agent 8: Insurance Quote Bot
**Priority:** 6
**Gap:** Tranchi suggests investors "get insurance" with no actual quote flow.
**What it does:** Collects property details, gets actual quotes from real carrier APIs (not estimates), compares landlord/dwelling/vacant property policies, recommends the best fit, initiates binding with the chosen carrier.
**Complexity:** S
**Integrations:** EZLynx, Vertafore, or direct carrier APIs (State Farm, Farmers, Travelers commercial lines); alternatively, Coterie or Lemonade APIs for programmatic quotes
**Why:** Every deal needs insurance before close. This is a concrete, bounded problem with available APIs.

---

## Agent 9: Tax / 1031 Advisor
**Priority:** 7
**Gap:** Tax guidance on Tranchi is absent. 1031 exchanges are mentioned in investor profiles but there's no support.
**What it does:** Explains 1031 exchange rules and timelines, tracks identification period (45 days) and exchange period (180 days), suggests like-kind properties, maintains cost basis tracking across the portfolio, explains depreciation, flags when to consult a CPA (always), prepares K-1 summaries for partnership deals.
**Complexity:** L
**Integrations:** Cost basis DB (new table), IRS FIRPTA/1031 rule set (static knowledge), CPA referral network, Tranchi portfolio data
**Why:** Tax is a top concern for investors with 5+ properties. Automation here is rare and sticky.
**Risk:** High. Tax advice has legal exposure. Every output must carry a "consult your CPA" disclaimer and this agent should never give specific tax advice, only educational content and tracking.

---

## Agent 10: Working Capital
**Priority:** 8
**Gap:** No flow on Tranchi for the short-term capital real estate investors actually need between deals — down payments, rehab budgets, earnest money, gap funding.
**What it does:** Rate-shops short-term working capital across personal loans (SoFi, LightStream, Marcus, Upstart, Discover, Best Egg, Upgrade), business lines of credit (Bluevine, OnDeck, Fundbox), HELOCs, 0% intro APR personal/business cards, and Solo-401k/SDIRA loans. Surfaces the apples-to-apples all-in cost (APR + origination fees + term) and walks the investor through the lender's application — without ever typing SSN on their behalf.
**Complexity:** M
**Integrations:** Lender brokers via email (Zapier MCP — Gmail), rate log via Zapier MCP (Google Sheets), co-pilot UX in the Tranchi web app for the SSN-handoff step. Long-term: rate-aggregator APIs (Even, Plaid Loans).
**Why:** Working capital is the #1 friction point between investor deals — they have a good deal, just not the cash this week. A bot that rate-shops in 60 seconds and hands them a ranked shortlist solves a real problem without taking on regulatory risk.
**Risk:** Moderate. State licensing for loan brokerage varies; the bot rate-shops and educates but never submits applications — the investor types SSN and clicks submit themselves. APRs above 36% are refused; payday / title-loan / MCA products are never recommended.

---

## Suggested Build Order

| Sprint | Agents | Rationale |
|--------|--------|-----------|
| 1 | Loan Officer + TX Coordinator | Fill the two advertised gaps (this repo) |
| 2 | Title & Escrow Coordinator + Inspection Coordinator | Complete the close automation chain |
| 3 | Property Manager Sourcer + Insurance Quote Bot | Post-close investor needs |
| 4 | Listing Agent Liaison + Cash Buyer Recruiter | Growth levers |
| 5 | Tax/1031 Advisor + Capital Raiser | High value, high risk — legal review required |
