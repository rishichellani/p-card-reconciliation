# P-Card & Expense Reconciliation: a Deterministic + LLM Hybrid Pipeline

A prototype that takes a corporate purchasing-card statement, collects employee justifications, audits them against a
written spending policy, and produces a balanced ERP journal entry. Deterministic code owns everything computable. A
free-tier LLM (Gemini, Groq or OpenRouter) is used for the one step that needs judgement.

Code: https://github.com/rishichellani/p-card-reconciliation

## The problem
Finance teams reconcile hundreds of card transactions a month. Most checks are arithmetic and rules. One is not:
*does this employee's explanation actually satisfy our spending policy?* "Team bonding" at a bar is not the same as
"dinner with Northwind procurement to discuss renewal terms".

## The design principle: use the LLM only where rules cannot
| Deterministic Python (no LLM) | LLM (qualitative only) |
|---|---|
| CSV/schema validation, quarantine of dirty rows | Is the justification specific and plausible? |
| Card to employee to cost center routing | Does it satisfy meal, gift, travel and home-office policy text? |
| Limits, receipt matching, duplicates, split purchases | Is it personal use dressed up as business use? |
| GL mapping, `Decimal` money, balanced double entry | |

Rule findings can only be **escalated** by the model, never downgraded. If the LLM approves something a rule flagged,
the flag stands.

## How it flows
```
bank statement CSV
   -> 1 Validate & route     (Pydantic; bad rows set aside with reasons)
   -> 2 Justifications       (entered by cardholders in the app, or loaded as clearly-labelled sample data)
   -> 3 Compliance audit     (rule checks first, then the LLM judges the justification against the policy text)
   -> 4 ERP journal          (balanced, tied to the statement total)
```
Each stage writes a JSON artifact (`01_raw_statement.json` ... `04_erp_journal_entry.csv`) instead of holding state
in memory.

## The app
A Streamlit app with two pages.
- **Live workflow:** create a batch from a statement, let each cardholder add a business purpose, attendees and receipt
  details, then run the audit. A transaction with no justification is flagged `MISSING_JUSTIFICATION` and skips the LLM.
  Nothing is invented. A **Load sample justifications** button fills the rest with labelled sample data (including a
  few planted problem cases) so the whole flow can be seen without typing 25 entries.
- **Results:** outcome summary and controls, a filterable transaction table with per-transaction detail, a
  **Follow a transaction** view that walks one transaction through all four stages, quarantined rows, the ERP journal,
  the audit trail, and an Excel export.

## Controls and audit trail
- **Immutable artifacts:** each stage's output is written once and made read-only. Re-running a stage into the same run
  is refused.
- **Hash chain:** every artifact stores the SHA-256 of its own payload and of the previous stage's payload, from the
  source CSV to the journal. `--verify RUN_ID` re-checks the whole chain, and editing any file is detected.
- **Append-only justification log:** each submission records who and when and commits to the previous entry's hash.
  A later submission supersedes an earlier one but never erases it. The log closes when the audit snapshots it.
- **Provenance is explicit:** every justification is labelled as entered by an employee or as sample data, in the app
  and in the Excel export, with a warning banner whenever sample data is present.
- **Journal checks:** the exporter refuses to write unless debits equal credits and the net card liability ties to
  the statement control total (purchases minus refunds). Every audited transaction must be represented.
- **Prompt-injection posture:** employee text is untrusted input, wrapped in delimiters and marked as data in the prompt.
  The LLM sees the justification, merchant, amount and department, not names, emails or card numbers.

## Resilient LLM routing
`utils/llm_client.py` is a small reusable client for any OpenAI-compatible endpoint.
- Priority failover across Gemini, Groq and OpenRouter, using whichever keys are configured.
- Per-provider throttling for free-tier rate limits, a cooldown after repeated failures, and permanent disable for
  rejected credentials or a retired model.
- JSON mode is used when supported and dropped automatically when not. Invalid output is retried on a different provider.
- A partial outage sends only the affected transactions to `MANUAL_REVIEW`. A **total** outage (the first three
  attempts all fail) stops the audit without saving anything, so it can be retried instead of baking an all-manual-review
  result into the record. A **Test connection** button shows which provider works before anything is locked.

## Data dictionary and reviewability
`docs/` holds Excel views of the input files and a data dictionary defining every field of the bank statement, the
employee directory and the policy rules: type, required or not, validation, and what each field drives downstream.

## Testing
35 offline tests cover money and date parsing, quarantine behaviour, provider failover, credential disabling, JSON repair
and retry, the hash chain and tamper detection, the append-only log, missing-justification handling, backup and restore
(including tampered and unsafe archives), and total versus partial LLM outage. A `--mock-llm` mode runs the whole
pipeline with no keys.

## Tech
Python 3.10+, Pydantic v2, Streamlit, OpenAI-compatible SDK client (Gemini / Groq / OpenRouter), `Decimal`, openpyxl, pytest.

## Honest limitations
This is a prototype, not a production system.
- **Sample data:** the statement, employees and policy are invented. No real cardholder data has been used.
- **No login:** in the app you choose who you are. An optional shared passcode gates access, but it is not identity control.
- **Receipts are typed in:** there is no image upload or OCR, and nothing verifies that the typed values match a real receipt.
- **Storage:** on a hosted free tier, files are lost when the app restarts, so the app has backup and restore. A real
  deployment needs a database or persistent disk.
- **Free-tier LLMs:** verdict quality varies, quotas run out, and providers may use prompts for training. During
  development the audit ran live on Groq and Gemini, and on a 26-transaction statement the failover kept the run going
  through rate limits and a provider error. Treat verdicts as a first pass for a human reviewer, not a decision.
- **Scale:** transactions are audited one at a time. It has not been load-tested.
