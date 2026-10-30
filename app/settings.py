import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


@dataclass(frozen=True)
class Settings:
    db_host: str = "localhost"
    db_port: str = "5432"
    db_name: str = "ai_chat"
    db_username: str = "ai_chat_svc"
    db_password: str = ""
    db_pool_size: int = 10
    content_base_url: str = "http://localhost:8001"
    content_timeout_seconds: float = 30.0
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    openai_reasoning_effort: str = "medium"
    openai_max_output_tokens: int = 32000
    openai_timeout_seconds: float = 120.0
    turn_deadline_seconds: float = 170.0
    step_max_chars: int = 40_000
    max_steps: int = 4
    held_max_chars: int = 200_000
    history_max_chars: int = 40_000
    answer_reserve_seconds: float = 75.0
    judge_model: str = ""
    judge_reasoning_effort: str = "low"
    judge_max_output_tokens: int = 2000
    heartbeat_seconds: float = 15.0
    cancel_poll_seconds: float = 2.0
    stale_turn_seconds: int = 300
    daily_credit_limit: int = 3_500_000
    shutdown_grace_seconds: float = 15.0
    setting_check_max_chars: int = 600_000
    setting_check_reasoning_effort: str = "medium"
    setting_check_judge_reasoning_effort: str = "low"
    setting_check_judge_max_output_tokens: int = 4000
    setting_check_judge_concurrency: int = 4
    setting_check_timeout_seconds: float = 300.0
    setting_check_deadline_seconds: float = 600.0
    setting_check_stale_seconds: int = 900
    manuscript_key_id: str = ""
    manuscript_key_region: str = "ap-northeast-2"

    @property
    def judge(self) -> str:
        return self.judge_model or self.openai_model

    @property
    def conninfo(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_username} password={self.db_password} connect_timeout=3"
        )

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            db_host=os.getenv("DB_HOST", cls.db_host),
            db_port=os.getenv("DB_PORT", cls.db_port),
            db_name=os.getenv("DB_NAME", cls.db_name),
            db_username=os.getenv("DB_USERNAME", cls.db_username),
            db_password=os.getenv("DB_PASSWORD", cls.db_password),
            db_pool_size=_int("DB_POOL_SIZE", cls.db_pool_size),
            content_base_url=os.getenv("CONTENT_BASE_URL", cls.content_base_url),
            content_timeout_seconds=_float(
                "CONTENT_TIMEOUT_SECONDS", cls.content_timeout_seconds
            ),
            openai_api_key=os.getenv("OPENAI_API_KEY", cls.openai_api_key),
            openai_model=os.getenv("OPENAI_MODEL", cls.openai_model),
            openai_reasoning_effort=os.getenv(
                "OPENAI_REASONING_EFFORT", cls.openai_reasoning_effort
            ),
            openai_max_output_tokens=_int(
                "OPENAI_MAX_OUTPUT_TOKENS", cls.openai_max_output_tokens
            ),
            openai_timeout_seconds=_float(
                "OPENAI_TIMEOUT_SECONDS", cls.openai_timeout_seconds
            ),
            turn_deadline_seconds=_float(
                "CHAT_TURN_DEADLINE_SECONDS", cls.turn_deadline_seconds
            ),
            step_max_chars=_int("CHAT_STEP_MAX_CHARS", cls.step_max_chars),
            max_steps=_int("CHAT_MAX_STEPS", cls.max_steps),
            held_max_chars=_int("CHAT_HELD_MAX_CHARS", cls.held_max_chars),
            history_max_chars=_int("CHAT_HISTORY_MAX_CHARS", cls.history_max_chars),
            answer_reserve_seconds=_float(
                "CHAT_ANSWER_RESERVE_SECONDS", cls.answer_reserve_seconds
            ),
            judge_model=os.getenv("CHAT_JUDGE_MODEL", cls.judge_model),
            judge_reasoning_effort=os.getenv(
                "CHAT_JUDGE_REASONING_EFFORT", cls.judge_reasoning_effort
            ),
            judge_max_output_tokens=_int(
                "CHAT_JUDGE_MAX_OUTPUT_TOKENS", cls.judge_max_output_tokens
            ),
            heartbeat_seconds=_float("CHAT_HEARTBEAT_SECONDS", cls.heartbeat_seconds),
            cancel_poll_seconds=_float(
                "CHAT_CANCEL_POLL_SECONDS", cls.cancel_poll_seconds
            ),
            stale_turn_seconds=_int("CHAT_STALE_TURN_SECONDS", cls.stale_turn_seconds),
            daily_credit_limit=_int("CHAT_DAILY_CREDIT_LIMIT", cls.daily_credit_limit),
            shutdown_grace_seconds=_float(
                "CHAT_SHUTDOWN_GRACE_SECONDS", cls.shutdown_grace_seconds
            ),
            setting_check_max_chars=_int(
                "SETTING_CHECK_MAX_CHARS", cls.setting_check_max_chars
            ),
            setting_check_reasoning_effort=os.getenv(
                "SETTING_CHECK_REASONING_EFFORT", cls.setting_check_reasoning_effort
            ),
            setting_check_judge_reasoning_effort=os.getenv(
                "SETTING_CHECK_JUDGE_REASONING_EFFORT",
                cls.setting_check_judge_reasoning_effort,
            ),
            setting_check_judge_max_output_tokens=_int(
                "SETTING_CHECK_JUDGE_MAX_OUTPUT_TOKENS",
                cls.setting_check_judge_max_output_tokens,
            ),
            setting_check_judge_concurrency=_int(
                "SETTING_CHECK_JUDGE_CONCURRENCY", cls.setting_check_judge_concurrency
            ),
            setting_check_timeout_seconds=_float(
                "SETTING_CHECK_TIMEOUT_SECONDS", cls.setting_check_timeout_seconds
            ),
            setting_check_deadline_seconds=_float(
                "SETTING_CHECK_DEADLINE_SECONDS", cls.setting_check_deadline_seconds
            ),
            setting_check_stale_seconds=_int(
                "SETTING_CHECK_STALE_SECONDS", cls.setting_check_stale_seconds
            ),
            manuscript_key_id=os.getenv("MANUSCRIPT_KEY_ID", cls.manuscript_key_id),
            manuscript_key_region=os.getenv(
                "MANUSCRIPT_KEY_REGION", cls.manuscript_key_region
            ),
        )
