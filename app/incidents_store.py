"""故障改派事件库：同一故障号的重试只返回首次落库的改派账。

与规划归档分表存放。incident_id 为主键，INSERT OR IGNORE 保证并发重试
也只会建立一份改派账；再次提交直接回读原记录，绝不重算、绝不二次扣减。
"""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.manifest import canonical, sha256
from app.modules.m8_incidents.rules import run as build_ledger


class IncidentStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("""CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            input_digest TEXT NOT NULL,
            payload TEXT NOT NULL
        )""")
        return connection

    def get(self, incident_id):
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def submit(self, data, incident):
        """提交一次故障申报。返回 (记录, 是否复用了既有改派账)。

        先查快速通道；未命中时仍以 INSERT OR IGNORE 的实际插入行数判定
        首次/复用，避免两个并发线程都把自己当成首次而重复计算与扣减。
        """
        incident_id = incident["incident"]
        digest = sha256([incident_id, data])
        existing = self.get(incident_id)
        if existing is not None:
            if existing["input_digest"] != digest:
                # 同一故障号换了底账再来：拒绝，防止覆盖首次改派导致重复扣减。
                from app.errors import InputError

                raise InputError("INCIDENT_CONFLICT", "该故障号已生成改派账，底账不一致，禁止覆盖")
            return existing, True
        ledger = build_ledger(data, incident)
        record = {
            "id": incident_id,
            "input_digest": digest,
            "incident": incident,
            "ledger": ledger,
        }
        with closing(self.connect()) as connection, connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO incidents VALUES (?, ?, ?)",
                (incident_id, digest, canonical(record)),
            ).rowcount
        # 插入 0 行说明并发线程已抢先落库：回读首条记录，本次视为复用。
        stored = self.get(incident_id)
        if not inserted and stored["input_digest"] != digest:
            from app.errors import InputError

            raise InputError("INCIDENT_CONFLICT", "该故障号已生成改派账，底账不一致，禁止覆盖")
        return stored, not bool(inserted)
