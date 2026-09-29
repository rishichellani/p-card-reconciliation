# P-Card & Expense Reconciliation: a Deterministic + LLM Hybrid Pipeline

A local, production-style prototype that turns a raw corporate purchasing-card statement into a balanced, audit-ready
ERP journal entry. It uses free-tier LLM APIs (Gemini, Groq, OpenRouter) for the one step that needs judgement.

## The problem
Finance teams reconcile hundreds of card transactions a month. Most checks are arithmetic and rules. One is not:
*does this employee's explanation actually satisfy our spending policy?* ("Team bonding" at a bar is not the same as
"dinner with Northwind procurement to discuss renewal terms".)

## The design principle: use the LLM only where rules cannot
| Deterministic Python (no LLM) | LLM (qualitative only) |
|---|---|
| CSV/schema validation, quarantine of dirty rows | Is the justification specific and plausible? |
| Card → employee → cost center routing | Does it satisfy meal, gift, travel and home-office policy text? |
| Limits, receipt matching, duplicates, split purchases | Is it personal use dressed up as business use? |
| GL mapping, `Decimal` money, balanced double entry | |

Rule findings can only be **escalated** by the model, never downgraded. If the LLM approves something a rule flagged,
the flag stands.

## Architecture
```
transactions.csv ─► 1 Ingest & route ─► 2 Justifications ─► 3 Compliance audit ─► 4 ERP journal
employees.json      01_raw_statement    02_user_justified   03_compliance_        04_erp_journal_
spending_policy.md  .json               .json               audited.json          entry.csv
                    Pydantic, no LLM    seeded simulation   rules + LLM router    Decimal, no LLM
```
- **Strict schemas:** Pydantic models at every stage boundary. Amounts like `$1,234.50` and `(250.00)` are parsed and
  bad rows (bad date, `N/A` amount, missing merchant, non-USD, duplicate ID, unknown card) are quarantined with
  reasons rather than crashing the run. In the demo, 6 of 32 rows are quarantined and the run still completes.
- **Structured LLM contract:** the model must return JSON `{verdict, policy_sections, rationale, confidence, red_flags}`.
  It is extracted, normalised and schema-validated. Invalid output is retried on a different provider.
- **Prompt-injection posture:** employee text is untrusted input, wrapped in delimiters and flagged as data in the prompt.

## Resilient multi-provider LLM routing
`utils/llm_client.py` is a small reusable client for any OpenAI-compatible endpoint.
- Priority failover across Gemini → Groq → OpenRouter, using whichever keys are configured.
- Per-provider throttling for free-tier rate limits, cooldown after repeated failures, and permanent disable for bad
  credentials or a retired model.
- Automatic fallback when an endpoint does not support JSON mode.
- If every provider fails for a transaction, it becomes `MANUAL_REVIEW` and the pipeline keeps going.

## Immutable, tamper-evident audit trail
Every stage persists a JSON artifact instead of holding state in memory.
- Artifacts are **write-once** (atomic link, then read-only). Re-running a stage into an existing run is refused.
- Each envelope stores `payload_sha256` and `upstream_sha256`, forming a hash chain from the source CSV to the journal.
- The ERP CSV has a manifest with its own hash. `--verify RUN_ID` re-checks the whole chain; editing any file is detected.
- Every LLM decision is stored with its rationale, cited policy sections, confidence and `provider:model` that served it.

## ERP output controls
The exporter refuses to write a journal unless:
1. total debits equal total credits (`Decimal`, no floating point),
2. the P-Card clearing liability ties exactly to the statement control total captured in stage 1, and
3. every audited transaction is represented.

Approved spend posts to the expense GL. Flagged, rejected and manual-review items post to an employee receivable until
resolved, so the card liability always reconciles to the bank.

## Testing
22 offline tests cover money and date parsing, quarantine behaviour, missing-file handling, provider failover,
credential disabling, all-providers-down degradation, JSON repair and retry, and a full end-to-end run that asserts
balance, tie-out, write-once behaviour and tamper detection. A `--mock-llm` mode runs the whole pipeline with no keys.

## Tech
Python 3.10+, Pydantic v2, OpenAI-compatible SDK client (Gemini / Groq / OpenRouter), `Decimal`, pytest.

## Honest limitations
Prototype scope: simulated justifications and receipts (stage 2 stands in for an expense-portal/OCR feed), sequential
LLM calls, and free-tier models whose verdict quality varies. Verified live against Groq (`openai/gpt-oss-120b`) and Gemini
(`gemini-3.8-flash`): on a 26-transaction run, transient rate limits and a Gemini 503 sent 1 item to `MANUAL_REVIEW`
while the run still completed and verified. Free tiers may use prompts for model improvement, so use only
non-sensitive data.
