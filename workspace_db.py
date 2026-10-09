"""Local SQLite workspace: byte imports, evidence, review-only facts and audit.

Only the Python standard library is required. PDF/OCR support is optional in
``evidence_engine``. Nothing here accesses the network. New facts are always
pending; only ``review_fact`` can accept or reject them, with an audit trail.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import evidence_engine

MAX_SOURCE_BYTES = evidence_engine.MAX_SOURCE_BYTES
FACT_STATUSES = frozenset({"pending", "accepted", "rejected"})
REVIEW_STATUSES = FACT_STATUSES
FACT_REQUIRED_FIELDS = (*evidence_engine.FACT_COLUMNS, "evidence_id")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _text(value: Any, field: str, *, required: bool = True, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    value = value.strip()
    if (required and not value) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field} 不能为空、包含 NUL 或超过 {maximum} 字符")
    return value


def _normalise_filename(filename: str) -> str:
    filename = _text(filename, "filename", maximum=4096)
    # Treat Windows separators as separators even on macOS/Linux. The result
    # is display metadata only; it is never used as a destination path.
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or len(name) > 255 or any(ord(char) < 32 for char in name):
        raise ValueError("filename 无效或过长")
    if Path(name).suffix.lower() not in evidence_engine.SUPPORTED_EXTENSIONS:
        raise ValueError("仅支持 PDF、CSV、JSON、Markdown 和 TXT")
    return name


class WorkspaceDB:
    """Thread-safe facade over ``state_dir/workspace.sqlite3``.

    Each mutation and its audit rows share one transaction. SQLite serializes
    writers across instances too. State directories are trusted application
    configuration, never an upload/API field.
    """

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.storage_dir = self.state_dir / "sources"
        if self.storage_dir.is_symlink():
            raise ValueError("来源存储目录不得为符号链接")
        self.storage_dir.mkdir(exist_ok=True)
        self.db_path = self.state_dir / "workspace.sqlite3"
        if self.db_path.is_symlink():
            raise ValueError("数据库文件不得为符号链接")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "WorkspaceDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, source_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL UNIQUE, size_bytes INTEGER NOT NULL
                        CHECK (size_bytes >= 0 AND size_bytes <= 12582912),
                    storage_path TEXT NOT NULL, company_id TEXT NOT NULL DEFAULT '',
                    parse_status TEXT NOT NULL, ocr_requested INTEGER NOT NULL DEFAULT 0,
                    warnings_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
                    locator_type TEXT NOT NULL, locator_value TEXT NOT NULL, page_number INTEGER,
                    quote TEXT NOT NULL, normalized_text TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS evidence_source_idx ON evidence(source_id);
                CREATE TABLE IF NOT EXISTS facts (
                    id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL CHECK (length(trim(company_id)) > 0),
                    metric_key TEXT NOT NULL CHECK (length(trim(metric_key)) > 0),
                    value REAL NOT NULL CHECK (value BETWEEN -1.7976931348623157e308 AND 1.7976931348623157e308),
                    unit TEXT NOT NULL CHECK (length(trim(unit)) > 0),
                    currency TEXT NOT NULL CHECK (length(trim(currency)) > 0),
                    period_start TEXT NOT NULL CHECK (length(trim(period_start)) > 0),
                    period_end TEXT NOT NULL CHECK (length(trim(period_end)) > 0),
                    period_type TEXT NOT NULL CHECK (length(trim(period_type)) > 0),
                    accounting_basis TEXT NOT NULL CHECK (length(trim(accounting_basis)) > 0),
                    scope TEXT NOT NULL CHECK (length(trim(scope)) > 0),
                    evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE RESTRICT,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected')),
                    review_reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS facts_lookup_idx
                    ON facts(company_id, metric_key, period_start, period_end, scope);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL, action TEXT NOT NULL, before_json TEXT,
                    after_json TEXT, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_entity_idx ON audit(entity_type, entity_id);
            """)
            try:
                self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
                    evidence_id UNINDEXED, source_id UNINDEXED, normalized_text
                )""")
                # Recover the optional index when a DB created without FTS5 is
                # later opened by a Python build with FTS5.
                self._conn.execute("""INSERT INTO evidence_fts(evidence_id, source_id, normalized_text)
                    SELECT e.id, e.source_id, e.normalized_text FROM evidence e
                    WHERE NOT EXISTS (SELECT 1 FROM evidence_fts f WHERE f.evidence_id=e.id)""")
                self._fts_available = True
            except sqlite3.OperationalError:
                self._fts_available = False

    def _audit(self, entity_type: str, entity_id: str, action: str,
               before: dict[str, Any] | None, after: dict[str, Any] | None, reason: str = "") -> None:
        self._conn.execute("""INSERT INTO audit
            (entity_type, entity_id, action, before_json, after_json, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (entity_type, entity_id, action, _json(before) if before is not None else None,
             _json(after) if after is not None else None, reason, _utc_now()))

    @staticmethod
    def _source_public(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["warnings"] = json.loads(item.pop("warnings_json"))
        item["ocr_requested"] = bool(item["ocr_requested"])
        item["title"] = item["filename"]
        return item

    def _import_result(self, row: sqlite3.Row) -> dict[str, Any]:
        evidence = [dict(item) for item in self._conn.execute(
            "SELECT * FROM evidence WHERE source_id=? ORDER BY rowid", (row["id"],))]
        pending_count = self._conn.execute("""SELECT COUNT(*) FROM facts f
            JOIN evidence e ON e.id=f.evidence_id WHERE e.source_id=? AND f.status='pending'""",
            (row["id"],)).fetchone()[0]
        return {"source": self._source_public(row), "evidence": evidence,
                "evidence_count": len(evidence), "pending_fact_count": pending_count}

    def _store_bytes(self, path: Path, data: bytes) -> None:
        # A generated temporary file + replace avoids following a symlink at
        # the final destination and avoids a partially written source file.
        if self.storage_dir.is_symlink() or self.storage_dir.resolve().parent != self.state_dir:
            raise ValueError("来源存储目录已被替换")
        descriptor, temporary = tempfile.mkstemp(prefix=".import-", dir=str(self.storage_dir))
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def import_bytes(self, filename: str, content_bytes: bytes, ocr: bool = False,
                     company_id: str = "") -> dict[str, Any]:
        """Content-hash dedup; matching-schema CSV proposes pending facts.

        ``pending_fact_count`` is the current pending count for this source,
        including on dedup. Reimporting never resets reviews or adds facts.
        Invalid CSV values retain row evidence and produce a source warning.
        """
        name = _normalise_filename(filename)
        if not isinstance(content_bytes, (bytes, bytearray, memoryview)):
            raise ValueError("content_bytes 必须是 bytes")
        size = content_bytes.nbytes if isinstance(content_bytes, memoryview) else len(content_bytes)
        if size > MAX_SOURCE_BYTES:
            raise ValueError("文件大小超过 12 MiB 上限")
        if not isinstance(ocr, bool):
            raise ValueError("ocr 必须为布尔值")
        company = _text(company_id, "company_id", required=False)
        data = bytes(content_bytes)
        digest = evidence_engine.sha256_bytes(data)
        with self._lock:
            existing = self._conn.execute("SELECT * FROM sources WHERE sha256=?", (digest,)).fetchone()
            if existing is not None:
                return self._import_result(existing)

        source_id = "src-" + digest
        suffix = Path(name).suffix.lower()
        proposed: list[dict[str, Any]] = []
        if suffix == ".csv":
            text, evidence, warnings, rows, line_numbers = evidence_engine.extract_csv_bytes(data, source_id)
            for row, line_number in zip(rows, line_numbers):
                payload = {**row, "evidence_id": f"{source_id}:row:{line_number}"}
                try:
                    proposed.append(self._validate_fact_payload(payload, check_evidence=False))
                except ValueError as exc:
                    warnings.append(f"CSV row {line_number} 未生成待审核事实：{exc}")
        else:
            text, evidence, warnings = evidence_engine.extract_bytes(name, data, source_id, ocr=ocr)
        path = self.storage_dir / (digest + suffix)
        now = _utc_now()
        source = {
            "id": source_id, "filename": name, "source_type": suffix.lstrip("."),
            "sha256": digest, "size_bytes": len(data), "storage_path": str(path),
            "company_id": company, "parse_status": "ok" if text.strip() else "empty",
            "ocr_requested": int(ocr), "warnings_json": _json(warnings), "created_at": now,
        }
        # Recheck dedup under SQLite's write lock: separate WorkspaceDB
        # instances may have parsed the same upload concurrently.
        with self._transaction():
            existing = self._conn.execute("SELECT * FROM sources WHERE sha256=?", (digest,)).fetchone()
            if existing is not None:
                return self._import_result(existing)
            self._store_bytes(path, data)
            try:
                self._conn.execute("""INSERT INTO sources
                    (id, filename, source_type, sha256, size_bytes, storage_path, company_id,
                     parse_status, ocr_requested, warnings_json, created_at)
                    VALUES (:id, :filename, :source_type, :sha256, :size_bytes, :storage_path,
                            :company_id, :parse_status, :ocr_requested, :warnings_json, :created_at)""", source)
                self._audit("source", source_id, "import", None,
                            self._source_public(self._conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()))
                for item in evidence:
                    record = {**item, "created_at": now}
                    self._conn.execute("""INSERT INTO evidence
                        (id, source_id, locator_type, locator_value, page_number, quote, normalized_text, created_at)
                        VALUES (:id, :source_id, :locator_type, :locator_value, :page_number,
                                :quote, :normalized_text, :created_at)""", record)
                    if self._fts_available:
                        self._conn.execute("INSERT INTO evidence_fts VALUES (?, ?, ?)",
                                           (record["id"], source_id, record["normalized_text"]))
                    self._audit("evidence", record["id"], "create", None, record)
                for payload in proposed:
                    self._insert_fact(payload, action="propose")
                return self._import_result(self._conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
            except BaseException:
                # The surrounding transaction rolls back all rows/audit/FTS.
                # Remove only the generated file created by this failed import.
                path.unlink(missing_ok=True)
                raise

    def _validate_fact_payload(self, payload: Mapping[str, Any], *, check_evidence: bool = True) -> dict[str, Any]:
        result = {}
        for field in FACT_REQUIRED_FIELDS:
            if field not in payload:
                raise ValueError(f"{field} 为必填项")
            if field != "value":
                result[field] = _text(payload[field], field)
        value = payload["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("value 必须为有限数值，不可为布尔值或 null")
        try:
            result["value"] = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("value 必须为有限数值") from exc
        if not math.isfinite(result["value"]):
            raise ValueError("value 必须为有限数值")
        for field in ("period_start", "period_end"):
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result[field]):
                    raise ValueError()
                date.fromisoformat(result[field])
            except ValueError as exc:
                raise ValueError(f"{field} 必须是有效 ISO 日期 YYYY-MM-DD") from exc
        if result["period_start"] > result["period_end"]:
            raise ValueError("period_start 不得晚于 period_end")
        status = payload.get("status", "pending")
        if not isinstance(status, str) or status != "pending":
            raise ValueError("新事实只能为 pending；请使用 review_fact 审核")
        review_reason = payload.get("review_reason", "")
        if review_reason is None:
            review_reason = ""
        result.update(status="pending", review_reason=_text(review_reason, "review_reason", required=False, maximum=4000))
        if check_evidence and self._conn.execute(
                "SELECT 1 FROM evidence WHERE id=?", (result["evidence_id"],)).fetchone() is None:
            raise ValueError("evidence_id 不存在")
        return result

    def _conflict_ids(self, fact: Mapping[str, Any]) -> list[str]:
        if fact["status"] == "rejected":
            return []
        # Same company/metric/date span/scope but different number OR reporting
        # basis/unit/currency is ambiguous, even while both are pending. Exact
        # corroborating duplicates are not conflicts. No implicit conversions.
        rows = self._conn.execute("""SELECT id FROM facts
            WHERE id<>? AND status<>'rejected' AND company_id=? AND metric_key=?
              AND period_start=? AND period_end=? AND scope=?
              AND (value<>? OR unit<>? OR currency<>? OR period_type<>? OR accounting_basis<>?)
            ORDER BY id""", (
                fact["id"], fact["company_id"], fact["metric_key"], fact["period_start"],
                fact["period_end"], fact["scope"], fact["value"], fact["unit"],
                fact["currency"], fact["period_type"], fact["accounting_basis"],
            )).fetchall()
        return [row["id"] for row in rows]

    def _fact_public(self, row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["conflicts_with"] = self._conflict_ids(result)
        result["conflict"] = bool(result["conflicts_with"])
        return result

    def _insert_fact(self, payload: Mapping[str, Any], *, action: str = "create") -> dict[str, Any]:
        # Recheck that every fact has the required fields before inserting.
        clean = self._validate_fact_payload(payload)
        now = _utc_now()
        record = {**clean, "id": "fact-" + uuid.uuid4().hex, "created_at": now, "updated_at": now}
        self._conn.execute("""INSERT INTO facts
            (id, company_id, metric_key, value, unit, currency, period_start, period_end,
             period_type, accounting_basis, scope, evidence_id, status, review_reason, created_at, updated_at)
            VALUES (:id, :company_id, :metric_key, :value, :unit, :currency, :period_start, :period_end,
                    :period_type, :accounting_basis, :scope, :evidence_id, :status, :review_reason, :created_at, :updated_at)""", record)
        result = self._fact_public(record)
        self._audit("fact", record["id"], action, None, result)
        if result["conflict"]:
            self._audit("fact", record["id"], "conflict_alert", None, result,
                        "同公司、指标、期间、scope 存在数值或口径冲突；需要人工核验")
        return result

    def add_fact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("payload 必须为对象")
        with self._transaction():
            return self._insert_fact(payload)

    def review_fact(self, fact_id: str, status: str, reason: str) -> dict[str, Any]:
        fact_id = _text(fact_id, "fact_id")
        if not isinstance(status, str) or status not in FACT_STATUSES:
            raise ValueError("status 只能是 pending、accepted 或 rejected")
        reason = _text(reason, "reason", maximum=4000)
        with self._transaction():
            row = self._conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            if row is None:
                raise ValueError(f"fact_id 不存在：{fact_id}")
            before = self._fact_public(row)
            self._conn.execute("UPDATE facts SET status=?, review_reason=?, updated_at=? WHERE id=?",
                               (status, reason, _utc_now(), fact_id))
            after = self._fact_public(self._conn.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone())
            self._audit("fact", fact_id, "review", before, after, reason)
            if after["conflict"]:
                self._audit("fact", fact_id, "conflict_alert", before, after,
                            "审核后仍存在同公司、指标、期间、scope 冲突；未自动覆盖其他事实")
            return after

    def sources(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._source_public(row) for row in self._conn.execute(
                "SELECT * FROM sources ORDER BY created_at DESC, id")]

    def evidence(self, query: str = "", source_id: str = "") -> list[dict[str, Any]]:
        query = _text(query, "query", required=False, maximum=2000)
        source_id = _text(source_id, "source_id", required=False)
        with self._lock:
            filter_sql = " WHERE source_id=?" if source_id else ""
            filter_args = [source_id] if source_id else []
            if not query:
                return [dict(row) for row in self._conn.execute(
                    "SELECT * FROM evidence" + filter_sql + " ORDER BY rowid", filter_args)]
            # Treat query as literal search terms, never user-supplied FTS
            # syntax. The substring fallback also covers CJK tokenization.
            terms = list(dict.fromkeys(re.findall(r"\w+", query.lower())))
            if not terms:
                terms = [query.lower()]
            found = {}
            if self._fts_available:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                sql = """SELECT e.* FROM evidence e JOIN evidence_fts f ON f.evidence_id=e.id
                         WHERE evidence_fts MATCH ?"""
                args = [expression]
                if source_id:
                    sql += " AND e.source_id=?"
                    args.append(source_id)
                try:
                    found = {row["id"]: dict(row) for row in self._conn.execute(sql, args)}
                except sqlite3.OperationalError:
                    pass
            sql = "SELECT * FROM evidence WHERE (" + " OR ".join(
                "instr(lower(normalized_text), ?)>0" for _ in terms) + ")"
            args = list(terms)
            if source_id:
                sql += " AND source_id=?"
                args.append(source_id)
            for row in self._conn.execute(sql, args):
                found[row["id"]] = dict(row)
            return sorted(found.values(), key=lambda item: (
                -sum(item["normalized_text"].lower().count(term) for term in terms), item["id"]))

    def facts(self, company_id: str = "") -> list[dict[str, Any]]:
        company_id = _text(company_id, "company_id", required=False)
        with self._lock:
            sql = "SELECT * FROM facts" + (" WHERE company_id=?" if company_id else "")
            rows = self._conn.execute(sql + " ORDER BY period_end DESC, created_at DESC, id",
                                      (company_id,) if company_id else ()).fetchall()
            return [self._fact_public(row) for row in rows]

    def audit(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for row in self._conn.execute("SELECT * FROM audit ORDER BY id"):
                item = dict(row)
                before, after = item.pop("before_json"), item.pop("after_json")
                item.update(before=json.loads(before) if before else None,
                            after=json.loads(after) if after else None)
                result.append(item)
            return result

    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {table: self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("sources", "evidence", "facts", "audit")}
            for status in ("pending", "accepted", "rejected"):
                counts[status + "_facts"] = self._conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE status=?", (status,)).fetchone()[0]
            counts["conflicting_facts"] = sum(bool(self._conflict_ids(dict(row))) for row in
                                               self._conn.execute("SELECT * FROM facts").fetchall())
            counts["fts_available"] = self._fts_available
            return counts


__all__ = ["WorkspaceDB", "MAX_SOURCE_BYTES", "FACT_STATUSES", "FACT_REQUIRED_FIELDS"]
