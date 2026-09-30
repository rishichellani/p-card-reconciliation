"""Live workflow: bank statement -> employee justifications -> audit -> results."""
from __future__ import annotations

import html
import logging
import os
from datetime import date

import pandas as pd
import streamlit as st

from schemas.models import PolicyRules, ReceiptMetadata, Stage1Payload, parse_money
from stages.stage1_ingest import REQUIRED_COLUMNS
from stages.stage2_justify import sample_for
from ui.theme import icon, money
from utils import submissions, workspace
from utils.artifacts import STAGE_FILES, read_json_artifact
from utils.errors import PipelineError
from utils.llm_client import load_providers, probe
from utils.loaders import load_json_model

log = logging.getLogger(__name__)
NEW = "New batch"
TEMPLATE = "txn_id,post_date,card_last4,merchant_name,mcc,amount,currency,statement_memo\n"

# A pending selection must be applied before the selectbox is created (Streamlit forbids setting it afterwards).
if "pending_pick" in st.session_state:
    st.session_state["batch_pick"] = st.session_state.pop("pending_pick")
STEPS = ["1. Statement", "2. Justifications", "3. Audit and results"]
if "pending_step" in st.session_state:
    st.session_state["wf_step"] = st.session_state.pop("pending_step")

st.title("Live workflow")
st.caption("Load a bank statement, let each cardholder add their justification, then run the audit.")

# --------------------------------------------------------------------------- sidebar: batches, backup, restore
with st.sidebar:
    st.header("Batches")
    batches = workspace.list_batches()
    def batch_label(b: dict) -> str:
        r = b["run_id"]  # run_YYYYMMDDTHHMMSSZ -> "MM-DD HH:MM"
        when = f"{r[8:10]}-{r[10:12]} {r[13:15]}:{r[15:17]} UTC"
        return f"{when} · {b['label']}" if b["label"] else when  # state is shown in the header: a state here goes stale

    labels = {b["run_id"]: batch_label(b) for b in batches}
    pick = st.selectbox("Batch", [NEW, *labels], key="batch_pick", format_func=lambda k: k if k == NEW else labels[k])

    st.divider()
    st.subheader("Backup and restore")
    st.caption("Hosted apps can lose files when they restart. Download a backup after each session and restore it later.")
    if pick != NEW:
        st.download_button("Download backup of this batch", workspace.backup_zip(workspace.OUTPUT / pick),
                           file_name=f"{pick}_backup.zip", mime="application/zip", icon=":material/download:", width="stretch")
    up = st.file_uploader("Restore a backup (.zip)", type="zip", key="restore_upload")
    if up is not None and st.button("Restore", width="stretch"):
        try:
            restored = workspace.restore_zip(up.getvalue())
            st.session_state["pending_pick"] = restored
            st.session_state["flash"] = f"Restored {restored}."
            st.rerun()
        except PipelineError as exc:
            st.error(str(exc))

if flash := st.session_state.pop("flash", None):
    st.success(flash)

# --------------------------------------------------------------------------- new batch
if pick == NEW:
    st.subheader("Start a batch")
    source = st.radio("Bank statement", ["Sample statement (32 rows, some deliberately messy)", "Upload my own CSV"])
    uploaded = None
    if source.startswith("Upload"):
        uploaded = st.file_uploader("Statement CSV", type="csv", help="Up to 5 MB")
        with st.expander("Required columns"):
            st.write(", ".join(f"`{c}`" for c in sorted(REQUIRED_COLUMNS)) + " (plus optional `currency`, `statement_memo`). "
                     "Definitions are in `docs/data_dictionary.xlsx`.")
            st.download_button("Download an empty template", TEMPLATE, file_name="statement_template.csv", mime="text/csv")
    label = st.text_input("Batch name (optional)", max_chars=80, placeholder="e.g. September statement")
    st.caption("Only the cardholders in `data/employees.json` can be matched. Rows that fail validation are set aside, "
               "not fixed silently.")
    ready = source.startswith("Sample") or uploaded is not None
    if st.button("Create batch and validate", type="primary", disabled=not ready, icon=":material/upload_file:"):
        try:
            data = (workspace.SAMPLE_DATA / "transactions.csv").read_bytes() if source.startswith("Sample") else uploaded.getvalue()
            with st.spinner("Validating the statement..."):
                ctx = workspace.create_batch(data, label=label)
            st.session_state["pending_pick"] = ctx.run_id
            st.session_state["flash"] = "Batch created. Statement validated and routed to cardholders."
            st.rerun()
        except PipelineError as exc:
            st.error(f"Could not create the batch: {exc}")
    st.stop()

