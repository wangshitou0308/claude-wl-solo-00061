"""SQLite 持久层。

所有表只追加/更新，不物理删除：撤回与更正都保留历史事件，可审计。
时间一律 UTC ISO 字符串。
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

DEFAULT_DB = os.environ.get("NIANHUA_DB", os.path.join(os.getcwd(), "nianhua.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS batch (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL,
    frozen_at   TEXT,
    freeze_hash TEXT
);

-- 交出方 / 接收方，各批次每方至多一行
CREATE TABLE IF NOT EXISTS party (
    batch_id   TEXT NOT NULL REFERENCES batch(id),
    side       TEXT NOT NULL CHECK(side IN ('handover','receive')),
    name       TEXT NOT NULL,
    contact    TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, side)
);

CREATE TABLE IF NOT EXISTS witness (
    id         TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL REFERENCES batch(id),
    name       TEXT NOT NULL,
    contact    TEXT,
    relation   TEXT,
    created_at TEXT NOT NULL
);

-- 条目版本：同一 (batch, side, code) 的每次登记/更正是一行新版本，
-- active=1 的最新版本参与核对；旧版本保留留痕。
CREATE TABLE IF NOT EXISTS item (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES batch(id),
    side        TEXT NOT NULL,
    code        TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,          -- 原始 JSON 声明（题名原样保留）
    payload_hash TEXT NOT NULL,
    superseded_by TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (batch_id, side, code, version)
);

-- 一方当前提交的条目集合（清点清单头）：登记当前应视为在册的条目 id。
-- 更正走新 item 版本 + 把本表里的 item_id 换掉；撤回清点确认时只动 confirmation。
CREATE TABLE IF NOT EXISTS inventory (
    batch_id TEXT NOT NULL,
    side     TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    PRIMARY KEY (batch_id, side, seq)
);

-- 本方套组标签 -> 本方条目（以 inventory 活动条目的 item_id 记录组成）。
-- 组成随更正而修订；修订历史由 item 版本与 event 日志承担。
CREATE TABLE IF NOT EXISTS set_member (
    batch_id  TEXT NOT NULL,
    side      TEXT NOT NULL,
    set_label TEXT NOT NULL,
    item_id   TEXT NOT NULL,
    PRIMARY KEY (batch_id, side, set_label, item_id)
);

CREATE TABLE IF NOT EXISTS observation (
    id         TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL REFERENCES batch(id),
    witness_id TEXT NOT NULL REFERENCES witness(id),
    target_kind TEXT NOT NULL,           -- item / set
    target_code TEXT NOT NULL,
    side_hint  TEXT,
    payload    TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 双方各自对每条见证的处置；双方 adopt 才并入证据
CREATE TABLE IF NOT EXISTS observation_decision (
    observation_id TEXT NOT NULL REFERENCES observation(id),
    side           TEXT NOT NULL,
    adopted        INTEGER NOT NULL,
    note           TEXT,
    decided_at     TEXT NOT NULL,
    PRIMARY KEY (observation_id, side)
);

-- 三阶段确认；同一 (batch, side, stage, scope) 仅一行有效（撤回=行删除并记事件）。
-- scope: inventory 阶段固定 '*'；grouping 阶段为 '*'；status 阶段为套组标签。
CREATE TABLE IF NOT EXISTS confirmation (
    batch_id   TEXT NOT NULL,
    side       TEXT NOT NULL,
    stage      TEXT NOT NULL,
    scope      TEXT NOT NULL DEFAULT '*',
    basis_hash TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, side, stage, scope)
);

CREATE TABLE IF NOT EXISTS event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id  TEXT NOT NULL,
    at        TEXT NOT NULL,
    actor     TEXT NOT NULL,             -- handover/receive/witness:<id>/system
    kind      TEXT NOT NULL,             -- 事件类型
    detail    TEXT NOT NULL              -- JSON
);

-- 冻结时写一份完整快照（含输入哈希、分歧处置、责任变化）
CREATE TABLE IF NOT EXISTS freeze_record (
    batch_id   TEXT PRIMARY KEY REFERENCES batch(id),
    snapshot   TEXT NOT NULL,
    hash       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DEFAULT_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def session(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    with session(db_path) as conn:
        conn.executescript(SCHEMA)
