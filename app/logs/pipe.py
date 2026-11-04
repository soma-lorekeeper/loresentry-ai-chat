"""다른 프로그램의 출력 줄을 ECS JSON 한 줄씩으로 바꾼다: ``python -m app.logs.pipe <logger> <service>``.

API 파드는 uvicorn보다 먼저 ``flyway migrate``를 돌린다(docker-entrypoint.sh). 그 평문 줄은 ES에서 log.level이 unknown이
되므로, 진입점이 flyway의 stdout·stderr를 이 모듈로 넘긴다. 줄마다 서비스와 같은 formatter(``configure_logging``)를 거쳐
logger 이름 ``<logger>``의 줄 하나가 된다.

- 레벨: "ERROR"로 시작하면 ERROR, "WARNING"으로 시작하면 WARN, 그 밖은 INFO
- message: 받은 줄 그대로(끝의 줄바꿈만 뺀다). 빈 줄은 버린다
- 종료 코드는 늘 0이다. 넘겨준 프로그램의 종료 코드는 셸(pipefail)이 지킨다

flyway 줄에는 비밀 값이 없다. 접속 URL(FLYWAY_URL)에 비밀번호를 넣지 않고 비밀번호는 환경 변수(FLYWAY_PASSWORD)로만 넘긴다.
"""

import logging
import sys
from collections.abc import Iterable

from app.logs._format import configure_logging

# 줄 앞머리 → 레벨. 위에서부터 맞춰 본다(flyway는 "ERROR: …", "WARNING: …"로 찍는다)
PREFIXES = (("ERROR", logging.ERROR), ("WARNING", logging.WARNING))


def level_of(line: str) -> int:
    """줄의 레벨. 정해 둔 앞머리가 없으면 INFO."""
    for prefix, level in PREFIXES:
        if line.startswith(prefix):
            return level
    return logging.INFO


def forward(name: str, lines: Iterable[str]) -> None:
    """줄마다 logger ``name``의 줄 하나를 남긴다. root의 ECS JSON handler가 표준 출력에 쓴다."""
    logger = logging.getLogger(name)
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line.strip():
            # 줄을 인자로 넘긴다. 줄 안의 %가 서식 문자로 읽히지 않는다
            logger.log(level_of(line), "%s", line)


def main(arguments: list[str]) -> None:
    """표준 입력이 끝날 때까지 줄마다 ECS JSON 한 줄을 표준 출력에 쓴다."""
    name, service = arguments
    configure_logging(service)
    # 깨진 바이트가 와도 줄을 버리지 않는다
    sys.stdin.reconfigure(errors="replace")
    forward(name, sys.stdin)


if __name__ == "__main__":
    main(sys.argv[1:])
