"""Status and result enums shared by the database models, pipeline and API."""

from enum import StrEnum


class POStatus(StrEnum):
    APPROVED = "APPROVED"
    CLOSED = "CLOSED"


class Decision(StrEnum):
    """Outcome of the validation engine."""

    APPROVED = "APPROVED"  # every mandatory check passed
    NEEDS_REVIEW = "NEEDS_REVIEW"  # a mandatory check failed or could not run, or a check flagged the invoice
    REJECTED = "REJECTED"  # confirmed duplicate of an approved invoice


# Higher wins. The decision is the highest-priority effect of any check, so a passing
# check can never override a failure.
DECISION_PRIORITY = {Decision.APPROVED: 0, Decision.NEEDS_REVIEW: 1, Decision.REJECTED: 2}


class RunStatus(StrEnum):
    """Processing lifecycle of a run. Independent of the business decision."""

    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"  # every stage ran; `decision` holds the business outcome
    FAILED = "FAILED"  # a stage failed (e.g. unreadable PDF); there is no decision


class WorkflowStage(StrEnum):
    """Workflow stages, in execution order. COMPLETED and FAILED are terminal."""

    RECEIVED = "RECEIVED"
    EXTRACTION = "EXTRACTION"
    NORMALIZATION = "NORMALIZATION"
    REQUIRED_FIELD_VALIDATION = "REQUIRED_FIELD_VALIDATION"
    DUPLICATE_CHECK = "DUPLICATE_CHECK"
    PO_MATCHING = "PO_MATCHING"
    BUSINESS_VALIDATION = "BUSINESS_VALIDATION"
    DECISION = "DECISION"
    PERSISTENCE = "PERSISTENCE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StageStatus(StrEnum):
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CheckResult(StrEnum):
    """Status of one validation check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
