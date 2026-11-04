import os

import psycopg

CONNECT_TIMEOUT_SECONDS = 3


def connection_info() -> dict[str, str]:
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": os.getenv("DB_PORT", "5432"),
        "dbname": os.getenv("DB_NAME", "ai_chat"),
        "user": os.getenv("DB_USERNAME", "ai_chat_svc"),
        "password": os.getenv("DB_PASSWORD", ""),
    }


def check() -> str:
    """헬스체크용 접속 확인. 접속한 데이터베이스 이름을 돌려준다."""
    info = connection_info()
    with psycopg.connect(
        host=info["host"],
        port=info["port"],
        dbname=info["dbname"],
        user=info["user"],
        password=info["password"],
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute("select current_database()")
            (database,) = cur.fetchone()

    return database
