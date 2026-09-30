# P-Card & Expense Reconciliation Pipeline

Local prototype that takes a raw purchasing-card bank statement to an ERP-ready journal entry, using a **free-tier LLM**
(Gemini, Groq or OpenRouter) for the qualitative audit. For an architecture write-up see `PORTFOLIO_README.md`.

**Hybrid pattern:** deterministic Python owns everything computable (validation, routing, limits, receipt matching,
duplicate/split detection, GL mapping, debits = credits). The LLM does exactly one thing: judge whether an employee's
business justification satisfies the *written* spending policy.

```
transactions.csv ─► Stage 1 ingest/route ─► Stage 2 justifications ─► Stage 3 audit ─► Stage 4 ERP journal
 employees.json     01_raw_statement.json   02_user_justified.json   03_compliance_    04_erp_journal_
                                                                      audited.json      entry.csv
                    (Pydantic, no LLM)      (seeded simulation)      (rules + LLM)     (Decimal, no LLM)
```

## Setup

Requires Python 3.10+.

```bash
cd p-card-pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # add: pip install -r requirements-dev.txt  to run the tests
```

### Get a free API key (any one is enough; more than one gives automatic failover)

| Provider | Get a key | Env var | Default model (override with) |
|---|---|---|---|
| Google Gemini (AI Studio) | https://aistudio.google.com/apikey | `GEMINI_API_KEY` | `gemini-3.8-flash` (`GEMINI_MODEL`) |
| Groq | https://console.groq.com/keys | `GROQ_API_KEY` | `openai/gpt-oss-120b` (`GROQ_MODEL`) |
| OpenRouter | https://openrouter.ai/keys | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct:free` (`OPENROUTER_MODEL`) |

```bash
cp .env.example .env         # then edit .env and paste your key after GROQ_API_KEY= (or GEMINI_API_KEY=)
# Alternatively: export GROQ_API_KEY="..." in your shell. Real env vars override .env.
```

Free-tier model names and quotas change. If a run logs `model '...' not found`, set the matching `*_MODEL` variable to a
currently available model. See `.env.example` for all optional settings.

## Run

```bash
python run_pipeline.py --mock-llm       # offline heuristic auditor, no key needed
python run_pipeline.py                  # live audit through the configured provider(s)
python run_pipeline.py --providers groq,gemini   # override priority order
python run_pipeline.py --verify RUN_ID  # re-check every artifact hash in a finished run
pytest -q                               # unit + offline routing + end-to-end tests
```

Other options: `--data-dir`, `--output-dir`, `--run-id R --start-stage N` (resume a run from stage N), `--log-level DEBUG`.
Exit codes: `0` ok, `1` pipeline/integrity failure, `3` no LLM API key configured.

Each run writes to `output/<run_id>/`: the four artifacts, `04_erp_journal_entry.csv.manifest.json` and `pipeline.log`.

## The app (live workflow)

```bash
./run_local.sh        # http://localhost:8600  (Streamlit's default 8501 is often taken)
```

Two pages:

**Live workflow** (real data path)
1. *Statement:* create a batch from the sample statement or your own CSV. The exact inputs are snapshotted into the batch
   folder and Stage 1 validates and routes every row. Rows that fail validation are listed with reasons.
2. *Justifications:* pick a cardholder, pick a transaction, and enter the business purpose, attendees and receipt details
   (typed from the receipt; there is no receipt upload or OCR). Each save is an entry in an append-only, hash-chained log
   with who and when. Saving again adds a new version; the old one stays in the history.
3. *Audit and results:* choose the auditor and lock submissions. Transactions with no justification are flagged
   `MISSING_JUSTIFICATION` and skip the LLM. Nothing is invented. Refunds need no justification.

**Results:** overview, transaction detail, follow one transaction through all four stages, quarantined rows, the ERP
journal, and the audit trail. Batches from the live workflow and simulated demos both appear here.

Identity: there is no login. "Choose who you are" is a demo convenience, so anyone who can open the page can act as any
cardholder. Set `APP_PASSCODE` (env var or Streamlit secret) to require a shared passcode before the app opens.

### Running with Docker

```bash
docker compose up --build          # http://localhost:8600
# or, without compose:
docker build -t pcard-reconciler .
docker run --rm -p 8600:8501 --env-file .env -v pcard-output:/app/output pcard-reconciler
```

