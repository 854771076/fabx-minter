"""私钥本地加密。

数据库里永远不保存明文私钥。每个账号独立随机 salt 与 nonce，
口令经 scrypt 派生 32 字节密钥后用 AES-256-GCM 加密。
密文布局（单列 BLOB）：version(1) + salt(16) + nonce(12) + ciphertext+tag。

口令仅来自环境变量 FABX_SECRET 或交互输入，不写日志、不落盘。
"""

from __future__ import annotations

import getpass
import os
from hashlib import scrypt

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 1
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32

# scrypt 参数：单次派生在百毫秒级，兼顾抗暴力与交互体验
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
# OpenSSL 默认 maxmem 为 32 MiB，而 128*N*r 已达 32 MiB，必须显式放宽，
# 否则部分平台（如 macOS 系统 Python）会直接抛 "memory limit exceeded"。
SCRYPT_MAXMEM = 128 * SCRYPT_N * SCRYPT_R * 2


class SecretError(RuntimeError):
    """口令缺失或解密失败。"""


def _derive_key(password: str, salt: bytes) -> bytes:
    return scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_LEN,
        maxmem=SCRYPT_MAXMEM,
    )


def encrypt_private_key(private_key_hex: str, password: str) -> bytes:
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(password, salt)
    ciphertext = AESGCM(key).encrypt(nonce, private_key_hex.encode("ascii"), None)
    return bytes([VERSION]) + salt + nonce + ciphertext


def decrypt_private_key(blob: bytes, password: str) -> str:
    if not blob or blob[0] != VERSION:
        raise SecretError("密文版本不受支持")
    # 至少要有 salt + nonce + 16 字节 GCM tag；长度不足说明记录被截断/损坏。
    # 提前拦截，避免把 AESGCM 因参数长度非法抛出的裸 ValueError 泄漏给上层。
    if len(blob) < 1 + SALT_LEN + NONCE_LEN + 16:
        raise SecretError("密文长度非法，记录可能已损坏")
    salt = blob[1 : 1 + SALT_LEN]
    nonce = blob[1 + SALT_LEN : 1 + SALT_LEN + NONCE_LEN]
    ciphertext = blob[1 + SALT_LEN + NONCE_LEN :]
    key = _derive_key(password, salt)
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("ascii")
    except InvalidTag as exc:
        raise SecretError("口令错误或密文被篡改") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        # 理论上会被上面的长度校验拦下；作为兜底统一成本项目的 SecretError
        raise SecretError("密文损坏，无法解密") from exc


def resolve_password(explicit: str | None = None, *, confirm: bool = False) -> str:
    """按 显式参数 -> 环境变量 -> 交互输入 的顺序取口令。"""
    if explicit:
        return explicit
    env = os.environ.get("FABX_SECRET") or os.environ.get("FABX_PASSWORD")
    if env:
        return env
    first = getpass.getpass("请输入私钥加密口令: ")
    if not first:
        raise SecretError("口令不能为空")
    if confirm:
        again = getpass.getpass("请再次输入以确认: ")
        if first != again:
            raise SecretError("两次输入不一致")
    return first
