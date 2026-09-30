"""Stage 3 - compliance audit: deterministic hard rules first, then a free-tier LLM for the qualitative policy judgement."""
from __future__ import annotations

import logging
from collections import Counter

from schemas.models import (
    AuditedTransaction,
    AuditStatus,
    EmployeeDirectory,
    Finding,
    PolicyRules,
    Severity,
    Stage2Payload,
    Stage3Payload,
    worst,
)
from stages.deterministic_checks import run_checks, status_from_findings
from stages.llm_auditor import Auditor, LLMAuditor, MockAuditor
from utils import llm_cache
from utils.artifacts import read_json_artifact, write_json_artifact
from utils.context import RunContext
from utils.errors import LLMResponseError, LLMUnavailableError, PipelineError
from utils.llm_client import LLMClient, load_providers
from utils.loaders import load_json_model, read_bytes

log = logging.getLogger(__name__)

# A run of consecutive LLM failures means the connection is down, not that the transactions are odd. Before any success
# three in a row is enough; after successes we allow a longer streak so a brief rate-limit blip does not abort a run.
SYSTEMIC_FAILURE_THRESHOLD = 3
SYSTEMIC_STREAK_AFTER_SUCCESS = 5

VERDICT_TO_STATUS = {
    "APPROVE": AuditStatus.APPROVED,
    "FLAG": AuditStatus.FLAGGED,
    "REJECT": AuditStatus.REJECTED,
}


def build_auditor(ctx: RunContext) -> Auditor:
    if ctx.mock_llm:
        log.warning("Using MockAuditor: results are NOT a real compliance review")
        return MockAuditor()
    policy = read_bytes(ctx.data_dir / "spending_policy.md").decode("utf-8")
    providers = load_providers(order=ctx.providers)  # raises LLMAuthError (fatal) if no key is configured
    log.info("LLM providers (priority order): %s",
             ", ".join(f"{p.name}/{p.model}" for p in providers))
    return LLMAuditor(policy_text=policy, client=LLMClient(providers))


def run(ctx: RunContext, auditor: Auditor | None = None, progress=None) -> str:
    _, s2, s2_sha = read_json_artifact(ctx, 2, Stage2Payload)
    rules = load_json_model(ctx.data_dir / "policy_rules.json", PolicyRules)
    employees = load_json_model(ctx.data_dir / "employees.json", EmployeeDirectory).by_id()
    auditor = auditor or build_auditor(ctx)

    det_findings = run_checks(s2.items, employees, rules)
    audited: list[AuditedTransaction] = []
    llm_ok = streak = reused = 0
    cached = llm_cache.load(ctx.run_dir)  # verdicts from an earlier, interrupted attempt at this same audit

    for n, item in enumerate(s2.items, start=1):
        txn = item.routed.transaction
        info = rules.classify(txn.mcc)
        findings = list(det_findings[txn.txn_id])
        det_status = status_from_findings(findings)
        llm_result, llm_error = None, None
        final = det_status

        if progress:
            progress(n, len(s2.items), txn.txn_id)
        if item.justification.is_missing():
            log.info("[%d/%d] %s no justification submitted; LLM skipped (%s)", n, len(s2.items), txn.txn_id, det_status.value)
        elif det_status is AuditStatus.REJECTED:
            log.info("[%d/%d] %s deterministic REJECT (%s); LLM skipped", n, len(s2.items), txn.txn_id,
                     ",".join(f.code for f in findings if f.severity is Severity.REJECT))
        else:
            try:
                key = auditor.cache_key(item, info.category, findings) if hasattr(auditor, "cache_key") else None
                if key is not None and key in cached:
                    llm_result = cached[key]
                    reused += 1
                    log.info("[%d/%d] %s verdict reused from an earlier attempt", n, len(s2.items), txn.txn_id)
                else:
                    llm_result = auditor.audit(item, info.category, findings)
                    if key is not None:
                        llm_cache.append(ctx.run_dir, key, txn.txn_id, llm_result)
                llm_status = VERDICT_TO_STATUS[llm_result.verdict]
                if llm_result.verdict == "APPROVE" and llm_result.confidence < rules.min_llm_confidence:
                    llm_status = AuditStatus.FLAGGED
                    findings.append(Finding(
                        code="LOW_LLM_CONFIDENCE", severity=Severity.FLAG,
                        message=f"auditor approved with confidence {llm_result.confidence:.2f} < {rules.min_llm_confidence}",
                    ))
                llm_ok += 1
                streak = 0
                final = worst(det_status, llm_status)  # LLM can escalate, never downgrade deterministic findings
                log.info("[%d/%d] %s det=%s llm=%s(%.2f) -> %s", n, len(s2.items), txn.txn_id,
                         det_status.value, llm_result.verdict, llm_result.confidence, final.value)
            except LLMResponseError as exc:
                streak += 1
                if streak >= (SYSTEMIC_FAILURE_THRESHOLD if llm_ok == 0 else SYSTEMIC_STREAK_AFTER_SUCCESS):
                    raise LLMUnavailableError(
                        f"The LLM providers are not responding ({streak} attempts in a row failed), so the audit "
                        f"was stopped instead of sending everything to manual review. Nothing from this stage was saved; "
                        f"fix the connection and run the audit again. Last error: {exc}") from exc
                llm_error = str(exc)
                final = worst(det_status, AuditStatus.MANUAL_REVIEW)
                log.error("[%d/%d] %s LLM audit failed -> MANUAL_REVIEW: %s", n, len(s2.items), txn.txn_id, exc)

        audited.append(AuditedTransaction(
            txn_id=txn.txn_id, justified=item, category=info.category, gl_account=info.gl_account,
            findings=findings, deterministic_status=det_status, llm_result=llm_result,
            llm_error=llm_error, final_status=final,
        ))

    if len(audited) != len(s2.items):
        raise PipelineError("Audit lost transactions; refusing to write artifact")

    counts = Counter(a.final_status.value for a in audited)
    payload = Stage3Payload(
        control_total_usd=s2.control_total_usd,
        auditor=auditor.name,
        status_counts=dict(counts),
        items=audited,
        quarantined_count=s2.quarantined_count,
    )
    log.info("Stage 3: %s (%d verdicts reused from an earlier attempt)", dict(counts), reused)
    return write_json_artifact(ctx, 3, payload, upstream_sha=s2_sha, counts={**counts, "llm_reused": reused})