The image is Python 3.12 on a slim base (about 830 MB, mostly Streamlit's dependencies), runs as an unprivileged user and has a
health check. API keys and `APP_PASSCODE` are never baked in: pass them with `--env-file .env` or `-e`. Batches and audit artifacts are
written to `/app/output`, which is a named volume, so they survive restarts (unlike Streamlit Community Cloud, where files are lost).
That makes a container host with a volume, such as Render, Fly.io or Cloud Run with a bucket, the option to choose if you need entries to persist.
The full test suite also passes on Python 3.12 and 3.13.

### Deploying to Streamlit Community Cloud

Community Cloud deploys straight from a GitHub repo (it does not run Docker) and has no persistent disk.

1. Push the `p-card-pipeline` folder to a GitHub repo as its root. `.gitignore` already excludes `.env`, `output/` and secrets.
2. On share.streamlit.io create an app: main file `app.py`. `requirements.txt` is picked up automatically.
3. In *Settings > Secrets* paste the values from `.streamlit/secrets.toml.example`: at least one LLM key and an `APP_PASSCODE`.
4. In *Settings > Sharing* restrict viewers if your plan allows it. Apps are public by default.

Limits to know before you rely on it:
- **Entries can be lost.** Files written by the app disappear when the app restarts, sleeps or is redeployed.
  Use *Download backup* after each session and *Restore a backup* to continue later (the restore verifies the hash chain).
- **Use sample data only** on a shared host. Free-tier LLM providers may use prompts for training. The LLM sees the
  justification text, merchant, amount and department, not names, emails or card numbers.
- Live audits are throttled to stay inside free-tier limits, so a large batch can take several minutes.
- Not yet tested on Community Cloud itself; the local app and the tests are verified.

## Review export (Excel / CSV)

```bash
python export_review.py            # latest run  ->  exports/<run_id>/pcard_review_<run_id>.xlsx + csv/
python export_review.py RUN_ID
```

The workbook has a Read-me sheet, Transactions (most severe first, with rule findings and LLM rationale), Findings,
Quarantined, ERP Journal and Audit trail. It is also downloadable from the dashboard sidebar. Exports are derived
copies; the immutable artifacts in `output/` stay the source of truth.

## Sample statements

The Live workflow offers two built-in statements, plus your own CSV.
- **Realistic sample** (`data/sample_realistic.csv`, justifications in `data/sample_justifications_realistic.json`): 50 valid transactions
  across five cardholders and a terminated one. With the mock auditor, 45 are approved (90%). The five exceptions are planted on purpose:
  a casino charge and a terminated employee's card (hard rules), a luxury gift and a "team bonding" bar tab (judged by the policy audit),
  and an airfare with no receipt (receipt rule). Six extra broken rows show validation setting rows aside. The 90% is a property of this
  invented data, not a measured accuracy. With a live LLM the split can differ, because a strict model may flag things the mock does not.
- **Stress-test sample** (`data/transactions.csv`): 26 transactions with about a dozen problems of every kind (duplicates, split purchases,
  over-limit, vague justifications, home-office items, missing receipts). Use it to see how many different things the checks catch.

A full live audit of the realistic sample makes about 47 model calls (roughly 90,000 tokens), which takes around 12 minutes at the free-tier
pace and uses close to half of Groq's daily token allowance for the default model. The mock auditor is instant.

## Statement file format

Stage 1 is deliberately strict: it rejects or sets aside anything it would otherwise have to guess.
- **Columns:** `txn_id, post_date, card_last4, merchant_name, mcc, amount` are required (`currency`, `statement_memo` optional).
  Header case and stray spaces do not matter. The delimiter may be a comma, semicolon or tab. A column may not appear twice.
- **Amounts:** US style only. `1,234.50`, `1234.5`, `$1,234.50`, `-5.00`, `(5.00)` are fine. `1,23`, `12 34`, `1e3`, `.5`, non-ASCII
  digits and anything over $1,000,000 are rejected. A European `1,23` is never read as $123.
- **Dates:** `YYYY-MM-DD`, `MM/DD/YYYY`, `DD-Mon-YYYY`, `YYYY/MM/DD`, between 2000 and 2099. Slash dates are read as **month first**;
  `31/12/2026` is rejected and `02/01/2026` means February 1.
- **Text:** tabs and line breaks become spaces; any other control character sets the row aside. `statement_memo` is capped at 500 characters.
- **Currency:** USD only.
- Set-aside rows are listed with reasons; if *every* row is invalid the error shows the first reasons.

## Quality assurance

`pytest -q` runs about 100 offline tests (about 89% line coverage). Beyond unit tests they cover: adversarial statement files, spreadsheet
formula injection in every export, hostile text through every raw-HTML block of the UI, oversized and malformed submissions, hostile backup
archives, tampering with every artifact and the submissions log, the passcode gate, damaged data (each page must show a message, not a
traceback), and an independent recomputation of the journal totals. Each functional defect found during QA has a regression test; most were checked to fail on the pre-fix code.

Known limits: the hash chain is *tamper-evident*, not tamper-proof. Someone who can rewrite every file and recompute every hash could forge a
run; anchoring the final hash outside the folder (or signing it) would close that. Real-browser rendering, accessibility and phone layouts
are not covered by automated tests.

## Layout

```
data/        transactions.csv  employees.json  spending_policy.md (sent to the LLM)
             policy_rules.json (hard thresholds, MCC→GL map, blocked MCCs)  justification_overrides.json
schemas/     models.py             every Pydantic model at every stage boundary
stages/      stage1_ingest.py  stage2_justify.py  stage3_audit.py  stage4_erp.py
             deterministic_checks.py (pure hard-rule functions)   llm_auditor.py (prompt, parsing, mock auditor)
utils/       llm_client.py (multi-provider routing)  artifacts.py (immutable hash-chained writes)
             loaders.py  logging_config.py  errors.py
app.py       Streamlit viewer
tests/       test_pipeline.py  test_llm_routing.py
```

## The audit stage

1. Deterministic checks run first: blocked MCC, inactive cardholder, single/monthly limits, missing receipt, receipt
   amount/vendor mismatch, duplicates, split purchases. Hard rejects skip the LLM.
2. Otherwise the LLM receives the full `spending_policy.md` (system prompt), the verified transaction facts, the
   deterministic findings, and the employee's justification wrapped in `<employee_submission>` tags (treated as untrusted
   data). It must return one JSON object:
   `{"verdict": "APPROVE|FLAG|REJECT", "policy_sections": [...], "rationale": "...", "confidence": 0.0-1.0, "red_flags": [...]}`
   `APPROVE` is the pass verdict; `FLAG` and `REJECT` do not pass. The artifact also stores a boolean `passed` and
   `served_by` (`provider:model`) for each transaction.
