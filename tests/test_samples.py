"""The two sample statements shipped with the app: what each is supposed to demonstrate, pinned so edits cannot drift."""
import csv
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from schemas.models import Justification, PolicyRules, Stage1Payload, Stage3Payload
from stages.stage2_justify import sample_for
from utils import submissions, workspace
from utils.artifacts import read_json_artifact, verify_run
from utils.loaders import load_json_model

DATA = workspace.SAMPLE_DATA
EXPECTED_EXCEPTIONS = {          # txn -> (final status, rule codes that fire)
    "R-2019": ("FLAGGED", {"MISSING_RECEIPT"}),
    "R-2022": ("REJECTED", {"BLOCKED_MCC"}),
    "R-2023": ("FLAGGED", set()),           # judged by the auditor: alcohol, no attendees, "team bonding"
    "R-2033": ("REJECTED", set()),          # judged by the auditor: luxury gift, no named recipient
    "R-2049": ("REJECTED", {"INACTIVE_CARDHOLDER"}),
}


@pytest.fixture(scope="module")
def realistic(tmp_path_factory):
    old = workspace.OUTPUT
    workspace.OUTPUT = tmp_path_factory.mktemp("out") / "output"
    try:
        ctx = workspace.create_batch((DATA / "sample_realistic.csv").read_bytes(), label="realistic")
        routed = {r.transaction.txn_id: r for r in read_json_artifact(ctx, 1, Stage1Payload)[1].routed}
        rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
        samples = sample_for(list(routed.values()), rules, ctx.seed, DATA)
        for t, (j, kind) in samples.items():
            submissions.submit(ctx.run_dir, routed[t], j.business_purpose, j.attendees, j.receipt, source=kind)
        workspace.run_audit(ctx, mock_llm=True, providers=None)
        yield ctx, routed, samples, read_json_artifact(ctx, 3, Stage3Payload)[1]
    finally:
        workspace.OUTPUT = old


def test_realistic_sample_shape(realistic):
    ctx, routed, samples, s3 = realistic
    stage1 = read_json_artifact(ctx, 1, Stage1Payload)[1]
    assert len(routed) == 50 and len(stage1.quarantined) == 6
    assert stage1.control_total_usd == Decimal("19767.23")
    assert set(routed) - set(samples) == {"R-2027"}                      # the only row without a justification is the refund


def test_realistic_sample_is_ninety_percent_approved_with_exactly_the_planted_exceptions(realistic):
    _, _, _, s3 = realistic
    counts = Counter(a.final_status.value for a in s3.items)
    assert counts["APPROVED"] == 45 and len(s3.items) == 50              # 90%
    exceptions = {a.txn_id: a for a in s3.items if a.final_status.value != "APPROVED"}
    assert set(exceptions) == set(EXPECTED_EXCEPTIONS)
    for txn, (status, codes) in EXPECTED_EXCEPTIONS.items():
        got = exceptions[txn]
        assert got.final_status.value == status, txn
        assert {f.code for f in got.findings if f.severity.value != "INFO"} == codes, txn


def test_no_clean_transaction_trips_any_rule(realistic):
    _, _, _, s3 = realistic
    noisy = {a.txn_id: [f.code for f in a.findings if f.severity.value != "INFO"]
             for a in s3.items if a.txn_id not in EXPECTED_EXCEPTIONS and any(f.severity.value != "INFO" for f in a.findings)}
    assert noisy == {}


def test_planted_entries_are_the_ones_marked_with_an_issue_note(realistic):
    _, _, samples, _ = realistic
    marked = {k for k, v in json.load(open(DATA / "sample_justifications_realistic.json")).items() if isinstance(v, dict) and "_issue" in v}
    assert marked == set(EXPECTED_EXCEPTIONS)
    assert {t for t, (_, kind) in samples.items() if kind == "sample_scenario"} == marked
    assert all(kind == "sample_template" for t, (_, kind) in samples.items() if t not in marked)


def test_realistic_sample_files_are_consistent():
    ids = [r["txn_id"] for r in csv.DictReader(open(DATA / "sample_realistic.csv", newline=""))]
    js = {k: v for k, v in json.load(open(DATA / "sample_justifications_realistic.json")).items() if not k.startswith("_")}
    assert set(js) <= set(ids)                                            # no justification for a row that does not exist
    for k, v in js.items():
        j = Justification.model_validate(v)
        assert len(j.business_purpose) >= 15
        if j.receipt.present:
            assert j.receipt.total is not None and j.receipt.vendor_name


def test_realistic_run_passes_the_integrity_check(realistic):
    assert verify_run(realistic[0].run_dir) == []


def test_stress_test_sample_is_unchanged_and_still_full_of_problems(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "OUTPUT", tmp_path / "output")
    ctx = workspace.create_batch((DATA / "transactions.csv").read_bytes())
    routed = {r.transaction.txn_id: r for r in read_json_artifact(ctx, 1, Stage1Payload)[1].routed}
    assert len(routed) == 26
    rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
    for t, (j, kind) in sample_for(list(routed.values()), rules, ctx.seed, DATA).items():
        submissions.submit(ctx.run_dir, routed[t], j.business_purpose, j.attendees, j.receipt, source=kind)
    workspace.run_audit(ctx, mock_llm=True, providers=None)
    counts = Counter(a.final_status.value for a in read_json_artifact(ctx, 3, Stage3Payload)[1].items)
    assert counts["APPROVED"] < 15 and counts["REJECTED"] >= 4


def test_sample_labels_do_not_call_hand_written_text_random(realistic):
    from utils.export import SOURCE_LABEL, source_label
    assert "random" not in " ".join(SOURCE_LABEL.values()).lower()
    _, _, _, s3 = realistic
    clean = next(a for a in s3.items if a.txn_id == "R-2001").justified.justification.model_dump(mode="json")
    planted = next(a for a in s3.items if a.txn_id == "R-2033").justified.justification.model_dump(mode="json")
    assert source_label(clean) == "SAMPLE DATA (demo text)" and source_label(planted) == "SAMPLE DATA (planted demo scenario)"


def test_the_trace_says_which_side_decided_the_final_status(realistic):
    from utils.export import build_trace
    ctx, _, _, _ = realistic
    trace = build_trace(ctx.run_dir)
    final = lambda t: next(s["detail"] for s in trace[t] if s["step"] == "Final status")
    # R-2019: the receipt rule flags it, the auditor approves: the RULE stands
    assert "rule result (Flagged) is worse than the Mock auditor verdict (Approved)" in final("R-2019") and "never override a rule" in final("R-2019")
    # R-2033: no rule fires, the auditor rejects: the AUDITOR stands
    assert "Mock auditor verdict (Rejected) is worse than the rule result (Approved)" in final("R-2033") and "verdict stands" in final("R-2033")
    # a clean transaction: both agree
    assert final("R-2001") == "Approved. The rules and the Mock auditor agree."
    # hard rule reject: the auditor is never asked
    assert "A hard rule decided this" in final("R-2022")
