import json
import os
import zlib
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import manuscript
from app.manuscript import (
    KeyUnavailable,
    ProjectKeys,
    Unreadable,
    associated_data,
    seal,
    unseal,
)
from tests.support import FakeKms

VECTORS = json.loads(
    (Path(__file__).parent / "manuscript-envelope-vectors.json").read_text()
)
KEY = bytes.fromhex(VECTORS["key_hex"])


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["purpose"])
def test_vectors_open_to_their_plaintext(vector):
    envelope = bytes.fromhex(vector["envelope_hex"])

    assert (
        unseal(
            KEY,
            envelope,
            vector["project_id"],
            vector["subject_id"],
            vector["purpose"],
        )
        == vector["plaintext"]
    )


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["purpose"])
def test_vectors_do_not_open_under_another_purpose_or_subject(vector):
    envelope = bytes.fromhex(vector["envelope_hex"])
    other = "body_json" if vector["purpose"] != "body_json" else "body_text"

    with pytest.raises(Unreadable):
        unseal(KEY, envelope, vector["project_id"], vector["subject_id"], other)
    with pytest.raises(Unreadable):
        unseal(KEY, envelope, vector["project_id"], str(uuid4()), vector["purpose"])
    with pytest.raises(Unreadable):
        unseal(
            bytes(32),
            envelope,
            vector["project_id"],
            vector["subject_id"],
            vector["purpose"],
        )


def test_vector_nonce_reproduces_the_envelope():
    vector = VECTORS["vectors"][0]
    envelope = bytes.fromhex(vector["envelope_hex"])
    nonce = envelope[1:13]
    sealed = AESGCM(KEY).encrypt(
        nonce,
        zlib.compress(vector["plaintext"].encode("utf-8")),
        associated_data(vector["project_id"], vector["subject_id"], "body_text"),
    )

    assert bytes([1]) + nonce + sealed == envelope


def test_seal_round_trips_with_a_fresh_nonce():
    project, document = str(uuid4()), str(uuid4())
    key = os.urandom(32)
    text = "레나는 북쪽 문 앞에 섰다. 😀\n" * 100

    first = seal(key, project, document, "body_text", text)
    second = seal(key, project, document, "body_text", text)

    assert first[0] == 1
    assert first[1:13] != second[1:13]
    assert unseal(key, first, project, document, "body_text") == text
    assert unseal(key, second, project.upper(), document, "body_text") == text


def test_damaged_envelopes_are_unreadable():
    project, document = str(uuid4()), str(uuid4())
    key = os.urandom(32)
    envelope = seal(key, project, document, "body_text", "본문")

    for damaged in (
        b"",
        b'{"body_text": "x"}',
        bytes([2]) + envelope[1:],
        envelope[:-1],
        envelope[:13] + bytes([envelope[13] ^ 1]) + envelope[14:],
    ):
        with pytest.raises(Unreadable):
            unseal(key, damaged, project, document, "body_text")


def test_plaintext_over_the_cap_is_rejected(monkeypatch):
    project, document = str(uuid4()), str(uuid4())
    key = os.urandom(32)
    envelope = seal(key, project, document, "body_text", "가" * 100)
    monkeypatch.setattr(manuscript, "MAX_PLAINTEXT_BYTES", 299)

    with pytest.raises(Unreadable):
        unseal(key, envelope, project, document, "body_text")
    monkeypatch.setattr(manuscript, "MAX_PLAINTEXT_BYTES", 300)
    assert unseal(key, envelope, project, document, "body_text") == "가" * 100


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_keys_are_unwrapped_with_the_project_context_and_cached():
    kms, clock = FakeKms(), Clock()
    project = str(uuid4())
    wrapped = kms.wrap(project, KEY)
    keys = ProjectKeys(kms, kms.key_id, clock=clock)

    assert keys.unwrap(project.upper(), wrapped) == KEY
    assert keys.unwrap(project, wrapped) == KEY
    assert kms.calls == [
        {
            "CiphertextBlob": wrapped,
            "KeyId": kms.key_id,
            "EncryptionContext": {"project_id": project},
        }
    ]

    clock.now = 599
    keys.unwrap(project, wrapped)
    assert len(kms.calls) == 1
    clock.now = 601
    keys.unwrap(project, wrapped)
    assert len(kms.calls) == 2


def test_the_key_cache_is_bounded():
    kms = FakeKms()
    keys = ProjectKeys(kms, kms.key_id, size=2)
    projects = [str(uuid4()) for _ in range(3)]
    wrapped = [kms.wrap(project, os.urandom(32)) for project in projects]

    for project, blob in zip(projects, wrapped, strict=True):
        keys.unwrap(project, blob)
    keys.unwrap(projects[2], wrapped[2])
    assert len(kms.calls) == 3
    keys.unwrap(projects[0], wrapped[0])
    assert len(kms.calls) == 4
    assert len(keys.cache) == 2


def test_kms_failures_are_key_unavailable():
    kms = FakeKms()
    project = str(uuid4())
    wrapped = kms.wrap(project, KEY)

    with pytest.raises(KeyUnavailable) as other_project:
        ProjectKeys(kms, kms.key_id).unwrap(str(uuid4()), wrapped)
    assert other_project.value.code == "InvalidCiphertextException"

    kms.fail_with = "KMSInternalException"
    with pytest.raises(KeyUnavailable) as outage:
        ProjectKeys(kms, kms.key_id).unwrap(project, wrapped)
    assert outage.value.code == "KMSInternalException"

    calls = len(kms.calls)
    with pytest.raises(KeyUnavailable) as unset:
        ProjectKeys(kms, "").unwrap(project, wrapped)
    assert unset.value.code == "MANUSCRIPT_KEY_ID"
    assert len(kms.calls) == calls
