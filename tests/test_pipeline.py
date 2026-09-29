import csv
import json
import shutil
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from run_pipeline import main
from schemas.models import RawTransaction, parse_money
from stages import stage1_ingest
from stages.deterministic_checks import vendors_match
from stages.llm_auditor import extract_json_object
from utils.artifacts import verify_run
from utils.context import RunContext
from utils.errors import LLMResponseError, PipelineError

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


@pytest.mark.parametrize("raw,expected", [("$1,234.50", "1234.50"), ("(250.00)", "-250.00"), ("-5", "-5.00"), ("84", "84.00")])
def test_parse_money_ok(raw, expected):
    assert parse_money(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["N/A", "", "12.345", "1e5", "NaN", "$$5"])
def test_parse_money_rejects(raw):
    with pytest.raises(ValueError):
        parse_money(raw)


def test_raw_transaction_rejects_non_usd_and_bad_date():
    base = dict(txn_id="T", post_date="2026-09-01", card_last4="1234", merchant_name="X", mcc="5999", amount="1.00")
    assert RawTransaction(**base).currency == "USD"
    with pytest.raises(ValidationError):
        RawTransaction(**{**base, "currency": "EUR"})
    with pytest.raises(ValidationError):
        RawTransaction(**{**base, "post_date": "someday"})


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json_object('Sure!\n```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(LLMResponseError):
        extract_json_object("no json here")


def test_vendor_matching():
    assert vendors_match("Home Depot", "HOME DEPOT")
    assert not vendors_match("Bob's Bait Shop", "TIFFANY & CO")


def make_ctx(tmp_path, data_dir=DATA):
    return RunContext(run_id="run_test_000001", run_dir=tmp_path / "run", data_dir=data_dir, mock_llm=True)


def test_stage1_quarantines_dirty_rows(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.run_dir.mkdir()
    stage1_ingest.run(ctx)
    payload = json.loads((ctx.run_dir / "01_raw_statement.json").read_text())["payload"]
    assert len(payload["routed"]) == 26 and len(payload["quarantined"]) == 6


def test_stage1_missing_file_and_columns(tmp_path):
    empty = tmp_path / "d"
    empty.mkdir()
    shutil.copy(DATA / "employees.json", empty)
    ctx = make_ctx(tmp_path, empty)
    ctx.run_dir.mkdir()
    with pytest.raises(PipelineError, match="not found"):
        stage1_ingest.run(ctx)
    (empty / "transactions.csv").write_text("txn_id,amount\nT1,5\n")
    with pytest.raises(PipelineError, match="missing required columns"):
        stage1_ingest.run(ctx)


def test_end_to_end_balanced_immutable_and_tamper_evident(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["run_pipeline", "--mock-llm", "--output-dir", str(out), "--run-id", "run_e2e_000001"])
    assert main() == 0
    run_dir = out / "run_e2e_000001"

    rows = list(csv.DictReader((run_dir / "04_erp_journal_entry.csv").open()))
    assert sum(Decimal(r["debit"]) for r in rows) == sum(Decimal(r["credit"]) for r in rows)
    liability = sum(Decimal(r["credit"]) - Decimal(r["debit"]) for r in rows if r["gl_account"] == "2100")
    assert liability == Decimal("24833.60")
    # blocked MCC (casino T-1012) must never hit an expense account
    assert {r["gl_account"] for r in rows if r["txn_id"] == "T-1012"} == {"1450", "2100"}
    assert verify_run(run_dir) == []

    # artifacts are write-once: re-running stage 1 into the same run is refused
    monkeypatch.setattr(sys, "argv", ["run_pipeline", "--mock-llm", "--output-dir", str(out),
                                      "--run-id", "run_e2e_000001", "--start-stage", "1"])
    assert main() == 1

    # tamper detection
    f = run_dir / "02_user_justified.json"
    f.chmod(0o644)
    f.write_text(f.read_text().replace("Marriott", "Marrio77"))
    assert verify_run(run_dir)
