"""
backend/logs.py

Read-only PostgreSQL access for FTP download activity logs.
Connects to the separate db_logs container.
Also reads/writes tailer_config for settings and status.
"""

import csv
import io
from datetime import datetime
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config import settings

# ── Connection pool ───────────────────────────────────────────────────────────
_pool: psycopg2.pool.SimpleConnectionPool | None = None


def _get_pool() -> psycopg2.pool.SimpleConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.SimpleConnectionPool(
            1, 6,
            settings.logs_database_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return _pool


class _Ctx:
    def __enter__(self):
        self.c = _get_pool().getconn()
        return self.c
    def __exit__(self, *_):
        _get_pool().putconn(self.c)


def _conn():
    return _Ctx()


def _q(conn, sql: str, params=()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _q1(conn, sql: str, params=()) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


# ── Tailer config helpers ─────────────────────────────────────────────────────

def get_tailer_config(key: str) -> Optional[str]:
    with _conn() as conn:
        row = _q1(conn, "SELECT value FROM tailer_config WHERE key = %s", (key,))
        return row["value"] if row else None


def set_tailer_config(key: str, value: str):
    with _conn() as conn:
        conn.cursor().execute(
            """
            INSERT INTO tailer_config (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (key, value),
        )
        conn.commit()


def get_log_filename() -> str:
    return get_tailer_config("log_filename") or "full_user.log"


def get_retention_days() -> int:
    v = get_tailer_config("log_retention_days")
    try:
        return int(v) if v else 90
    except ValueError:
        return 90


def get_retention_enabled() -> bool:
    v = get_tailer_config("log_retention_enabled")
    return v.lower() not in ("false", "0", "no") if v else True


def update_log_settings(log_filename: Optional[str], retention_days: Optional[int],
                        retention_enabled: Optional[bool] = None):
    if log_filename is not None:
        set_tailer_config("log_filename", log_filename)
    if retention_days is not None:
        set_tailer_config("log_retention_days", str(retention_days))
    if retention_enabled is not None:
        set_tailer_config("log_retention_enabled", "true" if retention_enabled else "false")


def get_system_status() -> dict:
    with _conn() as conn:
        rows = _q(conn, "SELECT key, value FROM tailer_config")
        cfg = {r["key"]: r["value"] for r in rows}

        ud_total = _q1(conn, "SELECT COUNT(*) AS n FROM user_downloads")["n"]
        ad_total = _q1(conn, "SELECT COUNT(*) AS n FROM anon_downloads")["n"]

    return {
        "log_filename":            cfg.get("log_filename", "full_user.log"),
        "log_retention_days":      int(cfg.get("log_retention_days", "90")),
        "log_retention_enabled":   cfg.get("log_retention_enabled", "true").lower() not in ("false", "0", "no"),
        "tailer_status":           cfg.get("tailer_status", "unknown"),
        "tailer_last_write":       cfg.get("tailer_last_write", ""),
        "tailer_pos":              int(cfg.get("tailer_pos", "0")),
        "tailer_total_rows":       ud_total + ad_total,
    }


# ── Retention pruning ─────────────────────────────────────────────────────────

def prune_old_records():
    """Delete rows older than the configured retention period. Skipped if retention is disabled."""
    if not get_retention_enabled():
        return 0, 0
    days = get_retention_days()
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_downloads WHERE logged_at < NOW() - INTERVAL '%s days'",
                (days,),
            )
            ud = cur.rowcount
            cur.execute(
                "DELETE FROM anon_downloads WHERE logged_at < NOW() - INTERVAL '%s days'",
                (days,),
            )
            ad = cur.rowcount
        conn.commit()
    return ud, ad


# ── Filter builder ────────────────────────────────────────────────────────────

def _where(
    username: Optional[str] = None,
    ip: Optional[str] = None,
    filename: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple[str, list]:
    clauses, params = [], []
    if username:
        clauses.append("username ILIKE %s")
        params.append(f"%{username}%")
    if ip:
        clauses.append("HOST(ip_address) ILIKE %s")
        params.append(f"%{ip}%")
    if filename:
        clauses.append("(filename ILIKE %s OR filepath ILIKE %s)")
        params.extend([f"%{filename}%", f"%{filename}%"])
    if date_from:
        clauses.append("logged_at >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("logged_at <= %s")
        params.append(date_to)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _ser(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


# ── User downloads ────────────────────────────────────────────────────────────

def get_user_downloads(
    page: int = 1, limit: int = 100,
    username: Optional[str] = None, ip: Optional[str] = None,
    filename: Optional[str] = None, date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    limit  = min(limit, 500)
    offset = (page - 1) * limit
    where, params = _where(username=username, ip=ip, filename=filename,
                           date_from=date_from, date_to=date_to)
    with _conn() as conn:
        total = _q1(conn, f"SELECT COUNT(*) AS n FROM user_downloads{where}", params)["n"]
        rows  = _q(conn,
            f"""SELECT id, logged_at AT TIME ZONE 'UTC' AS logged_at,
                       HOST(ip_address) AS ip_address,
                       username, filepath, filename, bytes
                FROM user_downloads{where}
                ORDER BY logged_at DESC LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
    return {
        "total": total, "page": page, "limit": limit,
        "pages": max(1, -(-total // limit)),
        "rows": [_ser(r) for r in rows],
    }


def get_user_downloads_for_user(username: str, limit: int = 50) -> list:
    with _conn() as conn:
        rows = _q(conn,
            """SELECT id, logged_at AT TIME ZONE 'UTC' AS logged_at,
                      HOST(ip_address) AS ip_address,
                      filepath, filename, bytes
               FROM user_downloads
               WHERE username = %s
               ORDER BY logged_at DESC LIMIT %s""",
            (username, limit),
        )
    return [_ser(r) for r in rows]


def get_user_download_stats(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
) -> dict:
    where, params = _where(date_from=date_from, date_to=date_to)
    with _conn() as conn:
        agg = _q1(conn,
            f"""SELECT COUNT(*) AS total,
                       COUNT(DISTINCT username) AS unique_users,
                       COUNT(DISTINCT ip_address) AS unique_ips,
                       COALESCE(SUM(bytes), 0) AS total_bytes
                FROM user_downloads{where}""", params)
        top_files = _q(conn,
            f"""SELECT filepath, filename, COUNT(*) AS downloads
                FROM user_downloads{where}
                GROUP BY filepath, filename ORDER BY downloads DESC LIMIT 10""", params)
        top_users = _q(conn,
            f"""SELECT username, COUNT(*) AS downloads,
                       COALESCE(SUM(bytes), 0) AS bytes
                FROM user_downloads{where}
                GROUP BY username ORDER BY downloads DESC LIMIT 10""", params)
        top_ips = _q(conn,
            f"""SELECT HOST(ip_address) AS ip_address, COUNT(*) AS downloads
                FROM user_downloads{where}
                GROUP BY ip_address ORDER BY downloads DESC LIMIT 10""", params)
    return {
        "total": agg["total"], "unique_users": agg["unique_users"],
        "unique_ips": agg["unique_ips"], "total_bytes": agg["total_bytes"],
        "top_files": [dict(r) for r in top_files],
        "top_users": [dict(r) for r in top_users],
        "top_ips":   [dict(r) for r in top_ips],
    }


def get_user_download_timeline(days: int = 30, bucket: str = "day") -> list:
    bucket = bucket if bucket in ("hour", "day", "week") else "day"
    with _conn() as conn:
        return _q(conn,
            f"""SELECT date_trunc(%s, logged_at) AS bucket,
                       COUNT(*) AS downloads, COALESCE(SUM(bytes), 0) AS bytes
                FROM user_downloads
                WHERE logged_at >= NOW() - INTERVAL '{days} days'
                GROUP BY bucket ORDER BY bucket ASC""", (bucket,))


def get_user_download_breakdown(days: int = 30) -> list:
    with _conn() as conn:
        return _q(conn,
            f"""SELECT username, COUNT(*) AS downloads,
                       COALESCE(SUM(bytes), 0) AS bytes
                FROM user_downloads
                WHERE logged_at >= NOW() - INTERVAL '{days} days'
                GROUP BY username ORDER BY downloads DESC LIMIT 25""")


def export_user_downloads_csv(
    username: Optional[str] = None, ip: Optional[str] = None,
    filename: Optional[str] = None, date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> str:
    where, params = _where(username=username, ip=ip, filename=filename,
                           date_from=date_from, date_to=date_to)
    with _conn() as conn:
        rows = _q(conn,
            f"""SELECT logged_at AT TIME ZONE 'UTC' AS logged_at,
                       HOST(ip_address) AS ip_address,
                       username, filepath, filename, bytes
                FROM user_downloads{where}
                ORDER BY logged_at DESC LIMIT 100000""", params)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "ip_address", "username", "filepath", "filename", "bytes"])
    for r in rows:
        w.writerow([r["logged_at"], r["ip_address"], r["username"],
                    r["filepath"], r["filename"], r["bytes"]])
    return buf.getvalue()


# ── Anon downloads ────────────────────────────────────────────────────────────

def get_anon_downloads(
    page: int = 1, limit: int = 100,
    ip: Optional[str] = None, filename: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
) -> dict:
    limit  = min(limit, 500)
    offset = (page - 1) * limit
    where, params = _where(ip=ip, filename=filename,
                           date_from=date_from, date_to=date_to)
    with _conn() as conn:
        total = _q1(conn, f"SELECT COUNT(*) AS n FROM anon_downloads{where}", params)["n"]
        rows  = _q(conn,
            f"""SELECT id, logged_at AT TIME ZONE 'UTC' AS logged_at,
                       HOST(ip_address) AS ip_address,
                       filepath, filename, bytes
                FROM anon_downloads{where}
                ORDER BY logged_at DESC LIMIT %s OFFSET %s""",
            params + [limit, offset],
        )
    return {
        "total": total, "page": page, "limit": limit,
        "pages": max(1, -(-total // limit)),
        "rows": [_ser(r) for r in rows],
    }


def get_anon_download_stats(
    date_from: Optional[str] = None, date_to: Optional[str] = None,
) -> dict:
    where, params = _where(date_from=date_from, date_to=date_to)
    with _conn() as conn:
        agg = _q1(conn,
            f"""SELECT COUNT(*) AS total,
                       COUNT(DISTINCT ip_address) AS unique_ips,
                       COALESCE(SUM(bytes), 0) AS total_bytes
                FROM anon_downloads{where}""", params)
        top_files = _q(conn,
            f"""SELECT filepath, filename, COUNT(*) AS downloads
                FROM anon_downloads{where}
                GROUP BY filepath, filename ORDER BY downloads DESC LIMIT 10""", params)
        top_ips = _q(conn,
            f"""SELECT HOST(ip_address) AS ip_address, COUNT(*) AS downloads
                FROM anon_downloads{where}
                GROUP BY ip_address ORDER BY downloads DESC LIMIT 10""", params)
    return {
        "total": agg["total"], "unique_ips": agg["unique_ips"],
        "total_bytes": agg["total_bytes"],
        "top_files": [dict(r) for r in top_files],
        "top_ips":   [dict(r) for r in top_ips],
    }


def get_anon_download_timeline(days: int = 30, bucket: str = "day") -> list:
    bucket = bucket if bucket in ("hour", "day", "week") else "day"
    with _conn() as conn:
        return _q(conn,
            f"""SELECT date_trunc(%s, logged_at) AS bucket,
                       COUNT(*) AS downloads, COALESCE(SUM(bytes), 0) AS bytes
                FROM anon_downloads
                WHERE logged_at >= NOW() - INTERVAL '{days} days'
                GROUP BY bucket ORDER BY bucket ASC""", (bucket,))


def export_anon_downloads_csv(
    ip: Optional[str] = None, filename: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
) -> str:
    where, params = _where(ip=ip, filename=filename,
                           date_from=date_from, date_to=date_to)
    with _conn() as conn:
        rows = _q(conn,
            f"""SELECT logged_at AT TIME ZONE 'UTC' AS logged_at,
                       HOST(ip_address) AS ip_address,
                       filepath, filename, bytes
                FROM anon_downloads{where}
                ORDER BY logged_at DESC LIMIT 100000""", params)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "ip_address", "filepath", "filename", "bytes"])
    for r in rows:
        w.writerow([r["logged_at"], r["ip_address"],
                    r["filepath"], r["filename"], r["bytes"]])
    return buf.getvalue()


# ── Combined summary ──────────────────────────────────────────────────────────

def get_summary(days: int = 30) -> dict:
    with _conn() as conn:
        cur = _q1(conn, f"""
            SELECT
              (SELECT COUNT(*) FROM user_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days} days') AS user_dl_cur,
              (SELECT COUNT(*) FROM user_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days*2} days'
                 AND logged_at <  NOW() - INTERVAL '{days} days')  AS user_dl_prev,
              (SELECT COALESCE(SUM(bytes),0) FROM user_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days} days') AS user_bytes_cur,
              (SELECT COALESCE(SUM(bytes),0) FROM user_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days*2} days'
                 AND logged_at <  NOW() - INTERVAL '{days} days')  AS user_bytes_prev,
              (SELECT COUNT(*) FROM anon_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days} days') AS anon_dl_cur,
              (SELECT COUNT(*) FROM anon_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days*2} days'
                 AND logged_at <  NOW() - INTERVAL '{days} days')  AS anon_dl_prev,
              (SELECT COALESCE(SUM(bytes),0) FROM anon_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days} days') AS anon_bytes_cur,
              (SELECT COALESCE(SUM(bytes),0) FROM anon_downloads
               WHERE logged_at >= NOW() - INTERVAL '{days*2} days'
                 AND logged_at <  NOW() - INTERVAL '{days} days')  AS anon_bytes_prev
        """)
        timeline = _q(conn, f"""
            SELECT bucket,
                   SUM(user_dl) AS user_downloads,
                   SUM(anon_dl) AS anon_downloads,
                   SUM(total_bytes) AS bytes
            FROM (
                SELECT date_trunc('day', logged_at) AS bucket,
                       COUNT(*) AS user_dl, 0 AS anon_dl,
                       COALESCE(SUM(bytes), 0) AS total_bytes
                FROM user_downloads
                WHERE logged_at >= NOW() - INTERVAL '{days} days'
                GROUP BY bucket
                UNION ALL
                SELECT date_trunc('day', logged_at) AS bucket,
                       0 AS user_dl, COUNT(*) AS anon_dl,
                       COALESCE(SUM(bytes), 0) AS total_bytes
                FROM anon_downloads
                WHERE logged_at >= NOW() - INTERVAL '{days} days'
                GROUP BY bucket
            ) sub
            GROUP BY bucket ORDER BY bucket ASC
        """)

    def pct(c, p):
        return round(((c - p) / p) * 100, 1) if p else None

    return {
        "days": days,
        "user_downloads":  {"current": cur["user_dl_cur"],    "previous": cur["user_dl_prev"],    "change": pct(cur["user_dl_cur"],    cur["user_dl_prev"])},
        "user_bytes":      {"current": cur["user_bytes_cur"], "previous": cur["user_bytes_prev"], "change": pct(cur["user_bytes_cur"], cur["user_bytes_prev"])},
        "anon_downloads":  {"current": cur["anon_dl_cur"],    "previous": cur["anon_dl_prev"],    "change": pct(cur["anon_dl_cur"],    cur["anon_dl_prev"])},
        "anon_bytes":      {"current": cur["anon_bytes_cur"], "previous": cur["anon_bytes_prev"], "change": pct(cur["anon_bytes_cur"], cur["anon_bytes_prev"])},
        "timeline": [_ser(r) for r in timeline],
    }
