"""私钥加密解密的行为约束。"""

from __future__ import annotations

import pytest

from fabx_minter.crypto import (
    NONCE_LEN,
    SALT_LEN,
    VERSION,
    SecretError,
    decrypt_private_key,
    encrypt_private_key,
)

PRIVATE_KEY = "11" * 32


def test_roundtrip() -> None:
    blob = encrypt_private_key(PRIVATE_KEY, "correct horse")
    assert decrypt_private_key(blob, "correct horse") == PRIVATE_KEY


def test_blob_layout() -> None:
    blob = encrypt_private_key(PRIVATE_KEY, "pw")
    assert blob[0] == VERSION
    # version + salt + nonce + (密文 64 字节 + 16 字节 GCM tag)
    assert len(blob) == 1 + SALT_LEN + NONCE_LEN + 64 + 16


def test_wrong_password_rejected() -> None:
    blob = encrypt_private_key(PRIVATE_KEY, "pw")
    with pytest.raises(SecretError):
        decrypt_private_key(blob, "pw2")


def test_random_salt_makes_ciphertext_unique() -> None:
    assert encrypt_private_key(PRIVATE_KEY, "pw") != encrypt_private_key(PRIVATE_KEY, "pw")


def test_bad_version_rejected() -> None:
    with pytest.raises(SecretError):
        decrypt_private_key(b"\x09" + b"\x00" * 40, "pw")


def test_empty_blob_rejected() -> None:
    # 空记录直接拒绝，避免越界读取版本号
    with pytest.raises(SecretError):
        decrypt_private_key(b"", "pw")


def test_truncated_blob_rejected() -> None:
    # 只保留版本号、salt、nonce 而丢掉 GCM tag，属于被截断的损坏记录
    blob = encrypt_private_key(PRIVATE_KEY, "pw")
    truncated = blob[: 1 + SALT_LEN + NONCE_LEN]
    with pytest.raises(SecretError):
        decrypt_private_key(truncated, "pw")


def test_corrupted_tag_rejected_as_secret_error() -> None:
    # 篡改 GCM tag 后必须归一为 SecretError，而不是向上层泄漏裸异常
    blob = bytearray(encrypt_private_key(PRIVATE_KEY, "pw"))
    blob[-1] ^= 0x01
    with pytest.raises(SecretError):
        decrypt_private_key(bytes(blob), "pw")
