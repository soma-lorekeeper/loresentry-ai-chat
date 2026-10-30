from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.errors import NO_STORE
from app.prompt import Profile
from app.routes import Deps, User, no_store, profile, uuid_or
from app.setting_check.runner import chapter

REASON_LIMIT = 500

router = APIRouter()


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: UUID


class VerdictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["INTENDED", "NOT_CONFLICT"]
    reason: str | None = Field(default=None, max_length=REASON_LIMIT)


@router.post("/projects/{project_id}/setting-checks")
async def start_check(
    project_id: str,
    body: StartIn,
    user: User,
    deps: Deps,
    who: Annotated[Profile, Depends(profile)],
) -> JSONResponse:
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    chat_map = await deps.content.map(user, project)
    node = chapter(chat_map, str(body.document_id))
    run = await deps.checks.store.begin(
        user,
        project,
        node.id,
        node.title,
        node.revision_no or 0,
        deps.checks.model.name,
    )
    deps.checks.start(
        UUID(run["id"]), user, project, node.id, who.locale or "ko", chat_map
    )
    return no_store(run, 202)


@router.get("/projects/{project_id}/setting-checks/summary")
async def check_summary(project_id: str, user: User, deps: Deps) -> JSONResponse:
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    return no_store(await deps.checks.store.summary(user, project))


@router.get("/projects/{project_id}/setting-checks/verdicts")
async def list_verdicts(project_id: str, user: User, deps: Deps) -> JSONResponse:
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    return no_store({"verdicts": await deps.checks.store.decisions(user, project)})


@router.get("/projects/{project_id}/setting-checks")
async def list_checks(
    project_id: str,
    user: User,
    deps: Deps,
    document_id: Annotated[UUID | None, Query()] = None,
) -> JSONResponse:
    project = uuid_or(project_id, "PROJECT_NOT_FOUND")
    return no_store({"runs": await deps.checks.store.runs(user, project, document_id)})


@router.get("/setting-checks/{run_id}")
async def get_check(run_id: str, user: User, deps: Deps) -> JSONResponse:
    run = uuid_or(run_id, "SETTING_CHECK_NOT_FOUND")
    return no_store(await deps.checks.store.run(user, run))


@router.post("/setting-checks/{run_id}/cancel", status_code=204)
async def cancel_check(run_id: str, user: User, deps: Deps) -> Response:
    run = uuid_or(run_id, "SETTING_CHECK_NOT_FOUND")
    if await deps.checks.store.cancel(user, run):
        deps.checks.stop(run)
    return Response(status_code=204, headers=NO_STORE)


@router.put("/setting-check-findings/{finding_id}/verdict")
async def set_verdict(
    finding_id: str, body: VerdictIn, user: User, deps: Deps
) -> JSONResponse:
    finding = uuid_or(finding_id, "SETTING_CHECK_FINDING_NOT_FOUND")
    reason = (body.reason or "").strip() or None
    return no_store(
        await deps.checks.store.set_verdict(user, finding, body.verdict, reason)
    )


@router.delete("/setting-check-findings/{finding_id}/verdict")
async def clear_verdict(finding_id: str, user: User, deps: Deps) -> JSONResponse:
    finding = uuid_or(finding_id, "SETTING_CHECK_FINDING_NOT_FOUND")
    return no_store(await deps.checks.store.clear_verdict(user, finding))