# --------------------------------------------------------------------------- existing batch
try:
    ctx = workspace.load_ctx(pick)
    run_dir = ctx.run_dir
    info = workspace.batch_info(run_dir)
    _, s1, _ = read_json_artifact(ctx, 1, Stage1Payload)
    rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
    routed = {r.transaction.txn_id: r for r in s1.routed}
    entries = submissions.read_log(run_dir)
    latest = submissions.latest_by_txn(entries)
    closed = (run_dir / STAGE_FILES[2]).exists()
    complete = (run_dir / STAGE_FILES[4]).exists()
except (PipelineError, OSError, ValueError, KeyError) as exc:
    st.error(f"This batch cannot be opened: {exc}")
    st.caption("Its files may have been damaged or edited. Choose another batch in the sidebar, or restore a backup.")
    st.stop()
needs_just = [t for t, r in routed.items() if r.transaction.amount > 0]
missing = [t for t in needs_just if t not in latest]

st.markdown(
    f'<p class="pc-muted">Batch <span class="pc-mono">{html.escape(pick)}</span>'
    + (f" &middot; {html.escape(info['label'])}" if info["label"] else "")
    + f" &middot; {html.escape(info['state'])}</p>", unsafe_allow_html=True)

# Not st.tabs: tabs snap back to the first one on every rerun (after saving, loading samples, running the audit).
_first_time = {} if "wf_step" in st.session_state else {"default": STEPS[0]}
step = st.segmented_control("Workflow step", STEPS, key="wf_step", label_visibility="collapsed", **_first_time) or STEPS[0]

# --------------------------------------------------------------------------- 1. statement
if step == STEPS[0]:
    c = st.columns(3)
    c[0].metric("Transactions routed", len(routed))
    c[1].metric("Rows set aside", len(s1.quarantined))
    c[2].metric("Control total", money(s1.control_total_usd))
    st.markdown(f'<p><strong>Statement period:</strong> <span class="pc-num">{s1.statement_period_start}</span> to '
                f'<span class="pc-num">{s1.statement_period_end}</span></p>', unsafe_allow_html=True)
    rows = []
    for t, r in routed.items():
        e = latest.get(t)
        rows.append({"txn_id": t, "date": r.transaction.post_date.isoformat(), "cardholder": r.employee_name,
                     "merchant": r.transaction.merchant_name.title(), "amount": float(r.transaction.amount),
                     "justification": ("Refund, none needed" if r.transaction.amount <= 0 else
                                       (f"Submitted {e.submitted_at:%Y-%m-%d %H:%M}" + (" (sample)" if e.source != "employee" else "")) if e else "Needed")})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"txn_id": "Txn", "date": "Date", "cardholder": "Cardholder", "merchant": "Merchant",
                                "amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
                                "justification": "Justification"})
    if s1.quarantined:
        st.subheader("Rows set aside")
        st.caption("These failed validation and never reach the audit or the journal. Fix them in the source file and start a new batch.")
        st.dataframe(pd.DataFrame([{"row": q.row_number, "txn_id": q.raw.get("txn_id"), "merchant": q.raw.get("merchant_name"),
                                    "amount": q.raw.get("amount"), "why": " | ".join(q.reasons)} for q in s1.quarantined]),
                     hide_index=True, width="stretch",
                     column_config={"row": "CSV row", "txn_id": "Txn", "merchant": "Merchant", "amount": "Amount", "why": "Why"})

