"""
logtailer/tailer.py

Tails a ProFTPd ExtendedLog (custom_user format) from a mounted log directory
and writes successful RETR (download) events into PostgreSQL.

Log format (7 pipe-delimited fields):
  [11/May/2026:18:04:13 +0000]|IP|USER|PATH|COMMAND|STATUS|BYTES

Behaviour:
  - Only RETR lines with status 226 (successful transfer) are recorded
  - username == 'anonftp' → anon_downloads table
  - any other real username → user_downloads table
  - Target log filename is read from tailer_config in the DB every CONFIG_POLL_INTERVAL
    seconds — changing it via the UI causes the tailer to switch files without restart
  - File position is persisted to a named Docker volume so restarts don't reprocess
  - Status (position, last write, row count) is written back to tailer_config after each commit
"""

import os
import re
import time
import logging
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

# ── Config ────────────────────────────────────────────────────────────────────
LOG_DIR         = os.environ.get("FTP_LOG_DIR",      "/logs")
DB_URL          = os.environ.get("LOGS_DATABASE_URL", "")
POS_PATH        = os.environ.get("POS_PATH",          "/pos/tailer.pos")
POLL_INTERVAL        = float(os.environ.get("POLL_INTERVAL",        "1"))
REOPEN_INTERVAL      = float(os.environ.get("REOPEN_INTERVAL",      "60"))
CONFIG_POLL_INTERVAL = float(os.environ.get("CONFIG_POLL_INTERVAL", "10"))
BATCH_SIZE      = int(os.environ.get("BATCH_SIZE", "100"))
ANON_USER       = "anonftp"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [tailer] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("tailer")

TS_RE = re.compile(
    r'^\[(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2})\s[+\-]\d{4}\]$'
)
MONTHS = {
    "Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
    "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12,
}


def parse_timestamp(ts_field: str) -> datetime:
    m = TS_RE.match(ts_field)
    if m:
        try:
            raw = m.group(1)
            day, rest  = raw.split("/", 1)
            mon_str, rest2 = rest.split("/", 1)
            year, time_part = rest2.split(":", 1)
            h, mi, s = time_part.split(":")
            return datetime(
                int(year), MONTHS[mon_str], int(day),
                int(h), int(mi), int(s), tzinfo=timezone.utc,
            )
        except Exception:
            pass
    return datetime.now(timezone.utc)


# ── DB ────────────────────────────────────────────────────────────────────────