3. The response is extracted (tolerating code fences and prose), normalised (`pass`/`fail` synonyms accepted) and validated
   against `LLMAuditResult`. Invalid output is retried once, on a different provider first.
4. Final status = worst of deterministic and LLM results, so the LLM can escalate but never downgrade. An `APPROVE` below
   the configured confidence (0.7) becomes `FLAGGED`.

## Resilient routing (`utils/llm_client.py`)

- Providers with a configured key are tried in priority order (default `gemini, groq, openrouter`).
- Rate limits, timeouts, 5xx, empty or truncated replies → next provider. Three consecutive failures put a provider on a
  60 s cooldown; rejected credentials or an unknown model disable it for the run.
- Per-provider throttling keeps calls inside free-tier rate limits (override with `LLM_MIN_INTERVAL_SECONDS`).
- JSON mode is used when the endpoint supports it, and dropped automatically when it does not.
- If every provider fails for a transaction, that transaction becomes `MANUAL_REVIEW` and the run continues. The only
  fatal LLM condition is having no API key at all.

## Audit trail and other error handling

| Situation | Behaviour |
|---|---|
| Missing/unreadable input, missing CSV columns, invalid config | Fatal `PipelineError`, logged, exit 1 |
| Malformed CSV row | Quarantined in `01_raw_statement.json` with reasons; run continues |
| Edited or swapped artifact | `ArtifactIntegrityError`; `--verify` reports it |
| Re-running a stage into an existing run | Refused: artifacts are write-once and `chmod 444` |

Each JSON artifact stores the SHA-256 of its payload and of the upstream payload; the CSV has a hash manifest.

## Notes and limits

- Free tiers are rate-limited: a full live run with one provider takes a few minutes because of throttling.
- Free-model verdict quality varies. Treat this as a prototype, not a compliance control.
- The mock auditor is a keyword heuristic for offline demos and tests.
- Free-tier providers may use submitted prompts for model improvement. Use only mock or non-sensitive data with them.
- Transactions are audited sequentially. Money is `Decimal` throughout and serialised as strings.

## License

MIT. See `LICENSE`. The data in this repo is invented sample data.
