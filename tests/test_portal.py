"""Interactive (portal) workflow: batches, employee submissions, closed audit, backup/restore. Offline (mock auditor)."""
import io
import json
import zipfile
from decimal import Decimal

import pytest

from schemas.models import ReceiptMetadata, Stage1Payload, Stage3Payload
from utils import submissions, workspace
from utils.artifacts import read_json_artifact, verify_run
from utils.errors import ArtifactIntegrityError, PipelineError
from utils.export import build_trace, write_workbook

CSV = (workspace.SAMPLE_DATA / "transactions.csv").read_bytes()
NO_RECEIPT = ReceiptMetadata(present=False)


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "OUTPUT", tmp_path / "output")
    ctx = workspace.create_batch(CSV, label="test")
    routed = {r.transaction.txn_id: r for r in read_json_artifact(ctx, 1, Stage1Payload)[1].routed}
    return ctx, routed


def test_create_batch_snapshots_inputs_and_runs_stage1(batch):
    ctx, routed = batch
    assert len(routed) == 26
    assert {p.name for p in (ctx.run_dir / "inputs").iterdir()} == {*workspace.INPUT_FILES, "transactions.csv"}
    assert workspace.batch_info(ctx.run_dir)["state"] == "collecting justifications"


def test_bad_upload_leaves_no_batch_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "OUTPUT", tmp_path / "output")
    with pytest.raises(PipelineError):
        workspace.create_batch(b"txn_id,amount\nT1,5\n")
    assert workspace.list_batches() == []


def test_submission_validation(batch):
    ctx, routed = batch
    r = routed["T-1004"]
    with pytest.raises(PipelineError, match="purpose"):
        submissions.submit(ctx.run_dir, r, "   ", [], NO_RECEIPT)
    with pytest.raises(PipelineError, match="vendor name and a total"):
        submissions.submit(ctx.run_dir, r, "Hotel for client visit", [], ReceiptMetadata(present=True))


def test_log_is_chained_latest_wins_and_tamper_evident(batch):
    ctx, routed = batch
    submissions.submit(ctx.run_dir, routed["T-1004"], "First version of the purpose", [], NO_RECEIPT)
    submissions.submit(ctx.run_dir, routed["T-1004"], "Corrected purpose text", ["A", " ", "B"], NO_RECEIPT)
    entries = submissions.read_log(ctx.run_dir)
    assert [e.seq for e in entries] == [1, 2]
    assert submissions.latest_by_txn(entries)["T-1004"].business_purpose == "Corrected purpose text"
    assert submissions.latest_by_txn(entries)["T-1004"].attendees == ["A", "B"]
    path = ctx.run_dir / submissions.LOG_NAME
    path.write_text(path.read_text().replace("First version", "Edited after the fact"))
    with pytest.raises(ArtifactIntegrityError):
        submissions.read_log(ctx.run_dir)


def test_full_flow_missing_justifications_are_flagged_not_invented(batch):
    ctx, routed = batch
    good = ReceiptMetadata(present=True, vendor_name="Marriott Denver", total=Decimal("1211.40"), receipt_date="2026-09-03")
    submissions.submit(ctx.run_dir, routed["T-1004"], "5 nights lodging for the Denver customer onsite with Northwind", [], good)
    workspace.run_audit(ctx, mock_llm=True, providers=None)

    s3 = read_json_artifact(ctx, 3, Stage3Payload)[1]
    by_id = {a.txn_id: a for a in s3.items}
    done, missing = by_id["T-1004"], by_id["T-1010"]
    assert done.justified.justification.source == "portal" and done.llm_result is not None
    assert not done.justified.justification.is_missing()
    assert missing.justified.justification.is_missing()
    assert "MISSING_JUSTIFICATION" in {f.code for f in missing.findings}
    assert missing.llm_result is None and missing.final_status.value == "FLAGGED"
    refund = by_id["T-1023"]  # refunds need no justification
    assert "MISSING_JUSTIFICATION" not in {f.code for f in refund.findings}

    assert verify_run(ctx.run_dir) == []
    assert (ctx.run_dir / "04_erp_journal_entry.csv").exists()
    with pytest.raises(PipelineError, match="closed"):
        submissions.submit(ctx.run_dir, routed["T-1004"], "Too late to change this", [], NO_RECEIPT)

    assert "No justification was submitted" in " ".join(s["detail"] for s in build_trace(ctx.run_dir)["T-1010"])
    assert write_workbook(ctx.run_dir, ctx.run_dir.parent / "review.xlsx").exists()


