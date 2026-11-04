import os
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.logs import AppLogger, LogField, LogFlow, skip_entry_log

# 프로젝트 키를 여는 KMS client가 있는 모듈이다. 봉투를 여닫는 함수는 값만 계산하므로 진입 줄을 남기지 않는다
LOG_LAYER = "client"

FORMAT = 1
NONCE_BYTES = 12
TAG_BYTES = 16
MAX_PLAINTEXT_BYTES = 32 * 1024 * 1024
KEY_TTL_SECONDS = 600.0
KEY_CACHE_SIZE = 1000
# unseal이 Unreadable에 싣는 이유 코드. 로그에 그대로 남겨도 되는 고정 낱말이다
UNREADABLE_REASONS = frozenset({"format", "tag", "zlib", "size", "utf-8"})


class Unreadable(ValueError):
    pass


class KeyUnavailable(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@skip_entry_log("Only builds the associated data of an envelope")
def associated_data(project_id: str, subject_id: str, purpose: str) -> bytes:
    return f"lore-sentry:v1:{UUID(project_id)}:{UUID(subject_id)}:{purpose}".encode()


@skip_entry_log("Only checks the envelope header")
def is_envelope(data: bytes) -> bool:
    return len(data) >= 1 + NONCE_BYTES + TAG_BYTES and data[0] == FORMAT


@skip_entry_log("Only encrypts; called for every summary")
def seal(
    key: bytes, project_id: str, subject_id: str, purpose: str, text: str
) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(
        nonce,
        zlib.compress(text.encode("utf-8")),
        associated_data(project_id, subject_id, purpose),
    )
    return bytes([FORMAT]) + nonce + sealed


@skip_entry_log("Only decrypts; called for every summary")
def unseal(
    key: bytes, envelope: bytes, project_id: str, subject_id: str, purpose: str
) -> str:
    if not is_envelope(envelope):
        raise Unreadable("format")
    try:
        compressed = AESGCM(key).decrypt(
            envelope[1 : 1 + NONCE_BYTES],
            envelope[1 + NONCE_BYTES :],
            associated_data(project_id, subject_id, purpose),
        )
    except InvalidTag:
        raise Unreadable("tag") from None
    inflater = zlib.decompressobj()
    try:
        data = inflater.decompress(compressed, MAX_PLAINTEXT_BYTES)
    except zlib.error:
        raise Unreadable("zlib") from None
    if inflater.unconsumed_tail or not inflater.eof:
        raise Unreadable("size")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Unreadable("utf-8") from None


@skip_entry_log("Only builds the boto3 client at startup")
def kms_client(region: str) -> Any:
    return boto3.client(
        "kms",
        region_name=region,
        config=Config(
            connect_timeout=2,
            read_timeout=5,
            retries={"mode": "standard", "max_attempts": 3},
        ),
    )


@skip_entry_log("Only reads the code of a KMS error")
def error_code(error: Exception) -> str:
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code") or "ClientError")
    return type(error).__name__


class ProjectKeys:
    def __init__(
        self,
        kms: Any,
        key_id: str,
        ttl_seconds: float = KEY_TTL_SECONDS,
        size: int = KEY_CACHE_SIZE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.kms = kms
        self.key_id = key_id
        self.ttl_seconds = ttl_seconds
        self.size = size
        self.clock = clock
        self.cache: OrderedDict[tuple[str, bytes], tuple[float, bytes]] = OrderedDict()
        self.lock = threading.Lock()

    def unwrap(self, project_id: str, wrapped_key: bytes) -> bytes:
        AppLogger.current().info(
            "kms project key unwrap call", LogField.project_id(project_id)
        )
        project = str(UUID(project_id))
        entry = (project, bytes(wrapped_key))
        with self.lock:
            cached = self.cache.get(entry)
            if cached is not None and cached[0] > self.clock():
                self.cache.move_to_end(entry)
                return cached[1]
        if not self.key_id:
            raise KeyUnavailable("MANUSCRIPT_KEY_ID")
        # 캐시에 없을 때만 KMS를 부른다. 결과 줄(kms call completed·failed)에 걸린 시간, 실패면 KMS가 준 코드가 남는다
        with LogFlow.call("kms") as call:
            try:
                response = self.kms.decrypt(
                    CiphertextBlob=entry[1],
                    KeyId=self.key_id,
                    EncryptionContext={"project_id": project},
                )
            except (BotoCoreError, ClientError) as error:
                call.fail(type(error).__name__)
                if isinstance(error, ClientError):
                    call.result(LogField.error_code(error_code(error)))
                    status = error.response.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode"
                    )
                    if isinstance(status, int):
                        call.status(status)
                raise KeyUnavailable(error_code(error)) from None
            # 결과 줄보다 먼저 캐시에 넣어, 같은 키를 찾는 다른 스레드가 바로 쓰게 한다
            key = response["Plaintext"]
            with self.lock:
                self.cache[entry] = (self.clock() + self.ttl_seconds, key)
                self.cache.move_to_end(entry)
                while len(self.cache) > self.size:
                    self.cache.popitem(last=False)
        return key
