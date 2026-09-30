"""UI tests through Streamlit's AppTest: the flows a person actually clicks, plus failure and hostile-input cases."""
import re
import time
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from schemas.models import ReceiptMetadata, Stage1Payload
from utils import submissions, workspace
from utils.artifacts import read_json_artifact

APP = str(Path(__file__).resolve().parent.parent / "app.py")
HDR = "txn_id,post_date,card_last4,merchant_name,mcc,amount,currency,statement_memo\n"
MOCK = "Mock (offline heuristic, no key needed)"


@pytest.fixture(autouse=True)
def isolated_output(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "OUTPUT", tmp_path / "output")
    monkeypatch.delenv("APP_PASSCODE", raising=False)
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setenv(k, "")  # blank values are ignored by load_dotenv's setdefault only if unset; empty means "no key"


def app(**state):
    at = AppTest.from_file(APP, default_timeout=120)
    for k, v in state.items():
        at.session_state[k] = v
    return at


def btn(at, label):
    return next((b for b in at.button if b.label == label), None)


def widget(at, kind, label):
    return next(w for w in getattr(at, kind) if w.label.startswith(label))


def audited_batch(label="ui", extra_csv="", sample=True):
    time.sleep(1.05)
    csv = (workspace.SAMPLE_DATA / "transactions.csv").read_bytes() + extra_csv.encode()
    ctx = workspace.create_batch(csv, label=label)
    if sample:
        from schemas.models import PolicyRules
        from stages.stage2_justify import sample_for
        from utils.loaders import load_json_model
        routed = {r.transaction.txn_id: r for r in read_json_artifact(ctx, 1, Stage1Payload)[1].routed}
        rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
        for t, (j, kind) in sample_for(list(routed.values()), rules, ctx.seed, workspace.SAMPLE_DATA).items():
            submissions.submit(ctx.run_dir, routed[t], j.business_purpose, j.attendees, j.receipt, source=kind)
        workspace.run_audit(ctx, mock_llm=True, providers=None)
    return ctx


def test_empty_states_render_without_errors():
    at = app().run()
    assert not at.exception and btn(at, "Create batch and validate")
    at.switch_page("ui/results.py").run()
    assert not at.exception and any("No completed runs" in i.value for i in at.info)


def test_full_flow_create_load_samples_audit_and_open_results():
    at = app().run()
    btn(at, "Create batch and validate").click().run()
    assert not at.exception and len(workspace.list_batches()) == 1
    at.session_state["wf_step"] = "2. Justifications"
    at.run()
    btn(at, "Load sample justifications").click().run()
    assert at.session_state["wf_step"] == "3. Audit and results"          # moves forward on its own
    assert btn(at, "Load sample justifications") is None                  # and is on the audit step now
    widget(at, "radio", "Auditor").set_value(MOCK).run()
    widget(at, "checkbox", "I understand").check().run()
    btn(at, "Lock submissions and run audit").click().run()
    assert not at.exception and at.session_state["wf_step"] == "3. Audit and results"   # does not fall back to step 1
    assert any("audit is complete" in s.value for s in at.success)
    at.switch_page("ui/results.py").run()
    assert not at.exception
    assert widget(at, "selectbox", "Run").value == at.session_state["results_run"]
    banner = " ".join(w.value for w in at.warning)
    assert "SAMPLE DATA" in banner and "mock auditor" in banner and "not from an LLM" in banner   # never claim an LLM ran


def test_saving_stays_on_the_step_and_advances_to_the_next_transaction():
    at = app().run()
    btn(at, "Create batch and validate").click().run()
    at.session_state["wf_step"] = "2. Justifications"
    at.run()
    widget(at, "selectbox", "Cardholder").select("E1002").run()
    first = widget(at, "selectbox", "Transaction").value.split("  ")[0]
    widget(at, "text_area", "Business purpose").set_value("Airfare for the Northwind onsite visit in Denver")
    btn(at, "Save justification").click().run()
    assert at.session_state["wf_step"] == "2. Justifications"
    assert widget(at, "selectbox", "Cardholder").value == "E1002"          # used to jump back to the first cardholder
    assert widget(at, "selectbox", "Transaction").value.split("  ")[0] != first


def test_form_errors_are_shown_next_to_the_form_not_as_a_crash():
    at = app().run()
    btn(at, "Create batch and validate").click().run()
    at.session_state["wf_step"] = "2. Justifications"
    at.run()
    widget(at, "selectbox", "Cardholder").select("E1002").run()
    widget(at, "text_area", "Business purpose").set_value("Hotel stay for the customer visit")
    widget(at, "text_area", "Attendees").set_value("\n".join(f"P{i}" for i in range(25)))
    btn(at, "Save justification").click().run()
    assert not at.exception and any("At most 20 attendees" in e.value for e in at.error)
    widget(at, "text_area", "Attendees").set_value("")
    widget(at, "checkbox", "I have a receipt").check().run()
    widget(at, "text_input", "Vendor name").set_value("Marriott")
    widget(at, "text_input", "Total").set_value("1,23")                   # European-style number: refused, not guessed
    btn(at, "Save justification").click().run()
    assert any("must be a number" in e.value for e in at.error)