# --------------------------------------------------------------------------- 2. justifications
if step == STEPS[1]:
    done = len(needs_just) - len(missing)
    st.markdown(f'<p role="status"><strong>{done} of {len(needs_just)} justifications submitted.</strong> '
                f'{len(missing)} still needed.</p>', unsafe_allow_html=True)
    st.progress(done / len(needs_just) if needs_just else 1.0)

    if closed:
        st.info("Submissions are closed: the audit has been run for this batch. The history below is read-only.")
    else:
        with st.container(border=True):
            st.markdown("**Just want to see a result?**")
            st.caption("Fills every transaction that still needs a justification with sample text, including a few planted problem "
                       "cases. Anything a cardholder already entered is left alone. These entries are marked as sample data "
                       "everywhere they appear, and the results carry a sample-data banner.")
            if st.button("Load sample justifications", icon=":material/science:", disabled=not missing,
                         help="Nothing to load: every transaction already has one." if not missing else None):
                try:
                    samples = sample_for([routed[t] for t in missing], rules, ctx.seed, workspace.SAMPLE_DATA)
                    for t, (j, kind) in samples.items():
                        submissions.submit(run_dir, routed[t], j.business_purpose, j.attendees, j.receipt, source=kind)
                    st.session_state["flash"] = (f"Loaded {len(samples)} sample justifications (marked as sample data). "
                                                 "Now run the audit.")
                    st.session_state["pending_step"] = STEPS[2]
                    st.rerun()
                except PipelineError as exc:
                    st.error(str(exc))
        st.caption("Demo mode: choose who you are. There is no login, so anyone who can open this page can act as any cardholder.")
        people: dict[str, tuple[str, str]] = {}
        for r in routed.values():
            people[r.employee_id] = (r.employee_name, r.department)
        pending_by = {eid: sum(1 for t in missing if routed[t].employee_id == eid) for eid in people}
        who_key = f"who_{pick}"
        # After a save we ask for the next pending transaction; apply it before the widgets exist.
        if "advance_to" in st.session_state:
            adv_who, adv_txn = st.session_state.pop("advance_to")
            st.session_state[who_key] = adv_who
            st.session_state[f"txn_{pick}_{adv_who}"] = adv_txn
        # Keyed widgets keep their value when the labels change (the pending counts change on every save).
        who = st.selectbox("Cardholder", sorted(people, key=lambda e: people[e][0]), key=who_key,
                           format_func=lambda e: f"{people[e][0]} ({people[e][1]}), {pending_by[e]} needed")
        mine = sorted((t for t, r in routed.items() if r.employee_id == who),
                      key=lambda t: (routed[t].transaction.post_date, t))  # stable order: it must not reshuffle after a save
        if not mine:
            st.info("This cardholder has no transactions in the batch.")
        else:
            def fmt(t: str) -> str:
                x = routed[t].transaction
                state = "refund" if x.amount <= 0 else "submitted" if t in latest else "needed"
                return f"{t}  {x.merchant_name.title()}  {money(x.amount)}  ({state})"
            first_needed = next((i for i, t in enumerate(mine) if t in missing), 0)
            txn_key = f"txn_{pick}_{who}"
            # Only set a default the first time; afterwards the keyed session value is the source of truth.
            default = {} if txn_key in st.session_state else {"index": first_needed}
            tid = st.selectbox("Transaction", mine, key=txn_key, format_func=fmt, **default)
            x = routed[tid].transaction
            prev = latest.get(tid)
            st.markdown(f'<div class="pc-panel"><strong>{html.escape(x.merchant_name)}</strong> &middot; '
                        f'<span class="pc-num">{money(x.amount)}</span> &middot; posted {x.post_date} &middot; MCC '
                        f'<span class="pc-mono">{x.mcc}</span></div>', unsafe_allow_html=True)
            if x.amount <= 0:
                st.info("This is a refund or credit. No justification is needed.")
            else:
                if prev:
                    st.caption(f"Already submitted {prev.submitted_at:%Y-%m-%d %H:%M}. Saving again adds a new version; "
                               "the earlier one stays in the history.")
                has_receipt = st.checkbox("I have a receipt for this charge", value=prev.receipt.present if prev else False,
                                          key=f"rc_{tid}_{len(entries)}",
                                          help=f"A receipt is required for charges over {money(rules.receipt_required_over)}.")
                with st.form(f"form_{tid}_{len(entries)}"):
                    purpose = st.text_area("Business purpose *", value=prev.business_purpose if prev else "", max_chars=1000,
                                           help="Who, what and why. Specific answers pass review; 'supplies' or 'team stuff' will be flagged.")
                    attendees = st.text_area("Attendees (one per line)", value="\n".join(prev.attendees) if prev else "",
                                             help="For meals and entertainment: name and company of everyone present.")
                    vendor = total_text = None
                    rdate = x.post_date
                    if has_receipt:
                        st.markdown("**Receipt details** (type what the receipt shows)")
                        r1, r2, r3 = st.columns(3)
                        vendor = r1.text_input("Vendor name *", value=(prev.receipt.vendor_name or "") if prev else "")
                        total_text = r2.text_input("Total *", value=f"{prev.receipt.total}" if prev and prev.receipt.total is not None else "",
                                                   placeholder="e.g. 1,211.40")
                        rdate = r3.date_input("Receipt date", value=(prev.receipt.receipt_date if prev and prev.receipt.receipt_date else x.post_date))
                    save = st.form_submit_button("Save justification", type="primary", icon=":material/check:")
                if save:
                    try:
                        receipt = ReceiptMetadata(present=False)
                        if has_receipt:
                            try:
                                total = parse_money(total_text or "")
                            except ValueError:
                                raise PipelineError("Receipt total must be a number such as 1,211.40.") from None
                            receipt = ReceiptMetadata(present=True, vendor_name=vendor, total=total,
                                                      receipt_date=rdate if isinstance(rdate, date) else None)
                        entry = submissions.submit(run_dir, routed[tid], purpose, attendees.splitlines(), receipt)
                        remaining = [t for t in mine if t in missing and t != tid]
                        after = [t for t in remaining if mine.index(t) > mine.index(tid)]
                        nxt = (after or remaining or [None])[0]  # next one down the list, wrapping to the first left
                        saved = f"Saved {tid} for {entry.employee_name} at {entry.submitted_at:%H:%M}."
                        if nxt:
                            st.session_state["advance_to"] = (who, nxt)
                            st.session_state["flash"] = f"{saved} Now showing {nxt}."
                        else:
                            st.session_state["flash"] = f"{saved} {entry.employee_name} has no more justifications to add."
                        st.rerun()
                    except PipelineError as exc:
                        st.error(str(exc))

    with st.expander(f"Submission history ({len(entries)} entries, append-only)"):
        if entries:
            st.dataframe(pd.DataFrame([{"seq": e.seq, "when": e.submitted_at.isoformat(sep=" "), "who": e.employee_name,
                                        "txn": e.txn_id, "source": e.source.replace("_", " "), "purpose": e.business_purpose[:90], "receipt": "yes" if e.receipt.present else "no",
                                        "hash": e.entry_hash[:12] + "..."} for e in entries]),
                         hide_index=True, width="stretch")
        else:
            st.caption("Nothing submitted yet.")

