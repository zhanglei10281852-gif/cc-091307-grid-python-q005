"""JSON 文件持久化：原子写入，计数在加载时由事件重算。"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any


class JsonStore:
    """单文件原子存储。

    计数（counters）不单独持久化——每次加载都由事件记录重算，
    因此异常掉电后状态与计数不会漂移。
    """

    def __init__(self, path: str):
        self.path = path
        self._data: dict[str, Any] = {"seq": 0, "events": {}}
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        else:
            self._data = {"seq": 0, "events": {}}
        self._data.setdefault("seq", 0)
        self._data.setdefault("events", {})

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".orderdb-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    @property
    def seq(self) -> int:
        return self._data["seq"]

    def next_seq(self) -> int:
        self._data["seq"] += 1
        return self._data["seq"]

    @property
    def events_raw(self) -> dict[str, Any]:
        return self._data["events"]
