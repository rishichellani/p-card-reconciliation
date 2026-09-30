"""Stage 2 - simulate employee justifications and receipt metadata.

In production this stage is replaced by the expense-portal / receipt-OCR feed. Here it is a seeded, deterministic
simulation (same seed -> same output) with planted scenarios from ``justification_overrides.json``.
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path
from datetime import datetime, time, timedelta
from decimal import Decimal

from pydantic import ValidationError

from schemas.models import (
    Justification,
    JustifiedTransaction,
    PolicyRules,
    ReceiptMetadata,
    RoutedTransaction,
    Stage1Payload,
    Stage2Payload,
)
from utils import submissions
from utils.artifacts import read_json_artifact, write_json_artifact
from utils.context import RunContext
from utils.errors import PipelineError
from utils.loaders import load_json_model

log = logging.getLogger(__name__)

GOOD_PURPOSES = {
    "SOFTWARE": [
        "Monthly {merchant} subscription used daily by the {dept} team for project delivery.",
        "Renewal of {merchant} seats for the {dept} team; already in the approved tool catalog.",
    ],
    "MEALS": ["Working lunch with Contoso Ltd. to review Q4 delivery plan."],
    "AIRFARE": ["Economy airfare to Austin for the customer workshop with Fabrikam, Oct 12-14."],
    "LODGING": ["Two nights lodging for the Austin customer workshop, Oct 12-14."],
    "OFFICE_SUPPLIES": ["Toner and printer paper for the {dept} floor; stock ran out on Monday."],
    "EQUIPMENT": ["Replacement laptop charger and docking station for a failed unit in {dept}."],
    "FACILITIES": ["Replacement HVAC filters and hardware for the scheduled quarterly building maintenance."],
    "GROUND_TRANSPORT": ["Airport-to-hotel ride for the customer visit in Boston."],
    "FUEL": ["Fuel for rental car during the Denver customer visit."],
    "MARKETING": ["Paid promotion for the approved Q3 webinar campaign."],
    "TRAINING": ["Online course on Kubernetes security required for my on-call role."],
    "GIFTS": ["Holiday gift basket ($60) for Wingtip Toys account manager, Sam Ortega, our 3-year customer."],
}
VAGUE_PURPOSES = ["needed it", "team stuff", "misc", "n/a", "for work", "stuff"]
EXTERNAL_GUESTS = ["Jordan Lee (Contoso)", "Priya Shah (Contoso)", "Carlos Ortiz (Fabrikam)"]


def _simulate(routed: RoutedTransaction, category: str, seed: int) -> Justification:
    txn = routed.transaction
    rng = random.Random(f"{seed}:{txn.txn_id}")  # str seeds are hashed -> stable across runs and platforms
    if txn.amount < 0:
        return Justification(
            business_purpose=f"Refund of a prior {txn.merchant_name.title()} charge.",
            receipt=ReceiptMetadata(present=False),
        )
    templates = GOOD_PURPOSES.get(category, ["Purchase from {merchant} needed for {dept} business operations."])
    if rng.random() < 0.85:
        purpose = rng.choice(templates).format(merchant=txn.merchant_name.title(), dept=routed.department)
    else:
        purpose = rng.choice(VAGUE_PURPOSES)
    attendees: list[str] = []
    if category in {"MEALS", "ENTERTAINMENT"} and purpose not in VAGUE_PURPOSES:
        attendees = [f"{routed.employee_name} (Acme)"] + rng.sample(EXTERNAL_GUESTS, k=rng.randint(1, 3))
    if rng.random() < 0.92:
        total = txn.amount if rng.random() < 0.95 else txn.amount + Decimal(rng.randint(5, 60))
        receipt = ReceiptMetadata(
            present=True, vendor_name=txn.merchant_name.title(), total=total, receipt_date=txn.post_date
        )
    else:
        receipt = ReceiptMetadata(present=False)
    return Justification(business_purpose=purpose, attendees=attendees, receipt=receipt)


SAMPLE_SOURCES = ("justification_overrides.json", "sample_justifications_realistic.json")


def _load_overrides(data_dir: Path) -> dict[str, dict]:
    """Hand-written sample justifications keyed by txn_id, merged from every source file that exists.

    The stress-test file holds only planted scenarios. The realistic file marks its few planted problems with an
    "_issue" note; every other entry in it is a clean, compliant justification. Each entry gets "_planted" for callers.
    """
    merged: dict[str, dict] = {}
    for name in SAMPLE_SOURCES:
        path = data_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PipelineError(f"{name} is not valid JSON: {exc}") from exc
        for k, v in data.items():
            if not k.startswith("_"):
                merged[k] = {**v, "_planted": name == "justification_overrides.json" or "_issue" in v}
    if not merged:
        log.info("No sample justification files found; using pure simulation")
    return merged


def sample_for(routed_list: list[RoutedTransaction], rules: PolicyRules, seed: int,
               overrides_dir: Path) -> dict[str, tuple[Justification, str]]:
    """Default justifications for the demo button: planted scenarios where they exist, seeded templates otherwise.

    Returns {txn_id: (justification, kind)} with kind "sample_scenario" or "sample_template".
    """
    overrides = _load_overrides(overrides_dir)
    out: dict[str, tuple[Justification, str]] = {}
    for routed in routed_list:
        txn = routed.transaction
        if txn.amount <= 0:
            continue  # refunds need no justification
        if txn.txn_id in overrides:
            try:
                entry = overrides[txn.txn_id]
                kind = "sample_scenario" if entry["_planted"] else "sample_template"
                out[txn.txn_id] = (Justification.model_validate({**entry, "source": "override" if entry["_planted"] else "simulated"}), kind)
                continue
            except ValidationError as exc:
                log.error("Override for %s is invalid, using a template: %s", txn.txn_id, exc)
        out[txn.txn_id] = (_simulate(routed, rules.classify(txn.mcc).category, seed), "sample_template")
    return out


def _run_portal(ctx: RunContext) -> str:
    """Snapshot what people actually submitted in the app. Nothing is invented: a missing entry stays missing."""
    _, s1, s1_sha = read_json_artifact(ctx, 1, Stage1Payload)
    with submissions.LOCK:  # no new submissions can slip in between reading the log and writing the artifact
        latest = submissions.latest_by_txn(submissions.read_log(ctx.run_dir))
        log_sha = submissions.log_sha256(ctx.run_dir)
        items: list[JustifiedTransaction] = []
        for routed in s1.routed:
            e = latest.get(routed.transaction.txn_id)
            if e:
                src = {"employee": "portal", "sample_template": "simulated", "sample_scenario": "override"}[e.source]
                just = Justification(business_purpose=e.business_purpose, attendees=e.attendees, receipt=e.receipt,
                                     source=src, submitted_by=e.employee_name if e.source == "employee" else "Sample data",
                                     submitted_at=e.submitted_at)
            else:
                just = Justification(business_purpose="", receipt=ReceiptMetadata(present=False), source="portal")
            items.append(JustifiedTransaction(routed=routed, justification=just))
        payload = Stage2Payload(control_total_usd=s1.control_total_usd, justification_mode="portal",
                                submissions_log_sha256=log_sha, items=items, quarantined_count=len(s1.quarantined))
        n_sub = sum(1 for i in items if not i.justification.is_missing())
        log.info("Stage 2 (portal): %d of %d transactions have a submitted justification", n_sub, len(items))
        return write_json_artifact(ctx, 2, payload, upstream_sha=s1_sha,
                                   counts={"items": len(items), "submitted": n_sub, "missing": len(items) - n_sub})


def run(ctx: RunContext) -> str:
    if ctx.justifications == "portal":
        return _run_portal(ctx)
    _, s1, s1_sha = read_json_artifact(ctx, 1, Stage1Payload)
    rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
    overrides = _load_overrides(ctx.data_dir)

    items: list[JustifiedTransaction] = []
    for routed in s1.routed:
        txn = routed.transaction
        category = rules.classify(txn.mcc).category
        justification = _simulate(routed, category, ctx.seed)
        if txn.txn_id in overrides:
            try:
                entry = overrides[txn.txn_id]
                justification = Justification.model_validate({**entry, "source": "override" if entry["_planted"] else "simulated"})
            except ValidationError as exc:
                log.error("Override for %s is invalid, using simulated value: %s", txn.txn_id, exc)
        if justification.submitted_at is None:  # simulated entry time: 1-4 days after posting, office hours
            rng = random.Random(f"{ctx.seed}:{txn.txn_id}:submitted")
            justification.submitted_at = datetime.combine(txn.post_date, time(rng.randint(8, 17), rng.randint(0, 59))) \
                + timedelta(days=rng.randint(1, 4))
            justification.submitted_by = routed.employee_name
        items.append(JustifiedTransaction(routed=routed, justification=justification))

    payload = Stage2Payload(
        control_total_usd=s1.control_total_usd,
        simulation_seed=ctx.seed,
        items=items,
        quarantined_count=len(s1.quarantined),
    )
    n_over = sum(1 for i in items if i.justification.source == "override")
    log.info("Stage 2: %d justifications (%d planted overrides, %d simulated)", len(items), n_over, len(items) - n_over)
    return write_json_artifact(
        ctx, 2, payload, upstream_sha=s1_sha, counts={"items": len(items), "overrides": n_over}
    )
