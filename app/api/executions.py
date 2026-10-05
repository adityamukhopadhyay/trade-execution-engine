"""Executions: submit a portfolio, list runs, read one run."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import get_service
from app.core.models import ExecuteRequest, ExecutionReport, RunStatus
from app.execution.service import ExecutionService

router = APIRouter(prefix="/executions", tags=["executions"])

Service = Annotated[ExecutionService, Depends(get_service)]


@router.post("", response_model=ExecutionReport, status_code=202,
             responses={200: {"description": "dry run, idempotent replay, or finished with wait=true"},
                        401: {"description": "session missing or expired"},
                        409: {"description": "idempotency key reused with a different payload, "
                                             "or its run is no longer stored"},
                        422: {"description": "portfolio_invalid: every issue listed in details"}})
async def create_execution(
    req: ExecuteRequest, response: Response, service: Service,
    wait: Annotated[bool, Query(description="block until the run is terminal")] = False,
) -> ExecutionReport:
    report, replayed = await service.submit(req, wait=wait)
    if replayed:
        response.headers["Idempotent-Replay"] = "true"
    response.status_code = 202 if report.status is RunStatus.RUNNING and not replayed else 200
    return report


@router.get("", response_model=list[ExecutionReport])
async def list_executions(
    service: Service,
    session_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ExecutionReport]:
    """Newest first."""
    return service.list(limit=limit, session_id=session_id)


@router.get("/{run_id}", response_model=ExecutionReport, responses={404: {"description": "run_not_found"}})
async def get_execution(run_id: str, service: Service) -> ExecutionReport:
    return service.get(run_id)
