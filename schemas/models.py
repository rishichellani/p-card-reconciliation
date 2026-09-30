"""Pydantic schemas for every stage boundary of the P-Card pipeline.

Raw inputs are parsed leniently (currency symbols, thousands separators, several
date formats) but *strictly validated*: a row that cannot be coerced into a clean
model is quarantined by Stage 1 instead of crashing the run.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

TWO_PLACES = Decimal("0.01")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d")
MAX_ABS_AMOUNT = Decimal("1000000")
# Proper US grouping only ("1,234.50", "1234.50"). "1,23" and "12 34" are rejected, not guessed: guessing turns a
# European "1,23" into $123.00. ASCII digits only; parentheses or a leading minus mean a credit.
_MONEY_RE = re.compile(r"^(?P<open>\()?(?P<sign>-)?(?P<cur>\$)?(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?P<close>\))?$", re.ASCII)
MIN_DATE, MAX_DATE = date(2000, 1, 1), date(2099, 12, 31)


def parse_money(value: Any) -> Decimal:
    """'$1,234.50' -> 1234.50, '(250.00)' -> -250.00. Raises ValueError on anything else."""
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        d = Decimal(str(value))
    elif isinstance(value, str):
        m = _MONEY_RE.match(value.strip())
        if not m or bool(m["open"]) != bool(m["close"]):
            raise ValueError(f"not a valid money amount: {value!r}")
        try:
            d = Decimal(m["num"].replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(f"not a valid money amount: {value!r}") from exc
        d = -d if (m["sign"] or m["open"]) else d
    else:
        raise ValueError(f"unsupported amount type: {type(value).__name__}")
    if not d.is_finite():
        raise ValueError("amount must be finite")
    if d != d.quantize(TWO_PLACES):
        raise ValueError(f"amount has more than 2 decimal places: {value!r}")
    if abs(d) > MAX_ABS_AMOUNT:
        raise ValueError(f"amount out of range: {value!r}")
    return d.quantize(TWO_PLACES)


def parse_date(value: Any) -> date:
    if isinstance(value, datetime):
        found = value.date()
    elif isinstance(value, date):
        found = value
    elif isinstance(value, str):
        found = None
        for fmt in DATE_FORMATS:
            try:
                found = datetime.strptime(value.strip(), fmt).date()
                break
            except ValueError:
                continue
    else:
        found = None
    if found is not None:
        if not MIN_DATE <= found <= MAX_DATE:
            raise ValueError(f"date out of range ({MIN_DATE} to {MAX_DATE}): {value!r}")
        return found
    raise ValueError(f"unrecognised date: {value!r} (accepted: {', '.join(DATE_FORMATS)})")


# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #
class Employee(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    employee_id: str = Field(pattern=r"^E[0-9]{4}$")
    name: str
    email: str
    department: str
    cost_center: str = Field(pattern=r"^[0-9]{4}$")
    manager_id: str | None = None
    card_last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    single_txn_limit: Decimal = Field(ge=0)
    monthly_limit: Decimal = Field(ge=0)
    active: bool = True


class EmployeeDirectory(BaseModel):
    employees: list[Employee]

    @model_validator(mode="after")
    def _unique_keys(self) -> "EmployeeDirectory":
        ids = [e.employee_id for e in self.employees]
        cards = [e.card_last4 for e in self.employees if e.card_last4]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate employee_id in directory")
        if len(cards) != len(set(cards)):
            raise ValueError("duplicate card_last4 in directory")
        return self

    def by_card(self) -> dict[str, Employee]:
        return {e.card_last4: e for e in self.employees if e.card_last4}

    def by_id(self) -> dict[str, Employee]:
        return {e.employee_id: e for e in self.employees}


class MccInfo(BaseModel):
    category: str
    gl_account: str = Field(pattern=r"^[0-9]{4}$")


class PolicyRules(BaseModel):
    """Deterministic thresholds and mappings. The qualitative policy lives in spending_policy.md."""

    receipt_required_over: Decimal
    receipt_amount_tolerance_pct: Decimal
    duplicate_window_days: int = Field(ge=0)
    min_justification_chars: int = Field(ge=0)
    min_llm_confidence: float = Field(ge=0, le=1)
    liability_account: str = Field(pattern=r"^[0-9]{4}$")
    suspense_account: str = Field(pattern=r"^[0-9]{4}$")
    default_category: str
    default_gl_account: str = Field(pattern=r"^[0-9]{4}$")
    gl_accounts: dict[str, str]
    mcc_map: dict[str, MccInfo]
    blocked_mccs: dict[str, str]

    def classify(self, mcc: str) -> MccInfo:
        return self.mcc_map.get(mcc) or MccInfo(
            category=self.default_category, gl_account=self.default_gl_account
        )


# --------------------------------------------------------------------------- #
# Stage 1 - raw statement
# --------------------------------------------------------------------------- #
class RawTransaction(BaseModel):
    """One bank-statement row after coercion. Anything that fails here is quarantined."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore", frozen=True)

    txn_id: str = Field(min_length=1, max_length=32)
    post_date: date
    card_last4: str = Field(pattern=r"^[0-9]{4}$")
    merchant_name: str = Field(min_length=1, max_length=120)
    mcc: str = Field(pattern=r"^[0-9]{4}$")
    amount: Decimal
    currency: Literal["USD"] = "USD"
    statement_memo: str = Field(default="", max_length=500)

    @field_validator("txn_id", "merchant_name", "statement_memo", mode="before")
    @classmethod
    def _clean_text(cls, v: Any) -> Any:
        """Tabs/newlines become spaces; any other control character (NUL, bell, escape...) rejects the row."""
        if not isinstance(v, str):
            return v
        v = re.sub(r"[\t\r\n]+", " ", v)
        if any(unicodedata.category(ch) == "Cc" for ch in v):
            raise ValueError("contains control characters")
        return v

    @field_validator("post_date", mode="before")
    @classmethod
    def _date(cls, v: Any) -> date:
        return parse_date(v)

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, v: Any) -> Decimal:
        return parse_money(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and not v.strip()):
            return "USD"
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("statement_memo", mode="before")
    @classmethod
    def _memo(cls, v: Any) -> Any:
        return "" if v is None else v


