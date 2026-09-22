"""Experimental-data snapshot backup and atomic restore.

A snapshot is a ZIP archive containing one newline-terminated JSONL file per
table (``settings.jsonl`` / ``events.jsonl`` / ``attempts.jsonl``) plus a
``manifest.json`` that records, for each data file:

* ``rows``     — number of JSONL lines,
* ``sha256``   — hex SHA-256 of the exact file bytes,
* ``db_version`` — SQLite schema version the archive was produced from.

Field order inside every JSONL object is fixed (the order columns were dumped
in), so archives are byte-reproducible and diff-friendly.

Restore is all-or-nothing: the archive structure, manifest, hashes, row
counts, foreign-key relationships and event payloads are all validated
*before* a single byte is written; only then are events/attempts replaced in
one transaction. The current settings row is never modified.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

from . import db

FORMAT = "webhook-replay-lab-snapshot"
MANIFEST_NAME = "manifest.json"
SETTINGS_FILE = "settings.jsonl"
EVENTS_FILE = "events.jsonl"
ATTEMPTS_FILE = "attempts.jsonl"
DATA_FILES = (SETTINGS_FILE, EVENTS_FILE, ATTEMPTS_FILE)

SETTINGS_FIELDS = list(db.SETTING_COLUMNS)
EVENT_FIELDS = list(db.EVENT_COLUMNS)
ATTEMPT_FIELDS = list(db.ATTEMPT_COLUMNS)

EVENT_STATUSES = {"pending", "queued", "running", "success", "failed"}
ATTEMPT_OUTCOMES = {"delivered", "retriable", "non_retriable"}

# Reject oversized uploads before they touch the parser.
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def backup_filename() -> str:
    return f"webhook-replay-snapshot-{_ts()}.zip"


def _encode_line(obj: dict[str, Any], fields: list[str]) -> bytes:
    return (json.dumps([obj.get(f) for f in fields], ensure_ascii=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _decode_line(line: bytes) -> list[Any]:
    return json.loads(line.decode("utf-8"))


def create_backup() -> bytes:
    """Export settings/events/attempts to an in-memory ZIP archive."""
    dataset = db.dump_dataset()
    db_version = db.get_schema_version()

    files: list[tuple[str, bytes]] = []
    manifest_files: dict[str, dict[str, Any]] = {}
    for name, fields, rows in (
        (SETTINGS_FILE, SETTINGS_FIELDS, dataset["settings"]),
        (EVENTS_FILE, EVENT_FIELDS, dataset["events"]),
        (ATTEMPTS_FILE, ATTEMPT_FIELDS, dataset["attempts"]),
    ):
        buf = b"".join(_encode_line(row, fields) for row in rows)
        files.append((name, buf))
        manifest_files[name] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(buf).hexdigest(),
            "bytes": len(buf),
        }

    manifest = {
        "format": FORMAT,
        "db_version": db_version,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": manifest_files,
    }

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # manifest first so streaming readers see it before the data
        zf.writestr(MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
        for name, buf in files:
            zf.writestr(name, buf)
    return out.getvalue()


class RestoreError(Exception):
    """Validation failed; nothing was written to the database."""

    def __init__(self, message: str, checks: list[dict[str, Any]]):
        super().__init__(message)
        self.checks = checks


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool))


def _is_nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v != ""


def restore_backup(raw: bytes) -> dict[str, Any]:
    """Validate a snapshot archive and atomically restore it.

    Raises ``RestoreError`` (no DB writes) on the first failed validation
    phase. On success returns a summary dict including the manifest and the
    full checklist.
    """
    checks: list[dict[str, Any]] = []

    def fail(name: str, detail: str) -> None:
        checks.append({"name": name, "ok": False, "detail": detail})
        raise RestoreError(detail, checks)

    def check(name: str, ok: bool, detail: str = "") -> None:
        if not ok:
            fail(name, detail)
        checks.append({"name": name, "ok": True, "detail": detail})

    # --- phase 1: container ---
    check("archive.non_empty", len(raw) > 0,
          f"{len(raw)} bytes" if raw else "")
    check("archive.size_limit", len(raw) <= MAX_ARCHIVE_BYTES,
          f"{len(raw)} bytes")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        fail("archive.zip", f"无法打开 ZIP 压缩包: {exc}")
    with zf:
        bad = zf.testzip()
        check("archive.zip_crc", bad is None,
              "" if bad is None else f"损坏的条目: {bad}")

        names = zf.namelist()
        check("archive.entries",
              set(names) == {MANIFEST_NAME, *DATA_FILES} and len(names) == len(set(names)),
              ", ".join(sorted(names)))
        for name in names:
            info = zf.getinfo(name)
            if info.is_dir() or name.startswith(("/", "\\")) or ".." in name.split("/"):
                fail("archive.entry_safe", f"非法条目路径: {name}")

        # --- phase 2: manifest ---
        try:
            manifest_bytes = zf.read(MANIFEST_NAME)
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except KeyError:
            fail("manifest.present", "缺少 manifest.json")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail("manifest.json", f"manifest.json 不是合法 JSON: {exc}")
        check("manifest.format", isinstance(manifest, dict)
              and manifest.get("format") == FORMAT,
              str(manifest.get("format")) if isinstance(manifest, dict) else "")
        check("manifest.db_version", _is_int(manifest.get("db_version")),
              str(manifest.get("db_version")))
        files_meta = manifest.get("files")
        check("manifest.files", isinstance(files_meta, dict)
              and all(n in files_meta for n in DATA_FILES),
              ", ".join(sorted(files_meta)) if isinstance(files_meta, dict) else "")
        for name in DATA_FILES:
            meta = files_meta[name]
            check(f"manifest.{name}.meta",
                  isinstance(meta, dict)
                  and _is_int(meta.get("rows")) and meta["rows"] >= 0
                  and _is_nonempty_str(meta.get("sha256"))
                  and all(c in "0123456789abcdef" for c in str(meta["sha256"]))
                  and len(str(meta["sha256"])) == 64,
                  json.dumps(meta, ensure_ascii=False))

        # --- phase 3: each data file's hash / row count / shape ---
        decoded: dict[str, list[list[Any]]] = {}
        for name, fields in ((SETTINGS_FILE, SETTINGS_FIELDS),
                             (EVENTS_FILE, EVENT_FIELDS),
                             (ATTEMPTS_FILE, ATTEMPT_FIELDS)):
            buf = zf.read(name)
            digest = hashlib.sha256(buf).hexdigest()
            check(f"{name}.sha256", digest == files_meta[name]["sha256"],
                  digest[:16] + "…")
            lines = buf.splitlines()
            check(f"{name}.row_count", len(lines) == files_meta[name]["rows"],
                  f"清单声明 {files_meta[name]['rows']} 行，实际 {len(lines)} 行")
            rows: list[list[Any]] = []
            row_errors: list[str] = []
            for i, line in enumerate(lines, start=1):
                try:
                    value = _decode_line(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    row_errors.append(f"第 {i} 行: JSON 无法解析 ({exc})")
                    continue
                if not isinstance(value, list) or len(value) != len(fields):
                    row_errors.append(
                        f"第 {i} 行: 应为 {len(fields)} 个固定字段的数组，实际 "
                        f"{len(value) if isinstance(value, list) else type(value).__name__}")
                    continue
                rows.append(value)
            check(f"{name}.jsonl_shape", not row_errors,
                  (f"{len(lines)} 行通过"
                   + ("" if not row_errors else
                      f"；{len(row_errors)} 行异常: " + "; ".join(row_errors[:3]))))
            decoded[name] = rows

    # --- phase 4: semantic validation (still no writes) ---
    # settings: exactly one row, validated for completeness but never written
    settings_errors: list[str] = []

    def settings_err(condition: bool, msg: str) -> None:
        if not condition:
            settings_errors.append(msg)

    settings_rows = decoded[SETTINGS_FILE]
    settings_err(len(settings_rows) == 1,
                f"settings.jsonl 必须恰有 1 行，实际 {len(settings_rows)} 行")
    settings_present: dict[str, Any] = {}
    if len(settings_rows) == 1:
        s = settings_present = dict(zip(SETTINGS_FIELDS, settings_rows[0]))
        settings_err(s.get("id") == 1, f"settings.id 必须为 1，实际 {s.get('id')!r}")
        settings_err(_is_nonempty_str(s.get("target_url")), "settings.target_url 必须为非空字符串")
        settings_err(isinstance(s.get("hmac_secret"), str), "settings.hmac_secret 必须为字符串")
        settings_err(_is_int(s.get("max_retries")) and 0 <= s["max_retries"] <= 10,
                     f"settings.max_retries 必须为 0–10 的整数，实际 {s.get('max_retries')!r}")
        settings_err(_is_number(s.get("timeout_seconds")) and 0 < s["timeout_seconds"] <= 60,
                     f"settings.timeout_seconds 必须为 (0,60] 的数值，实际 {s.get('timeout_seconds')!r}")
    check("settings.semantic", not settings_errors,
          "; ".join(settings_errors[:3]) if settings_errors
          else "配置行完整（恢复时保留当前配置）")

    # events
    events_errors: list[str] = []
    event_ids: set[str] = set()
    dup_ids: set[str] = set()
    event_records: list[dict[str, Any]] = []
    for i, values in enumerate(decoded[EVENTS_FILE], start=1):
        e = dict(zip(EVENT_FIELDS, values))
        prefix = f"events 第 {i} 行({e.get('event_id')!r})"
        if not _is_nonempty_str(e.get("event_id")):
            events_errors.append(f"{prefix}: event_id 必须为非空字符串")
        elif e["event_id"] in event_ids:
            dup_ids.add(e["event_id"])
        event_ids.add(e["event_id"])
        if not _is_int(e.get("seq")) or e["seq"] < 1:
            events_errors.append(f"{prefix}: seq 必须为 ≥1 的整数")
        if not isinstance(e.get("payload"), str):
            events_errors.append(f"{prefix}: payload 必须为 JSON 字符串")
        else:
            try:
                parsed = json.loads(e["payload"])
                if not isinstance(parsed, dict):
                    events_errors.append(f"{prefix}: payload 必须为 JSON 对象")
            except json.JSONDecodeError as exc:
                events_errors.append(f"{prefix}: payload 无法解析 ({exc.msg})")
        if e.get("status") not in EVENT_STATUSES:
            events_errors.append(
                f"{prefix}: status 非法 {e.get('status')!r}")
        if not _is_nonempty_str(e.get("created_at")):
            events_errors.append(f"{prefix}: created_at 必须为非空字符串")
        if not _is_nonempty_str(e.get("updated_at")):
            events_errors.append(f"{prefix}: updated_at 必须为非空字符串")
        event_records.append(e)
    if dup_ids:
        events_errors.append(f"event_id 重复: {sorted(dup_ids)}")
    seqs = [e["seq"] for e in event_records if _is_int(e.get("seq"))]
    if len(seqs) != len(set(seqs)):
        events_errors.append("seq 存在重复值")
    check("events.semantic", not events_errors,
          (f"{len(event_records)} 行通过"
           + ("" if not events_errors else
              f"；{len(events_errors)} 项错误: " + "; ".join(events_errors[:3]))))

    # attempts: foreign keys + field domains
    attempts_errors: list[str] = []
    attempt_ids: set[int] = set()
    dup_attempt_ids: set[int] = set()
    attempt_records: list[dict[str, Any]] = []
    for i, values in enumerate(decoded[ATTEMPTS_FILE], start=1):
        a = dict(zip(ATTEMPT_FIELDS, values))
        prefix = f"attempts 第 {i} 行(id={a.get('id')!r})"
        if not _is_int(a.get("id")) or a["id"] < 1:
            attempts_errors.append(f"{prefix}: id 必须为 ≥1 的整数")
        elif a["id"] in attempt_ids:
            dup_attempt_ids.add(a["id"])
        attempt_ids.add(a["id"])
        if not _is_nonempty_str(a.get("event_id")) or a["event_id"] not in event_ids:
            attempts_errors.append(
                f"{prefix}: event_id {a.get('event_id')!r} 在 events 中不存在（违反外键）")
        if not _is_int(a.get("attempt_no")) or a["attempt_no"] < 1:
            attempts_errors.append(f"{prefix}: attempt_no 必须为 ≥1 的整数")
        if not _is_int(a.get("replay_no")) or a["replay_no"] < 1:
            attempts_errors.append(f"{prefix}: replay_no 必须为 ≥1 的整数")
        if not _is_nonempty_str(a.get("idem_key")):
            attempts_errors.append(f"{prefix}: idem_key 必须为非空字符串")
        if not _is_nonempty_str(a.get("request_at")):
            attempts_errors.append(f"{prefix}: request_at 必须为非空字符串")
        if a.get("response_at") is not None and not isinstance(a["response_at"], str):
            attempts_errors.append(f"{prefix}: response_at 必须为字符串或 null")
        if a.get("http_status") is not None and (
                not _is_int(a["http_status"]) or not (100 <= a["http_status"] <= 599)):
            attempts_errors.append(
                f"{prefix}: http_status 必须为 100–599 的整数或 null")
        if a.get("outcome") not in ATTEMPT_OUTCOMES:
            attempts_errors.append(f"{prefix}: outcome 非法 {a.get('outcome')!r}")
        if not _is_int(a.get("elapsed_ms")) or a["elapsed_ms"] < 0:
            attempts_errors.append(f"{prefix}: elapsed_ms 必须为 ≥0 的整数")
        if not isinstance(a.get("req_headers"), str):
            attempts_errors.append(f"{prefix}: req_headers 必须为 JSON 字符串")
        else:
            try:
                if not isinstance(json.loads(a["req_headers"]), dict):
                    attempts_errors.append(f"{prefix}: req_headers 必须为 JSON 对象")
            except json.JSONDecodeError as exc:
                attempts_errors.append(f"{prefix}: req_headers 无法解析 ({exc.msg})")
        if a.get("resp_excerpt") is not None and not isinstance(a["resp_excerpt"], str):
            attempts_errors.append(f"{prefix}: resp_excerpt 必须为字符串或 null")
        attempt_records.append(a)
    if dup_attempt_ids:
        attempts_errors.append(f"attempt id 重复: {sorted(dup_attempt_ids)}")
    check("attempts.semantic", not attempts_errors,
          (f"{len(attempt_records)} 行通过"
           + ("" if not attempts_errors else
              f"；{len(attempts_errors)} 项错误: " + "; ".join(attempts_errors[:3]))))

    # db version compatibility — last semantic gate
    current_version = db.get_schema_version()
    check("db.version", manifest["db_version"] == current_version,
          f"备份 v{manifest['db_version']} / 当前 v{current_version}")

    # --- phase 5: single-transaction replacement (settings preserved) ---
    try:
        result = db.replace_dataset(event_records, attempt_records)
    except Exception as exc:
        # replace_dataset rolls back on any error, so the previous data is intact.
        fail("restore.transaction", f"事务写入失败并已回滚: {exc!r}")
    checks.append({"name": "restore.transaction", "ok": True,
                   "detail": "单事务替换 events/attempts，现有 settings 未改动"})

    return {
        "restored": True,
        "manifest": manifest,
        "checks": checks,
        "counts": {
            "settings": len(settings_rows),
            "events": len(event_records),
            "attempts": len(attempt_records),
            "queued_running_reset_to_pending": result["reset_to_pending"],
        },
        "settings_preserved": db.get_settings(),
        "snapshot_settings": {
            "target_url": settings_present.get("target_url"),
            "hmac_secret": "***" if settings_present.get("hmac_secret") else "",
            "max_retries": settings_present.get("max_retries"),
            "timeout_seconds": settings_present.get("timeout_seconds"),
        },
    }
