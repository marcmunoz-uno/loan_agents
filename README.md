# loan_agents

This is the brain behind Tranchi's AI loan officer. When someone fills out the qualification form on the website, **this** is what reads their bank statement, does the math, writes a pre-qualification letter, and emails it to them — usually within a minute, with no human involved.

---

## What happens when somebody fills out the Typeform

Imagine a borrower named **Jane** filling out the qualification form on her phone.

1. **Jane finishes the form.** She typed her name, email, phone, credit-score guess — and she uploaded a PDF of her latest bank statement (showing $60,000 sitting in her account). She taps **Submit**.

2. **Typeform calls our server.** Typeform takes everything Jane wrote and POSTs it to `https://loan-agents.onrender.com/api/loan/webhook/typeform-submit`. Think of it like Typeform texting our server: "hey, Jane just finished — here's her info."

3. **Server checks it's really Typeform.** We use a secret password (an HMAC signature) to make sure the request isn't a hacker pretending to be Typeform.

4. **Server saves Jane's answers** into the `loan_borrower_intakes` table. Now there's a record of "Jane filled out the form on May 25."

5. **Server sees Jane uploaded a bank statement** → kicks off the AI work in a **background thread**, then *immediately* tells Typeform `200 OK`. This part matters: Typeform gets impatient — if we take more than 10 seconds it thinks we crashed and retries. So we say "thanks!" fast and do the slow work behind the scenes.

6. **In the background, the AI loan officer reads the bank statement.**
   - Downloads Jane's PDF from Typeform's CDN
   - Sends the PDF to Claude (the AI), basically asking: *"what's the ending balance on this statement?"*
   - Claude reads it like a person would and replies `{"ending_balance": 60000}`

7. **Server does the math.** $60,000 in liquid cash means Jane can probably afford a house in the **$139,000 – $164,000** range (with 20% down, ~3% closing costs, and 6 months of mortgage payments held back as reserves). The math is a binary-search solver — it keeps guessing house prices until it finds the biggest one Jane's cash can actually cover.

8. **Server builds the pre-qualification letter PDF.** Munoz, Ghezlan & Co. letterhead, *"we're pleased to advise you have been conditionally pre-qualified,"* a bulleted conditions list, Marc's signature, 90-day expiration, the works. Built fresh with Jane's specific numbers.

9. **Server uploads the PDF to S3** so Gmail can grab it from a public URL.

10. **Server sends Jane the email** via Zapier → Gmail → with the PDF attached. From `Marc Munoz <replies@munoz.ltd>`, subject *"Your Pre-Qualification — Non-QM DSCR Loan."*

11. **Server updates Jane's row** with `letter_sent`, the `letter_id`, and her computed $60,000 liquidity — so later you can look up "what did Jane get?" and see the full audit trail.

**End result**: Jane finishes the form on her phone, and ~30 seconds later her email pings with a formal pre-qualification letter PDF saying she's good for a $139K–$164K house. She had to do nothing else. The AI read her bank statement, did the math, drafted the letter, sent it — without a human touching it.

**If Jane didn't upload a bank statement** → we can't compute her cash, so we skip the formal letter and send her a friendlier "thanks, you're soft-qualified, please send us your statements" email instead. Nobody walks away empty-handed.

---

## The three things this service does

1. **AI Loan Officer** (`/api/loan/*`) — handles the borrower side: prequal scoring, application state machine, auto-generated pre-qualification letters with PDF + email, the Typeform auto-fire described above.
2. **AI Loan Processor (Casey)** (`/api/processor/*`) — handles the internal side after docs come in: scores files against 9 lender programs, generates underwriting conditions, drafts the credit memo.
3. **Intake pipeline** (`/api/intake/*`) — accepts document uploads to S3, runs them through Claude vision to classify and extract fields, fires the prequal letter automatically when the doc checklist completes.

---

## Where to go next

- 🏗 **Engineering detail** → [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full endpoint inventory, env vars, tests, deployment, every capability with its status.
- 📨 **Prequal letter format / how to send a test** → [`.claude/skills/tranchi-prequal-letter/SKILL.md`](.claude/skills/tranchi-prequal-letter/SKILL.md) — the canonical recipe for the letter email + PDF.
- 🧪 **Tests** → `python -m pytest -q` (223 tests as of writing).
- 🚀 **Deployed** at https://loan-agents.onrender.com (auto-deploys from `main`).