def connect_db() -> psycopg2.extensions.connection:
    while True:
        try:
            conn = psycopg2.connect(DB_URL)
            conn.autocommit = False
            log.info("Connected to logs database")
            return conn
        except Exception as e:
            log.warning("DB connection failed: %s — retrying in 5s", e)
            time.sleep(5)


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_downloads (
                id BIGSERIAL PRIMARY KEY,
                logged_at TIMESTAMPTZ NOT NULL,
                ip_address INET NOT NULL,
                username TEXT NOT NULL,
                filepath TEXT NOT NULL,
                filename TEXT NOT NULL,
                bytes BIGINT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS anon_downloads (
                id BIGSERIAL PRIMARY KEY,
                logged_at TIMESTAMPTZ NOT NULL,
                ip_address INET NOT NULL,
                filepath TEXT NOT NULL,
                filename TEXT NOT NULL,
                bytes BIGINT
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tailer_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        for k, v in [
            ("log_filename",          "full_user.log"),
            ("log_retention_days",    "90"),
            ("log_retention_enabled", "true"),
            ("tailer_status",         "starting"),
            ("tailer_last_write",     ""),
            ("tailer_pos",            "0"),
            ("tailer_total_rows",     "0"),
        ]:
            cur.execute(
                "INSERT INTO tailer_config (key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING",
                (k, v),
            )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ud_logged_at ON user_downloads(logged_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ud_username  ON user_downloads(username)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_logged_at ON anon_downloads(logged_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ad_ip        ON anon_downloads(ip_address)")
    conn.commit()
    log.info("Schema ready")


def get_config(conn, key: str, default: str = "") -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM tailer_config WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def set_config(conn, key: str, value: str):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tailer_config (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            (key, value),
        )


# ── File position ─────────────────────────────────────────────────────────────

def load_pos() -> int:
    try:
        with open(POS_PATH) as f:
            return int(f.read().strip())
    except Exception:
        return 0


def save_pos(pos: int):
    os.makedirs(os.path.dirname(POS_PATH), exist_ok=True)
    with open(POS_PATH, "w") as f:
        f.write(str(pos))


# ── Line parsing ──────────────────────────────────────────────────────────────

def parse_line(line: str) -> tuple | None:
    line = line.rstrip("\n\r")
    if not line:
        return None
    parts = line.split("|")
    if len(parts) != 7:
        return None
    ts_field, ip, user, filepath, command, status, raw_bytes = parts
    if command != "RETR" or status != "226":
        return None
    if not filepath or filepath == "-":
        return None

    logged_at = parse_timestamp(ts_field)
    filename  = os.path.basename(filepath)
    bytes_val = int(raw_bytes) if raw_bytes.isdigit() else None
    ip        = ip   if ip   and ip   != "-" else "0.0.0.0"
    user      = user if user and user != "-" else ANON_USER

    row = {"logged_at": logged_at, "ip_address": ip,
           "filepath": filepath, "filename": filename, "bytes": bytes_val}
    if user == ANON_USER:
        return ("anon_downloads", row)
    else:
        row["username"] = user
        return ("user_downloads", row)


# ── Main loop ─────────────────────────────────────────────────────────────────

def tail_loop():
    conn = connect_db()
    ensure_schema(conn)

    pos             = load_pos()
    last_reopen     = time.monotonic()
    last_config_poll = time.monotonic()
    fh              = None
    total_rows      = 0
    current_filename = get_config(conn, "log_filename", "full_user.log")
    current_log_path = os.path.join(LOG_DIR, current_filename)

    set_config(conn, "tailer_status", "running")
    conn.commit()

    log.info("Starting tail of %s from byte position %d", current_log_path, pos)

    while True:
        now = time.monotonic()

        # ── Poll for config changes (log filename / retention) ────────────────
        if now - last_config_poll >= CONFIG_POLL_INTERVAL:
            last_config_poll = now
            try:
                new_filename = get_config(conn, "log_filename", "full_user.log")
                if new_filename != current_filename:
                    log.info("Log filename changed: %s → %s", current_filename, new_filename)
                    current_filename = new_filename
                    current_log_path = os.path.join(LOG_DIR, new_filename)
                    if fh:
                        fh.close()
                        fh = None
                    pos = 0
                    save_pos(0)
            except Exception as e:
                log.warning("Config poll failed: %s", e)

        # ── Open / reopen file handle ─────────────────────────────────────────
        if fh is None or (now - last_reopen) >= REOPEN_INTERVAL:
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
            try:
                fh = open(current_log_path, "r", encoding="utf-8", errors="replace")
                current_size = os.fstat(fh.fileno()).st_size
                if pos > current_size:
                    log.info("File shrank (rotation?) — seeking to 0")
                    pos = 0
                fh.seek(pos)
                last_reopen = now
            except FileNotFoundError:
                log.warning("Log file not found: %s — retrying in 5s", current_log_path)
                fh = None
                time.sleep(5)
                continue

        # ── Read batch ────────────────────────────────────────────────────────
        user_rows, anon_rows = [], []
        lines_read = 0

        while lines_read < BATCH_SIZE * 10:
            line = fh.readline()
            if not line:
                break
            lines_read += 1
            result = parse_line(line)
            if result is None:
                continue
            table, row = result
            if table == "user_downloads":
                user_rows.append(row)
            else:
                anon_rows.append(row)

        if not user_rows and not anon_rows:
            new_pos = fh.tell()
            if new_pos != pos:
                pos = new_pos
                save_pos(pos)
            time.sleep(POLL_INTERVAL)
            continue

        # ── Commit to Postgres ────────────────────────────────────────────────
        try:
            with conn.cursor() as cur:
                if user_rows:
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO user_downloads "
                        "(logged_at,ip_address,username,filepath,filename,bytes) VALUES %s",
                        [(r["logged_at"],r["ip_address"],r["username"],
                          r["filepath"],r["filename"],r["bytes"]) for r in user_rows],
                    )
                if anon_rows:
                    psycopg2.extras.execute_values(
                        cur,
                        "INSERT INTO anon_downloads "
                        "(logged_at,ip_address,filepath,filename,bytes) VALUES %s",
                        [(r["logged_at"],r["ip_address"],
                          r["filepath"],r["filename"],r["bytes"]) for r in anon_rows],
                    )

            pos = fh.tell()
            total_rows += len(user_rows) + len(anon_rows)
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            set_config(conn, "tailer_status",     "running")
            set_config(conn, "tailer_last_write",  now_iso)
            set_config(conn, "tailer_pos",         str(pos))
            set_config(conn, "tailer_total_rows",  str(total_rows))

            conn.commit()
            save_pos(pos)

            log.info("Committed %d user + %d anon rows (total: %d, pos: %d)",
                     len(user_rows), len(anon_rows), total_rows, pos)

        except psycopg2.OperationalError as e:
            log.error("DB write error: %s — reconnecting", e)
            try:
                conn.close()
            except Exception:
                pass
            conn = connect_db()
        except Exception as e:
            log.error("Unexpected error during commit: %s", e)
            try:
                conn.rollback()
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DB_URL:
        raise RuntimeError("LOGS_DATABASE_URL environment variable is not set")
    if not os.path.isdir(LOG_DIR):
        log.warning("Log directory %s does not exist yet — waiting", LOG_DIR)
        for _ in range(30):
            if os.path.isdir(LOG_DIR):
                break
            time.sleep(2)

    tail_loop()
