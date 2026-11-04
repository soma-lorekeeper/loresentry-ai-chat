import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from starlette.exceptions import HTTPException

from app import db
from app.content import ContentClient, ContentSource
from app.errors import (
    NO_STORE,
    ChatFailure,
    UnexpectedErrorMiddleware,
    handle_failure,
    handle_http,
    handle_unexpected,
    handle_validation,
)
from app.logs import (
    AppLogger,
    LogField,
    LogFlow,
    RequestLogMiddleware,
    configure_logging,
    setup_tracing,
)
from app.manuscript import ProjectKeys, kms_client
from app.model import (
    AnswerModel,
    CheckModel,
    OpenAICheckModel,
    OpenAIModel,
    UnconfiguredModel,
)
from app.routes import Services, router
from app.setting_check.routes import router as check_router
from app.setting_check.runner import CheckRunner
from app.setting_check.store import CheckStore
from app.settings import Settings
from app.store import Store
from app.turns import TurnRunner

SERVICE = "ai-chat-api"

# uvicorn이 "Started server process"를 찍기 전, 이 모듈을 import할 때 로그(ECS JSON)를 설정한다.
# 추적 엔진과 계측(content 호출의 httpx, DB 쿼리의 psycopg, KMS의 botocore)은 EDOT distro가 프로세스 시작 때 켠다
configure_logging(SERVICE)


def create_app(
    settings: Settings | None = None,
    content: ContentSource | None = None,
    model: AnswerModel | None = None,
    checker: CheckModel | None = None,
    keys: ProjectKeys | None = None,
) -> FastAPI:
    config = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            pool = AsyncConnectionPool(
                config.conninfo,
                min_size=1,
                max_size=config.db_pool_size,
                kwargs={"row_factory": dict_row, "autocommit": True},
                # Test each connection before handing it out, so a DB restart
                # costs a reconnect instead of a failed request.
                check=AsyncConnectionPool.check_connection,
                open=False,
            )
            await pool.open(wait=False)
            stack.push_async_callback(pool.close)
            source = content
            if source is None:
                # content 호출에는 httpx 계측이 traceparent를 싣는다. OpenAI SDK가 쓰는 httpx2는 계측하지 않아
                # OpenAI 호출에는 싣지 않는다(docker-entrypoint.sh의 OTEL_PYTHON_DISABLED_INSTRUMENTATIONS)
                http = httpx.AsyncClient(
                    base_url=config.content_base_url,
                    timeout=config.content_timeout_seconds,
                )
                stack.push_async_callback(http.aclose)
                source = ContentClient(http)
            client = None
            if model is None or checker is None:
                if config.openai_api_key:
                    client = AsyncOpenAI(api_key=config.openai_api_key, max_retries=2)
                    stack.push_async_callback(client.close)
                else:
                    AppLogger.current().error(
                        "openai api key is not set",
                        LogField.error_type("OPENAI_API_KEY_MISSING"),
                    )
            answers = model
            if answers is None:
                answers = (
                    OpenAIModel(
                        client,
                        config.openai_model,
                        config.openai_reasoning_effort,
                        config.openai_max_output_tokens,
                        config.openai_timeout_seconds,
                        config.judge,
                        config.judge_reasoning_effort,
                        config.judge_max_output_tokens,
                    )
                    if client
                    else UnconfiguredModel(config.openai_model)
                )
            checks_model = checker
            if checks_model is None:
                checks_model = (
                    OpenAICheckModel(
                        client,
                        config.openai_model,
                        config.setting_check_reasoning_effort,
                        config.openai_max_output_tokens,
                        config.judge,
                        config.setting_check_judge_reasoning_effort,
                        config.setting_check_judge_max_output_tokens,
                        config.setting_check_timeout_seconds,
                    )
                    if client
                    else UnconfiguredModel(config.openai_model)
                )
            if keys is None and not config.manuscript_key_id:
                # 요약을 열 키가 없어 지도에서 요약이 빠진다
                AppLogger.current().warn(
                    "manuscript key id is not set",
                    LogField.error_type("MANUSCRIPT_KEY_ID_MISSING"),
                )
            store = Store(
                pool,
                config.stale_turn_seconds,
                keys
                or ProjectKeys(
                    kms_client(config.manuscript_key_region), config.manuscript_key_id
                ),
            )
            runner = TurnRunner(store, source, answers, config)
            check_store = CheckStore(pool, config.setting_check_stale_seconds)
            checks = CheckRunner(check_store, source, checks_model, config)
            app.state.services = Services(config, store, source, runner, checks)
            # 오래 GENERATING·RUNNING으로 남은 행 정리. 실패하면 LogFlow.task가 실패 줄(error, 메시지 없는 스택)을 남기고,
            # 기동은 막지 않는다
            with suppress(Exception), LogFlow.task("stale chat turn expiry") as expiry:
                async with asyncio.timeout(5):
                    expired = await store.expire_stale()
                expiry.result(LogField.result("expired", expired))
            with (
                suppress(Exception),
                LogFlow.task("stale setting check expiry") as expiry,
            ):
                async with asyncio.timeout(5):
                    expired = await check_store.expire_stale()
                expiry.result(LogField.result("expired", expired))
            yield
            await asyncio.gather(runner.drain(), checks.drain())

    app = FastAPI(title=SERVICE, lifespan=lifespan)
    app.add_exception_handler(ChatFailure, handle_failure)
    app.add_exception_handler(HTTPException, handle_http)
    app.add_exception_handler(RequestValidationError, handle_validation)
    app.add_exception_handler(Exception, handle_unexpected)
    # 예상하지 못한 예외를 INTERNAL_ERROR 응답으로 바꾼다. 요청 로그 middleware 안쪽이라 그 이유가 결과 줄에 붙는다
    app.add_middleware(UnexpectedErrorMiddleware)
    # 요청마다 로그 객체를 바인딩하고 request received·request completed를 남긴다. 나중에 더한 middleware가 바깥이고,
    # 아래 계측이 연 서버 span 안쪽에서 돈다
    app.add_middleware(RequestLogMiddleware)
    app.include_router(router)
    app.include_router(check_router)

    @app.get("/")
    def root() -> dict[str, str]:
        AppLogger.current().info("service name requested")
        return {"service": SERVICE}

    @app.get("/health")
    def health() -> dict[str, str]:
        AppLogger.current().info("health check requested")
        return {"status": "ok"}

    @app.get("/health/db")
    def health_db() -> JSONResponse:
        AppLogger.current().info("database health check requested")
        try:
            database = db.check()
        except Exception as exception:
            # 예외 메시지에는 접속 정보가 섞일 수 있어 본문에는 고정 문구만 싣고, 결과 줄에 클래스 이름과 메시지 없는 스택을 맡긴다
            logger = AppLogger.current()
            logger.add_to_outcome(
                LogField.error_type(type(exception).__name__),
                LogField.safe_stack_trace(exception),
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "service": SERVICE,
                    "message": "The database is unreachable",
                    "trace_id": logger.trace_id,
                },
                headers=NO_STORE,
            )
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "service": SERVICE, "database": database},
        )

    # 요청마다 서버 span을 연다. gateway가 보낸 traceparent가 있으면 그 trace를 잇는다.
    # SSE 응답의 조각마다 생기는 send·receive span은 만들지 않는다
    setup_tracing(app)
    return app


app = create_app()