def test_log_edit_after_audit_is_detected(batch):
    ctx, routed = batch
    submissions.submit(ctx.run_dir, routed["T-1004"], "Hotel for the Denver customer onsite", [], NO_RECEIPT)
    workspace.run_audit(ctx, mock_llm=True, providers=None)
    assert verify_run(ctx.run_dir) == []
    path = ctx.run_dir / submissions.LOG_NAME
    with path.open("a") as fh:
        fh.write("\n")  # even a byte-level change after the audit snapshot is flagged
    assert any("changed after the audit" in p for p in verify_run(ctx.run_dir))


def test_backup_restore_roundtrip_partial_and_complete(batch, tmp_path, monkeypatch):
    ctx, routed = batch
    submissions.submit(ctx.run_dir, routed["T-1004"], "Hotel for the Denver customer onsite", [], NO_RECEIPT)
    partial = workspace.backup_zip(ctx.run_dir)
    workspace.run_audit(ctx, mock_llm=True, providers=None)
    complete = workspace.backup_zip(ctx.run_dir)

    monkeypatch.setattr(workspace, "OUTPUT", tmp_path / "fresh")
    assert workspace.restore_zip(complete) == ctx.run_id
    assert verify_run(tmp_path / "fresh" / ctx.run_id) == []
    with pytest.raises(PipelineError, match="already exists"):
        workspace.restore_zip(complete)
    monkeypatch.setattr(workspace, "OUTPUT", tmp_path / "fresh2")
    workspace.restore_zip(partial)
    assert workspace.batch_info(tmp_path / "fresh2" / ctx.run_id)["state"] == "collecting justifications"


def test_restore_rejects_tampered_and_unsafe_zips(batch, tmp_path, monkeypatch):
    ctx, routed = batch
    submissions.submit(ctx.run_dir, routed["T-1004"], "Hotel for the Denver customer onsite", [], NO_RECEIPT)
    good = workspace.backup_zip(ctx.run_dir)
    monkeypatch.setattr(workspace, "OUTPUT", tmp_path / "fresh")

    tampered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(good)) as zin, zipfile.ZipFile(tampered, "w") as zout:
        for i in zin.infolist():
            data = zin.read(i.filename)
            if i.filename.endswith(submissions.LOG_NAME):
                data = data.replace(b"Denver", b"Boston")
            zout.writestr(i.filename, data)
    with pytest.raises(PipelineError, match="integrity"):
        workspace.restore_zip(tampered.getvalue())

    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("run_x/../../escape.txt", "x")
    with pytest.raises(PipelineError):
        workspace.restore_zip(evil.getvalue())
    with pytest.raises(PipelineError):
        workspace.restore_zip(b"not a zip")


def test_sample_button_fills_only_missing_and_is_labelled(batch):
    from stages.stage2_justify import sample_for
    from schemas.models import PolicyRules
    from utils.export import provenance_note
    from utils.loaders import load_json_model

    ctx, routed = batch
    submissions.submit(ctx.run_dir, routed["T-1004"], "My own real hotel justification for the Denver visit", [], NO_RECEIPT)
    rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
    latest = submissions.latest_by_txn(submissions.read_log(ctx.run_dir))
    todo = [routed[t] for t, r in routed.items() if r.transaction.amount > 0 and t not in latest]
    samples = sample_for(todo, rules, ctx.seed, workspace.SAMPLE_DATA)
    assert "T-1004" not in samples and len(samples) == 24
    for t, (j, kind) in samples.items():
        submissions.submit(ctx.run_dir, routed[t], j.business_purpose, j.attendees, j.receipt, source=kind)
    assert samples["T-1014"][1] == "sample_scenario" and samples["T-1002" if "T-1002" in samples else "T-1001"][1] == "sample_template"

    workspace.run_audit(ctx, mock_llm=True, providers=None)
    s3 = read_json_artifact(ctx, 3, Stage3Payload)[1]
    by_id = {a.txn_id: a for a in s3.items}
    assert not any(f.code == "MISSING_JUSTIFICATION" for a in s3.items for f in a.findings)
    assert by_id["T-1004"].justified.justification.source == "portal"          # the real entry stays real
    assert by_id["T-1014"].justified.justification.source == "override"        # planted scenario, labelled as sample
    assert by_id["T-1014"].final_status.value == "REJECTED"
    assert provenance_note([a.model_dump(mode="json") for a in s3.items]) is not None
    assert verify_run(ctx.run_dir) == []