def test_hostile_text_is_escaped_in_every_raw_html_block():
    payloads = ['<img src=x onerror=alert(1)>', '<script>alert("x")</script>', '"><svg onload=alert(2)>']
    rows = "".join(f'H-{i},2026-09-0{i},4821,"{p.replace(chr(34), chr(34) * 2)}",5734,{90 + i}.00,USD,m\n' for i, p in enumerate(payloads, 1))
    time.sleep(1.05)
    ctx = workspace.create_batch((HDR + rows).encode(), label='<script>alert("label")</script>')
    for t, r in {r.transaction.txn_id: r for r in read_json_artifact(ctx, 1, Stage1Payload)[1].routed}.items():
        submissions.submit(ctx.run_dir, r, f"Purpose <img src=x onerror=alert(9)> {t}</div><script>alert(3)</script>", ["<script>x</script>"],
                           ReceiptMetadata(present=True, vendor_name="<img src=x onerror=1>", total=Decimal("95.00")))
    workspace.run_audit(ctx, mock_llm=True, providers=None)
    danger = re.compile(r"<(script|img|iframe)\b|<svg(?![^>]*class=\"pc-icon\")", re.I)

    def raw_blocks(at):
        return [m.value for m in at.markdown if getattr(m, "allow_html", False)]

    at = app(batch_pick=ctx.run_id)
    for step in ("1. Statement", "2. Justifications", "3. Audit and results"):
        at.session_state["wf_step"] = step
        at.run()
        assert not at.exception and not [b for b in raw_blocks(at) if danger.search(b)], step
    at.switch_page("ui/results.py")
    at.session_state["results_run"] = ctx.run_id
    at.run()
    assert not at.exception and not [b for b in raw_blocks(at) if danger.search(b)]
    assert any("&lt;script&gt;" in b for b in raw_blocks(at))            # the text is shown, harmlessly


def test_passcode_gate_blocks_every_page_until_the_right_passcode(monkeypatch):
    monkeypatch.setenv("APP_PASSCODE", "s3cret-pass")
    at = app().run()
    assert at.text_input and not any(t.value == "Live workflow" for t in at.title)
    at.switch_page("ui/results.py").run()                                  # deep link is gated too
    assert at.text_input
    for attempt, ok in (("wrong", False), ("", False), ("s3cret-pass", True)):
        at.text_input[0].set_value(attempt)
        next(b for b in at.button if b.label == "Enter").click().run()
        authed = at.session_state["authed"] if "authed" in at.session_state else False
        assert bool(authed) is ok, attempt
    assert at.session_state["authed"] is True


def _rewrite(path, fn):
    path.chmod(0o644)
    path.write_text(fn(path.read_text()))


def test_damaged_files_show_a_message_and_never_crash_a_page():
    good = audited_batch("good")
    corrupt = workspace.create_batch((workspace.SAMPLE_DATA / "transactions.csv").read_bytes(), label="corrupt")
    _rewrite(corrupt.run_dir / "batch.json", lambda t: "{not json")
    time.sleep(1.05)
    tampered = workspace.create_batch((workspace.SAMPLE_DATA / "transactions.csv").read_bytes(), label="tampered")
    _rewrite(tampered.run_dir / "01_raw_statement.json", lambda t: t.replace('"1420.55"', '"1.55"'))
    garbage = audited_batch("garbage")
    _rewrite(garbage.run_dir / "03_compliance_audited.json", lambda t: "garbage")

    at = app().run()                                                       # a corrupted batch.json must not hide the rest
    assert not at.exception
    assert {x["run_id"] for x in workspace.list_batches()} == {good.run_id, tampered.run_id, garbage.run_id}
    assert len(widget(at, "selectbox", "Batch").options) == 4              # "New batch" plus the three readable ones

    at = app(batch_pick=tampered.run_id).run()
    assert not at.exception and any("cannot be opened" in e.value for e in at.error)

    at = app(results_run=garbage.run_id)
    at.switch_page("ui/results.py")
    at.run()
    assert not at.exception and any("cannot be read" in e.value for e in at.error)


def test_results_controls_name_the_auditor_honestly_and_explain_gross_vs_net():
    ctx = audited_batch("controls")
    at = app(results_run=ctx.run_id)
    at.switch_page("ui/results.py")
    at.run()
    text = " ".join(m.value for m in at.markdown)
    plain = re.sub(r"<[^>]+>", "", text)
    assert "mock auditor (offline keyword heuristic, not an LLM)" in plain
    assert "gross: includes $250.00 of refunds on both sides" in plain
    assert "Net card liability $24,833.60 matches the statement total $24,833.60" in plain


def test_screens_account_for_every_row_and_explain_the_refund():
    at = app().run()
    assert any("50 valid transactions" in c.value and "49 need a justification" in c.value for c in at.caption)   # before creating
    btn(at, "Create batch and validate").click().run()
    assert any("56 rows read: 50 routed to cardholders, 6 set aside as invalid" in s.value for s in at.success)
    stmt = " ".join(c.value for c in at.caption)
    assert "56 rows in the file: 50 valid transactions routed" in stmt and "6 set aside as invalid" in stmt
    assert "1 of the 50 is a refund, so 49 need a justification" in stmt
    at.session_state["wf_step"] = "2. Justifications"
    at.run()
    status = next(m.value for m in at.markdown if "justifications submitted" in m.value)
    assert "0 of 49 justifications submitted" in status and "50 transactions in the batch; 1 refund needs none" in status


def test_control_checks_say_pass_not_approved():
    ctx = audited_batch("checks")
    at = app(results_run=ctx.run_id)
    at.switch_page("ui/results.py")
    at.run()
    controls = next(m.value for m in at.markdown if "<h3>Controls</h3>" in m.value)
    plain = re.sub(r"<[^>]+>", " ", controls)
    assert plain.count("Pass") == 3 and "Approved" not in plain and "Rejected" not in plain   # hash chain, balance, tie-out
