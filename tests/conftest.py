"""测试夹具：每个用例独立的内存/临时数据库，避免相互污染。"""

from __future__ import annotations

import pytest

from fabx_minter.config import Settings
from fabx_minter.db import Database


@pytest.fixture
def settings(tmp_path) -> Settings:
    """测试配置固定指向临时库，并给定口令，避免交互输入。"""
    return Settings(db_path=tmp_path / "test.db", secret="test-secret")


@pytest.fixture
def db(settings: Settings) -> Database:
    return Database(settings.resolved_db_path())
