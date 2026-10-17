"""Schemas for duplicate detection and the validation engine."""

from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, computed_field

from app.schemas.enums import DECISION_PRIORITY, CheckResult, Decision

# --- duplicates --------------------------------------------------------------------


class DuplicateBasis:
    """Why an earlier run was matched as a duplicate."""

    VENDOR_AND_INVOICE_NUMBER = "vendor+invoice_number"  # primary key: exact
    SAME_FILE = "same_file"  # identical file bytes: exact
    INVOICE_NUMBER_VENDOR_VARIANT = "invoice_number+vendor_variant"  # same number, vendor differs only in legal form
    VENDOR_DATE_AMOUNT = "vendor+date+amount"  # different number, same vendor, date and total

    EXACT = {VENDOR_AND_INVOICE_NUMBER, SAME_FILE}


class DuplicateMatch(BaseModel):
    run_id: str  # public run id of the earlier run
    decision: Decision
    file_name: str
    vendor_name: str | None
    invoice_number: str | None
    invoice_date: date | None
    total_amount: Decimal | None
    bases: list[str]

    @property
    def is_exact(self) -> bool:
        return bool(DuplicateBasis.EXACT.intersection(self.bases))


class DuplicateCheckResult(BaseModel):
    """Earlier runs decided APPROVED or NEEDS_REVIEW that this invoice may duplicate.

    * confirmed: the same invoice (primary key or identical file) was already APPROVED.
    * pending: the same invoice is awaiting review; it can be neither approved nor rejected yet.
    * potential: a similar invoice exists (vendor + date + amount, or a vendor-name variant).
    """

    vendor_key: str | None
    invoice_key: str | None
    confirmed: list[DuplicateMatch] = Field(default_factory=list)
    pending: list[DuplicateMatch] = Field(default_factory=list)
    potential: list[DuplicateMatch] = Field(default_factory=list)

    @property
    def has_primary_key(self) -> bool:
        return self.vendor_key is not None and self.invoice_key is not None


# --- validation --------------------------------------------------------------------


class CheckOutcome(BaseModel):
    """The result of one validation check."""

    name: str
    category: str  # "fields" | "duplicates" | "purchase_order" | "amounts"
    status: CheckResult
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    mandatory: bool
    failure_decision: Decision = Decision.NEEDS_REVIEW  # what a FAIL leads to

    @computed_field
    @property
    def effect(self) -> Decision:
        """The decision this check alone would force.

        FAIL forces `failure_decision`. A mandatory check that was SKIPPED (it could not
        run, e.g. no PO to compare against) forces NEEDS_REVIEW: approval requires
        positive evidence. PASS and WARN force nothing.
        """
        if self.status is CheckResult.FAIL:
            return self.failure_decision
        if self.status is CheckResult.SKIPPED and self.mandatory:
            return Decision.NEEDS_REVIEW
        return Decision.APPROVED


class ValidationReport(BaseModel):
    decision: Decision
    reason: str
    deciding_checks: list[str] = Field(description="Checks whose effect equals the decision")
    checks: list[CheckOutcome]

    @staticmethod
    def decide(checks: list[CheckOutcome]) -> "ValidationReport":
        decision = max((c.effect for c in checks), key=DECISION_PRIORITY.__getitem__, default=Decision.NEEDS_REVIEW)
        if not checks:
            return ValidationReport(decision=decision, reason="No checks were run.", deciding_checks=[], checks=[])
        if decision is Decision.APPROVED:
            return ValidationReport(
                decision=decision, reason="All mandatory checks passed.", deciding_checks=[], checks=checks
            )
        deciding = [c for c in checks if c.effect is decision]
        reason = "; ".join(f"{c.name}: {c.reason.rstrip('.')}" for c in deciding) + "."
        return ValidationReport(
            decision=decision, reason=reason, deciding_checks=[c.name for c in deciding], checks=checks
        )

    def check(self, name: str) -> CheckOutcome:
        return next(c for c in self.checks if c.name == name)