# --------------------------------------------------------------------------- 3. audit
if step == STEPS[2]:
    if complete:
        st.success("The audit is complete and every artifact verified.")
        st.session_state["results_run"] = pick  # the Results page opens this batch
        # A page link behaves exactly like the sidebar entry; a scripted switch_page did not navigate on the hosted app.
        st.page_link("ui/results.py", label="Open results", icon=":material/fact_check:")
    else:
        if closed:
            st.warning("A previous audit attempt did not finish. Submissions are already locked; run it again to complete it.")
        if missing:
            by = {}
            for t in missing:
                by.setdefault(routed[t].employee_name, []).append(t)
            st.markdown(f'<div class="pc-todo">{icon("info")} <strong>{len(missing)} transactions have no justification.</strong> '
                        "They will be flagged <span class='pc-mono'>MISSING_JUSTIFICATION</span> for follow-up. Nothing is invented. "
                        + html.escape("; ".join(f"{n}: {len(ts)}" for n, ts in sorted(by.items()))) + "</div>", unsafe_allow_html=True)
        else:
            st.success("Every transaction that needs a justification has one.")

        mode = st.radio("Auditor", ["Live LLM (free-tier providers)", "Mock (offline heuristic, no key needed)"])
        live = mode.startswith("Live")
        providers = None
        can_run = True
        if live:
            providers = st.text_input("Provider order", os.environ.get("LLM_PROVIDERS", "groq,gemini"),
                                      help="Comma-separated. Each needs its API key configured.")
            try:
                found = load_providers(order=providers)
                n_calls = len(needs_just) - len(missing)
                minutes = n_calls * found[0].min_interval / 60
                st.caption(f"Live audits are paced to stay inside free-tier limits: up to about {minutes:.0f} min for "
                           f"{n_calls} submitted transactions (hard-rule rejections skip the model).")
                st.caption("Will use: " + ", ".join(f"{p.name} ({p.model})" for p in found) + ". "
                           "The justification text, merchant, amount and department are sent to these providers. "
                           "Names, emails and card numbers are not. Free tiers may use prompts for training.")
                if st.button("Test connection", icon=":material/network_check:",
                             help="Sends one tiny request to each provider so you can see which ones work before locking anything."):
                    with st.spinner("Contacting the providers..."):
                        results = probe(found)
                    for name, model, ok, detail in results:
                        (st.success if ok else st.error)(f"{name} ({model}): {detail}")
                    if not any(ok for _, _, ok, _ in results):
                        st.warning("No provider responded. Fix the keys or model names first, or the audit will stop.")
            except PipelineError as exc:
                can_run = False
                st.error(f"{exc}. Add a key in `.env` or the app's Secrets, or choose Mock.")
        confirm = st.checkbox("I understand this locks submissions for the batch")
        if st.button("Lock submissions and run audit", type="primary", disabled=not (confirm and can_run),
                     icon=":material/play_arrow:"):
            bar = st.progress(0.0, text="Locking submissions...")

            def progress(n: int, total: int, tid: str) -> None:
                bar.progress(n / total, text=f"Auditing {n} of {total}: {tid}")

            try:
                with st.spinner("Running the audit. Live mode is throttled to stay inside free-tier limits."):
                    workspace.run_audit(ctx, mock_llm=not live, providers=providers, progress=progress)
                st.session_state["flash"] = "Audit complete. Open the results below."
                st.session_state["pending_step"] = STEPS[2]
                st.rerun()
            except PipelineError as exc:
                st.error(f"The audit stopped: {exc}")
            except Exception:  # noqa: BLE001 - surface anything unexpected without losing the page
                log.exception("Audit failed")
                st.error("The audit failed unexpectedly. Details are in the batch's pipeline.log.")
