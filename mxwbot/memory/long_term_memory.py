"""长期记忆 — SQLite + FTS5 持久化存储。

v1 使用 FTS5 全文检索（无需 embedding 模型）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_fts5_special(query: str) -> bool:
    """判断查询是否包含 FTS5 语法特殊字符。

    Args:
        query: 搜索关键词。

    Returns:
        True 表示需要回退到 LIKE 搜索。
    """
    return any(c in query for c in ('"', "'", "(", ")", "AND", "OR", "NOT", "-"))


# ---------------------------------------------------------------------------
# SQL DDL
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    category TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    content=memories,
    content_rowid=rowid
);

-- 保持 FTS 索引与主表同步
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.rowid, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""


class LongTermMemory:
    """基于 SQLite + FTS5 的持久化长期记忆存储。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path

    async def init_db(self) -> None:
        """初始化数据库表结构。幂等——可重复调用。"""
        async with aiosqlite.connect(str(self._db_path)) as db:
            await db.executescript(DDL)
            await db.commit()

    # -- 写入 ---------------------------------------------------------------

    async def add(self, entry: dict[str, Any]) -> str:
        """插入一条记忆。

        Args:
            entry: 记忆字典，含 content、category、importance 等字段。

        Returns:
            分配的记忆 id。
        """
        if "id" not in entry:
            entry["id"] = _new_id()
        if "created_at" not in entry:
            entry["created_at"] = _utc_now()

        async with aiosqlite.connect(str(self._db_path)) as db:
            await db.execute(
                "INSERT INTO memories(id, content, category, importance, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    entry["id"],
                    str(entry.get("content", "")),
                    str(entry.get("category", "")),
                    float(entry.get("importance", 0.5)),
                    str(entry.get("created_at", "")),
                    json.dumps(entry.get("metadata", {}), ensure_ascii=False),
                ),
            )
            await db.commit()
        return str(entry["id"])

    async def add_batch(self, entries: list[dict[str, Any]]) -> list[str]:
        """批量插入记忆，单事务提交。

        Args:
            entries: 记忆字典列表。

        Returns:
            分配的记忆 id 列表。
        """
        ids: list[str] = []
        async with aiosqlite.connect(str(self._db_path)) as db:
            for entry in entries:
                eid = entry.get("id") or _new_id()
                ids.append(eid)
                await db.execute(
                    "INSERT INTO memories(id, content, category, importance, created_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        eid,
                        str(entry.get("content", "")),
                        str(entry.get("category", "")),
                        float(entry.get("importance", 0.5)),
                        entry.get("created_at") or _utc_now(),
                        json.dumps(entry.get("metadata", {}), ensure_ascii=False),
                    ),
                )
            await db.commit()
        return ids

    # -- 查询 ---------------------------------------------------------------

    async def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """通过 FTS5 全文检索搜索记忆。

        查询中包含 FTS5 特殊字符时自动回退到 LIKE 搜索。

        Args:
            query: 搜索关键词。
            k: 返回条数上限。

        Returns:
            匹配的记忆列表，按 importance 降序。
        """
        safe = query.replace("*", "").replace('"', "").strip()
        if not safe or _has_fts5_special(query):
            return await self._search_fallback(query, k)

        async with aiosqlite.connect(str(self._db_path)) as db:
            try:
                rows = await db.execute_fetchall(
                    "SELECT id, content, category, importance, created_at, metadata "
                    "FROM memories WHERE rowid IN (SELECT rowid FROM memories_fts WHERE content MATCH ?) "
                    "ORDER BY importance DESC LIMIT ?",
                    (safe, k),
                )
            except sqlite3.OperationalError:
                return await self._search_fallback(query, k)

        return [self._row_to_dict(r) for r in rows]

    async def _search_fallback(self, query: str, k: int) -> list[dict[str, Any]]:
        """LIKE 模糊搜索（FTS5 不可用时的回退方案）。

        Args:
            query: 搜索关键词。
            k: 返回条数上限。

        Returns:
            匹配的记忆列表。
        """
        async with aiosqlite.connect(str(self._db_path)) as db:
            rows = await db.execute_fetchall(
                "SELECT id, content, category, importance, created_at, metadata "
                "FROM memories WHERE content LIKE ? ORDER BY importance DESC LIMIT ?",
                (f"%{query}%", k),
            )
        return [self._row_to_dict(r) for r in rows]

    async def get_recent(self, n: int = 10) -> list[dict[str, Any]]:
        """获取最近存储的 N 条记忆。

        Args:
            n: 返回条数。

        Returns:
            按创建时间降序的记忆列表。
        """
        async with aiosqlite.connect(str(self._db_path)) as db:
            rows = await db.execute_fetchall(
                "SELECT id, content, category, importance, created_at, metadata "
                "FROM memories ORDER BY created_at DESC LIMIT ?",
                (n,),
            )
        return [self._row_to_dict(r) for r in rows]

    async def get_by_category(self, category: str, k: int = 10) -> list[dict[str, Any]]:
        """按分类标签筛选记忆。

        Args:
            category: 分类标签。
            k: 返回条数上限。

        Returns:
            匹配分类的记忆列表。
        """
        async with aiosqlite.connect(str(self._db_path)) as db:
            rows = await db.execute_fetchall(
                "SELECT id, content, category, importance, created_at, metadata "
                "FROM memories WHERE category = ? ORDER BY importance DESC LIMIT ?",
                (category, k),
            )
        return [self._row_to_dict(r) for r in rows]

    # -- 删除 ---------------------------------------------------------------

    async def context_for_query(self, query: str | None, k: int = 5) -> str:
        """生成可注入 LLM 上下文的记忆文本块。

        Args:
            query: 搜索关键词，None 或空字符串时返回最近记忆。
            k: 返回条数上限。

        Returns:
            格式化的记忆上下文文本，无结果时返回空字符串。
        """
        if query:
            results = await self.search(query, k=k)
        else:
            results = await self.get_recent(n=k)

        if not results:
            return ""

        lines = ["相关记忆:"]
        for r in results:
            lines.append(f"- [{r.get('category', '')}] {r['content']}")
        return "\n".join(lines)

    async def delete(self, memory_id: str) -> bool:
        """删除指定 id 的记忆。

        Args:
            memory_id: 记忆 id。

        Returns:
            True 表示删除成功，False 表示 id 不存在。
        """
        async with aiosqlite.connect(str(self._db_path)) as db:
            cursor = await db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            await db.commit()
            return cursor.rowcount > 0

    # -- 内部工具 -----------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        """将数据库行转换为字典。

        Args:
            row: 数据库查询结果行。

        Returns:
            标准化的记忆字典。
        """
        return {
            "id": row[0],
            "content": row[1],
            "category": row[2],
            "importance": row[3],
            "created_at": row[4],
            "metadata": json.loads(row[5]) if row[5] else {},
        }
