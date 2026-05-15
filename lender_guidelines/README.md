# Lender Guidelines

This directory stores underwriting guidelines for each lender-product combination supported by the Tranchi - Loan Processor.

## Structure

Each file covers one lender + product combination:

```
<lender_slug>_<product_type>.md   — Full narrative guidelines (markdown)
guidelines_index.json              — Machine-readable matrix for fast programmatic lookup
```

## How the Loan Processor uses these

1. `guideline_engine.py` loads `guidelines_index.json` at startup to build the matching matrix.
2. When a pre-underwriting check runs, the engine filters the index by FICO / LTV / DSCR / property type / state to produce a ranked lender list.
3. For the top matches, the engine loads the full `.md` file and passes it as context to the LLM when generating the credit memo and condition list.

## Adding a new lender

1. Create `<lender_slug>_<product>.md` following the template structure below.
2. Add an entry to `guidelines_index.json` with all scalar fields populated.
3. Run `python -m loan_processor.guideline_engine` to verify the index loads cleanly.

## Guideline file template

```markdown
# [Lender] — [Product]

## Quick Reference
- Min FICO: 660
- Max LTV (purchase): 80%
- Max LTV (cash-out): 75%
- Min DSCR: 1.10
- Min loan: $100,000
- Max loan: $3,500,000
- Reserves: 6 months PITI

## Eligible Property Types
...

## Ineligible / Restrictions
...

## Rate Sheet (ranges — pull current pricing from lender portal)
...

## Underwriting Hot Buttons
...

## Required Conditions (standard package)
...

## Submission Process
...

## Lender Contact
...
```

## Rate sheet note

Rates change weekly. The ranges in each file and in `guidelines_index.json` (`rate_range_pct`) are the historical bands for reference only. Always pull live pricing from the lender's broker portal or rate lock desk before quoting. The Loan Processor will never quote a specific rate to a borrower — that goes to the MLO.

## Updating guidelines

Lender programs change. When a lender updates their guidelines (FICO floors, LTV caps, program additions):
1. Update the `.md` file.
2. Update `guidelines_index.json`.
3. Bump `last_updated` in the JSON entry if present.
4. Commit with message: `chore(guidelines): update <lender> <product> — <what changed>`
