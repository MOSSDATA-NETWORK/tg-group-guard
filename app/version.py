"""应用版本信息。

APP_VERSION 随每次发布递增；UPDATE_GITHUB_REPO 是定时比对的目标仓库，
也可通过环境变量 GITHUB_REPO 覆盖（便于 fork 后比对自己的仓库）。
"""
from __future__ import annotations

import os

APP_VERSION = "1.0.3"

_DEFAULT_REPO = "MOSSDATA-NETWORK/tg-group-guard"


def github_repo() -> str:
    repo = os.getenv("GITHUB_REPO", "").strip()
    return repo or _DEFAULT_REPO
