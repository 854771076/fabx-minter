"""ABI 提取结果的完整性校验（逆向来源：前端 abi.generated-*.js）。"""

from __future__ import annotations

from eth_utils import keccak

from fabx_minter import abi


def test_all_known_abis_present() -> None:
    data = abi.load_all()
    missing = [name for name in abi.KNOWN_ABIS if name not in data]
    assert missing == []
    assert len(data) == 13


def test_transistors_key_functions() -> None:
    names = {
        item["name"]
        for item in abi.load_abi("transistorsAbi")
        if item.get("type") == "function"
    }
    # mint 是批量铸造入口；NAND/LATCH 提供 token id；mintPrice 决定应付金额
    assert {"mint", "mintPrice", "minted", "mintedOf", "typeCap", "NAND", "LATCH"} <= names


def test_mint_function_shape() -> None:
    mint = next(
        item
        for item in abi.load_abi("transistorsAbi")
        if item.get("type") == "function" and item.get("name") == "mint"
    )
    assert [i["type"] for i in mint["inputs"]] == ["uint256", "uint256"]
    assert mint["stateMutability"] == "payable"


def test_function_selectors() -> None:
    selectors = abi.function_selectors("transistorsAbi")
    expected = "0x" + keccak(text="mint(uint256,uint256)")[:4].hex()
    assert selectors["mint(uint256,uint256)"] == expected
    # NAND/LATCH 无参，选择器稳定可复现
    assert selectors["NAND()"] == "0x" + keccak(text="NAND()")[:4].hex()