def test_log_lines_written_before_the_source_field_still_verify(batch):
    import json as _json
    ctx, routed = batch
    body = {"seq": 1, "txn_id": "T-1004", "employee_id": "E1002", "employee_name": "Marcus Webb",
            "submitted_at": "2026-09-04T10:27:00", "business_purpose": "Legacy line written by an older version",
            "attendees": [], "receipt": {"present": False, "vendor_name": None, "total": None, "receipt_date": None},
            "prev_hash": submissions.GENESIS}
    body["entry_hash"] = submissions._entry_hash(body)
    (ctx.run_dir / submissions.LOG_NAME).write_text(_json.dumps(body) + "\n")
    assert submissions.read_log(ctx.run_dir)[0].source == "employee"


class _DownAuditor:
    name = "always-failing"

    def audit(self, item, category, findings):
        from utils.errors import LLMResponseError
        raise LLMResponseError("all LLM providers failed: groq: rate limited; gemini: API error 503")


class _FlakyAuditor:
    """Works for the first calls, then fails: a partial outage must NOT abort the audit."""
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def audit(self, item, category, findings):
        from utils.errors import LLMResponseError
        from schemas.models import LLMAuditResult
        self.calls += 1
        if self.calls > 2:
            raise LLMResponseError("temporary failure")
        return LLMAuditResult(verdict="APPROVE", rationale="Looks specific and compliant.", confidence=0.9)


def _load_samples(ctx, routed):
    from stages.stage2_justify import sample_for
    from schemas.models import PolicyRules
    from utils.loaders import load_json_model
    rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
    for t, (j, kind) in sample_for(list(routed.values()), rules, ctx.seed, workspace.SAMPLE_DATA).items():
        submissions.submit(ctx.run_dir, routed[t], j.business_purpose, j.attendees, j.receipt, source=kind)


def test_total_llm_outage_stops_the_audit_and_can_be_retried(batch):
    from dataclasses import replace
    from stages import stage3_audit
    from utils.errors import LLMUnavailableError
    ctx, routed = batch
    _load_samples(ctx, routed)
    workspace.run_stage(replace(ctx, mock_llm=True), 2)
    with pytest.raises(LLMUnavailableError, match="not responding"):
        stage3_audit.run(ctx, auditor=_DownAuditor())
    assert not (ctx.run_dir / "03_compliance_audited.json").exists()      # nothing systemic was saved
    assert workspace.batch_info(ctx.run_dir)["state"] == "audit incomplete"
    workspace.run_audit(ctx, mock_llm=True, providers=None)               # retry completes; justifications not re-entered
    assert workspace.batch_info(ctx.run_dir)["state"] == "complete"


def test_partial_llm_outage_still_completes_with_manual_review(batch):
    from dataclasses import replace
    from stages import stage3_audit
    ctx, routed = batch
    _load_samples(ctx, routed)
    workspace.run_stage(replace(ctx, mock_llm=True), 2)
    stage3_audit.run(ctx, auditor=_FlakyAuditor())
    s3 = read_json_artifact(ctx, 3, Stage3Payload)[1]
    assert s3.status_counts.get("MANUAL_REVIEW", 0) > 0 and any(a.llm_result for a in s3.items)