class RoutedTransaction(BaseModel):
    transaction: RawTransaction
    employee_id: str
    employee_name: str
    department: str
    cost_center: str
    approver_id: str | None
    employee_active: bool


class RejectedRow(BaseModel):
    row_number: int
    raw: dict[str, str | None]
    reasons: list[str]


class Stage1Payload(BaseModel):
    source_file: str
    source_sha256: str
    statement_period_start: date | None
    statement_period_end: date | None
    control_total_usd: Decimal  # net sum of routed transactions; ERP must tie out to this
    routed: list[RoutedTransaction]
    quarantined: list[RejectedRow]


# --------------------------------------------------------------------------- #
# Stage 2 - employee justification
# --------------------------------------------------------------------------- #
class ReceiptMetadata(BaseModel):
    present: bool
    vendor_name: str | None = None
    total: Decimal | None = None
    receipt_date: date | None = None


class Justification(BaseModel):
    business_purpose: str
    attendees: list[str] = Field(default_factory=list)
    receipt: ReceiptMetadata
    # "simulated" = demo text (hand-written or template-generated), "override" = planted demo problem. A real portal feed would use "portal".
    source: Literal["simulated", "override", "portal"] = "simulated"
    submitted_by: str | None = None
    submitted_at: datetime | None = None

    def is_missing(self) -> bool:
        """True for a portal batch where the cardholder never submitted anything for this transaction."""
        return self.source == "portal" and self.submitted_at is None


class JustifiedTransaction(BaseModel):
    routed: RoutedTransaction
    justification: Justification


class Stage2Payload(BaseModel):
    control_total_usd: Decimal
    justification_mode: Literal["simulated", "portal"] = "simulated"
    simulation_seed: int | None = None  # simulated mode only
    submissions_log_sha256: str | None = None  # portal mode only: fingerprint of the submissions log this snapshot came from
    items: list[JustifiedTransaction]
    quarantined_count: int


# --------------------------------------------------------------------------- #
# Stage 3 - compliance audit
# --------------------------------------------------------------------------- #
class AuditStatus(str, Enum):
    APPROVED = "APPROVED"
    FLAGGED = "FLAGGED"
    MANUAL_REVIEW = "MANUAL_REVIEW"  # LLM unavailable / invalid response
    REJECTED = "REJECTED"


STATUS_RANK = {
    AuditStatus.APPROVED: 0,
    AuditStatus.FLAGGED: 1,
    AuditStatus.MANUAL_REVIEW: 2,
    AuditStatus.REJECTED: 3,
}


def worst(*statuses: AuditStatus) -> AuditStatus:
    return max(statuses, key=STATUS_RANK.__getitem__)


class Severity(str, Enum):
    INFO = "INFO"
    FLAG = "FLAG"
    REJECT = "REJECT"


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str


class LLMAuditResult(BaseModel):
    """Contract for the LLM's qualitative verdict. Anything that fails this is treated as an invalid response."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["APPROVE", "FLAG", "REJECT"]
    policy_sections: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=10, max_length=1500)
    confidence: float = Field(ge=0.0, le=1.0)
    red_flags: list[str] = Field(default_factory=list)
    served_by: str | None = None  # "provider:model"; stamped by code, never trusted from the model

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalise_verdict(cls, v: Any) -> Any:
        """Free-tier models drift on casing/synonyms; accept pass/fail wording."""
        if isinstance(v, str):
            v = v.strip().upper()
            return {"PASS": "APPROVE", "APPROVED": "APPROVE", "FAIL": "REJECT", "REJECTED": "REJECT",
                    "FLAGGED": "FLAG"}.get(v, v)
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        """Binary pass/fail view of the verdict (only APPROVE passes)."""
        return self.verdict == "APPROVE"


class AuditedTransaction(BaseModel):
    txn_id: str
    justified: JustifiedTransaction
    category: str
    gl_account: str
    findings: list[Finding]
    deterministic_status: AuditStatus
    llm_result: LLMAuditResult | None = None
    llm_error: str | None = None
    final_status: AuditStatus


class Stage3Payload(BaseModel):
    control_total_usd: Decimal
    auditor: str  # e.g. "llm-router[gemini>groq]" or "mock-heuristic"
    status_counts: dict[str, int]
    items: list[AuditedTransaction]
    quarantined_count: int


# --------------------------------------------------------------------------- #
# Stage 4 - ERP journal
# --------------------------------------------------------------------------- #
class JournalLine(BaseModel):
    journal_id: str
    line_no: int = Field(ge=1)
    posting_date: date
    gl_account: str = Field(pattern=r"^[0-9]{4}$")
    gl_account_name: str
    cost_center: str = Field(pattern=r"^[0-9]{4}$")
    debit: Decimal = Field(ge=0)
    credit: Decimal = Field(ge=0)
    currency: Literal["USD"] = "USD"
    description: str = Field(max_length=80)
    txn_id: str
    employee_id: str
    audit_status: AuditStatus

    @model_validator(mode="after")
    def _one_sided(self) -> "JournalLine":
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("a journal line must carry exactly one of debit or credit")
        return self
