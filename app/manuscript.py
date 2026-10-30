import os
import threading
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable
from typing import Any
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = 1
NONCE_BYTES = 12
TAG_BYTES = 16
MAX_PLAINTEXT_BYTES = 32 * 1024 * 1024
KEY_TTL_SECONDS = 600.0
KEY_CACHE_SIZE = 1000


class Unreadable(ValueError):
    pass


class KeyUnavailable(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def associated_data(project_id: str, subject_id: str, purpose: str) -> bytes:
    return f"lore-sentry:v1:{UUID(project_id)}:{UUID(subject_id)}:{purpose}".encode()


def is_envelope(data: bytes) -> bool:
    return len(data) >= 1 + NONCE_BYTES + TAG_BYTES and data[0] == FORMAT


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
        project = str(UUID(project_id))
        entry = (project, bytes(wrapped_key))
        with self.lock:
            cached = self.cache.get(entry)
            if cached is not None and cached[0] > self.clock():
                self.cache.move_to_end(entry)
                return cached[1]
        if not self.key_id:
            raise KeyUnavailable("MANUSCRIPT_KEY_ID")
        try:
            response = self.kms.decrypt(
                CiphertextBlob=entry[1],
                KeyId=self.key_id,
                EncryptionContext={"project_id": project},
            )
        except (BotoCoreError, ClientError) as error:
            raise KeyUnavailable(error_code(error)) from None
        key = response["Plaintext"]
        with self.lock:
            self.cache[entry] = (self.clock() + self.ttl_seconds, key)
            self.cache.move_to_end(entry)
            while len(self.cache) > self.size:
                self.cache.popitem(last=False)
        return key
