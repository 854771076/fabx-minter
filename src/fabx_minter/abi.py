"""合约 ABI 注册表。

ABI 来源：fabx.tech 前端打包产物 ``/assets/abi.generated-CtIRDRwc.js``。
该文件用内联对象暴露了 13 套 ABI，逆向提取后原样保存在
``abi/fabx.abi.json``，键名与前端导出的命名一致。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

ABI_DIR = Path(__file__).resolve().parent / "abi"
ABI_FILE = ABI_DIR / "fabx.abi.json"

# 本工具直接使用的 ABI（其余 ABI 保留在 JSON 中供扩展）
KNOWN_ABIS = (
    "accountLensAbi",
    "accountOpenerAbi",
    "circuitAccountAbi",
    "circuitFactoryAbi",
    "circuitMarketAbi",
    "circuitsAbi",
    "fabSiteRegistryAbi",
    "marketplaceAbi",
    "okbBoostAbi",
    "podLensAbi",
    "podMiningAbi",
    "podTokenAbi",
    "transistorsAbi",
)


@lru_cache(maxsize=1)
def load_all() -> dict[str, list[dict[str, Any]]]:
    with ABI_FILE.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    missing = [name for name in KNOWN_ABIS if name not in data]
    if missing:
        raise RuntimeError(f"ABI 文件缺少条目: {missing}")
    return data


def load_abi(name: str) -> list[dict[str, Any]]:
    """按名称取出一套 ABI，名称见 ``KNOWN_ABIS``。"""
    try:
        return load_all()[name]
    except KeyError as exc:  # pragma: no cover - 防御式分支
        raise KeyError(f"未知 ABI: {name}；可选 {sorted(load_all())}") from exc


def function_selectors(name: str) -> dict[str, str]:
    """返回 {signature: 0xselector}，用于快速核对链上调用目标。"""
    from eth_utils import keccak

    out: dict[str, str] = {}
    for item in load_abi(name):
        if item.get("type") != "function":
            continue
        inputs = ",".join(i["type"] for i in item.get("inputs", []))
        signature = f"{item['name']}({inputs})"
        out[signature] = "0x" + keccak(text=signature)[:4].hex()
    return out
