from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api import presenters
from app.api.errors import ApiError
from app.db import get_db
from app.db.models import ProcessingRun
from app.schemas import Decision, ErrorResponse, RunDetail, RunPage, RunResult, RunStats, RunStatus

router = APIRouter(prefix="/runs", tags=["runs"])

RUN_ID = Path(..., pattern=r"^[0-9a-f]{32}$", description="The run's 32-character hexadecimal id")
_NOT_FOUND = {404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}}


def _get_run(db: Session, run_id: str) -> ProcessingRun:
    run = db.scalar(select(ProcessingRun).where(ProcessingRun.run_id == run_id))
    if run is None:
        raise ApiError(404, "run_not_found", f"No run with id '{run_id}'.")
    return run


@router.get("", response_model=RunPage, responses={422: {"model": ErrorResponse}})
def list_runs(
    status: RunStatus | None = Query(None, description="Filter by processing status"),
    decision: Decision | None = Query(None, description="Filter by business decision"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> RunPage:
    """Run history, newest first."""
    query = select(ProcessingRun)
    if status is not None:
        query = query.where(ProcessingRun.status == status)
    if decision is not None:
        query = query.where(ProcessingRun.decision == decision)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    runs = db.scalars(
        query.order_by(ProcessingRun.created_at.desc(), ProcessingRun.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return RunPage(
        items=[presenters.summary(r) for r in runs],
        total=total,
        page=page,
        page_size=page_size,
        has_next=page * page_size < total,
    )


@router.get("/stats", response_model=RunStats)
def run_stats(db: Session = Depends(get_db)) -> RunStats:
    """Counts by status and decision, and average duration, over all runs.

    Declared before /{run_id} so that "stats" is not taken for a run id.
    """
    by_status = dict(db.execute(select(ProcessingRun.status, func.count()).group_by(ProcessingRun.status)).all())
    by_decision = dict(
        db.execute(
            select(ProcessingRun.decision, func.count())
            .where(ProcessingRun.decision.is_not(None))
            .group_by(ProcessingRun.decision)
        ).all()
    )
    average, last_run_at = db.execute(
        select(
            func.avg(ProcessingRun.duration_ms).filter(
                ProcessingRun.status.in_((RunStatus.COMPLETED, RunStatus.FAILED))
            ),
            func.max(ProcessingRun.created_at),
        )
    ).one()
    return RunStats(
        total_runs=sum(by_status.values()),
        completed=by_status.get(RunStatus.COMPLETED, 0),
        failed=by_status.get(RunStatus.FAILED, 0),
        in_progress=by_status.get(RunStatus.RECEIVED, 0) + by_status.get(RunStatus.PROCESSING, 0),
        approved=by_decision.get(Decision.APPROVED, 0),
        needs_review=by_decision.get(Decision.NEEDS_REVIEW, 0),
        rejected=by_decision.get(Decision.REJECTED, 0),
        average_duration_ms=average,
        last_run_at=last_run_at,
    )


@router.get("/{run_id}", response_model=RunDetail, responses=_NOT_FOUND)
def get_run(run_id: str = RUN_ID, db: Session = Depends(get_db)) -> RunDetail:
    """Everything recorded about a run: stages, checks, events, extracted fields."""
    return presenters.detail(_get_run(db, run_id))


@router.get("/{run_id}/result", response_model=RunResult, responses=_NOT_FOUND | {409: {"model": ErrorResponse}})
def get_run_result(run_id: str = RUN_ID, db: Session = Depends(get_db)) -> RunResult:
    """The decision, reasons, extracted fields and checks.

    For a FAILED run this is 200 with status FAILED, decision null and the error. For
    a run that has not finished it is 409.
    """
    run = _get_run(db, run_id)
    if run.status in (RunStatus.RECEIVED, RunStatus.PROCESSING):
        raise ApiError(409, "run_not_finished", f"The run is still {run.status.value}; try again shortly.",
                       run=presenters.summary(run))
    return presenters.result(run)
