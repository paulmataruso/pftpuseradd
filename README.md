# CIOS FTP User Manager

A self-hosted, fully Dockerized web portal for managing ProFTPd virtual users. Users self-register via a web form, and the system automatically syncs credentials to ProFTPd's virtual user file. An admin dashboard provides full user management, real-time download activity logging, and statistics charts.

---

## Features

- **Self-service registration** — users sign up with username, email, and password
- **Admin-created accounts** — admins can provision accounts directly without self-registration
- **ProFTPd virtual users** — no Linux system accounts; all users map to a single FTP system user
- **Automatic sync** — `ftpd.passwd` regenerated atomically on every account change
- **Admin dashboard** — enable/disable accounts, reset passwords, update emails, add notes
- **Audit log** — full history of every admin action with timestamps and IP addresses
- **Download activity logging** — tracks named-user and anonymous FTP downloads in real time
- **Statistics dashboard** — Chart.js charts with 1/7/30/90 day ranges, trend cards, top files/users
- **Log tailer** — dedicated container tails ProFTPd's ExtendedLog, writes to a separate PostgreSQL DB
- **Configurable log target** — switch which log file the tailer reads from the Settings panel, no restart needed
- **Log retention** — automatic nightly pruning with enable/disable toggle; keep logs forever if you want
- **CSV export** — download user or anonymous download logs as CSV with current filters applied
- **User database backup** — export all accounts (including hashed passwords) to JSON; import to restore
- **Admin credentials in DB** — change admin username/password from the UI; env vars only used on first start
- **Rate limiting** — per-IP limits at both nginx and application layers
- **Security hardened** — bcrypt passwords, JWT sessions, CSP headers, isolated DB networks

---

## Architecture

```
                    ┌─────────────────────────────────┐
Internet ──► NPM ──►│ nginx (reverse proxy)           │
                    │                                 │
                    │  ┌──────────┐  ┌─────────────┐ │
                    │  │ frontend │  │   backend   │ │
                    │  │ (nginx)  │  │  (FastAPI)  │ │
                    │  └──────────┘  └──────┬──────┘ │
                    │                       │        │
                    │          ┌────────────┴──────┐ │
                    │          │   db  │  db_logs  │ │
                    │          │ (PG)  │   (PG)    │ │
                    │          └───────┴───────────┘ │
                    └─────────────────────────────────┘
                                              ▲
                    ┌─────────────────────────┴───────┐
                    │  logtailer                      │
                    │  (tails log dir → db_logs)      │
                    └─────────────────────────────────┘
                              ▲
                    /var/log/proftpd/ (host, read-only)
```

| Container | Image | Purpose |
|---|---|---|
| `nginx` | `nginx:alpine` | Reverse proxy, rate limiting, security headers |
| `frontend` | Custom | Single-file HTML/JS registration + admin UI |
| `backend` | Custom | FastAPI REST API, business logic, ftpd.passwd writer |
| `db` | `postgres:16-alpine` | User accounts, audit log, and admin config |
| `db_logs` | `postgres:16-alpine` | FTP download activity (isolated from user DB) |
| `logtailer` | Custom | Tails ProFTPd log directory, writes downloads to db_logs |

---

## Requirements

- Docker Engine 24+ and Docker Compose v2
- ProFTPd installed on the host with `mod_auth_file` (standard on Ubuntu/Debian)
- The ProFTPd log must use the `custom_user` ExtendedLog format — see [ProFTPd Configuration](#proftpd-configuration)
- Host directories accessible to Docker:
  - `/etc/proftpd` (or wherever `ftpd.passwd` lives) — bind-mounted into `backend`
  - `/var/log/proftpd` (or your ProFTPd log directory) — bind-mounted read-only into `logtailer`

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/ciosuseradd.git
cd ciosuseradd
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env
```

Fill in all `CHANGEME` values. At minimum:

```bash
# Generate a strong JWT secret
openssl rand -hex 32

# Find your FTP system user's UID/GID
id anonftp
```

See the full [Environment Variables](#environment-variables) reference below.

### 3. Configure ProFTPd

See the [ProFTPd Configuration](#proftpd-configuration) section. Quick version:

```bash
# Add auth directives and logging to proftpd.conf
sudo nano /etc/proftpd/proftpd.conf

# Deploy the virtual users access config
sudo cp proftpd/virtualusers.conf /etc/proftpd/conf.d/virtualusers.conf
sudo nano /etc/proftpd/conf.d/virtualusers.conf   # set your FTP root path

# Test and reload
sudo proftpd --configtest
sudo systemctl reload proftpd
```

### 4. (Optional) Add a favicon

Drop your `favicon.ico` or `favicon.png` into `frontend/static/` before building. See `frontend/static/FAVICON_README.txt`.

### 5. Start the stack

```bash
docker compose up -d
```

### 6. Verify

```bash
docker compose ps
curl http://127.0.0.1:8222/api/health
```

All containers should show `healthy` or `running`. Access the web UI at `http://YOUR_HOST_IP:LISTEN_PORT`.

---

## ProFTPd Configuration

### Overview

This system integrates with ProFTPd in two ways:

1. **Virtual user authentication** — the backend writes `ftpd.passwd`; ProFTPd reads it to authenticate FTP users
2. **Download activity tracking** — the logtailer reads ProFTPd's ExtendedLog to record what files are downloaded

Both require specific ProFTPd configuration.

### Step 1 — Add required directives to proftpd.conf

Edit `/etc/proftpd/proftpd.conf` and add the following. Merge carefully if directives already exist.

```apache
# ── Virtual user authentication ───────────────────────────────────────────────
# Check the virtual user file first, fall back to system accounts.
# mod_auth_file is built into standard Ubuntu/Debian ProFTPd packages.
AuthOrder          mod_auth_file.c mod_auth_unix.c

# Path to the virtual user file. Must match FTPD_PASSWD_DIR in .env + /ftpd.passwd
AuthUserFile       /etc/proftpd/ftpd.passwd

# Virtual users don't have entries in /etc/shells — disable this check
RequireValidShell  off

# ── TLS — strongly recommended ────────────────────────────────────────────────
# Without TLS, FTP passwords are sent in cleartext over the network.
<IfModule mod_tls.c>
  TLSEngine                on
  TLSLog                   /var/log/proftpd/tls.log
  TLSProtocol              TLSv1.2 TLSv1.3
  TLSRSACertificateFile    /etc/letsencrypt/live/yourdomain.com/fullchain.pem
  TLSRSACertificateKeyFile /etc/letsencrypt/live/yourdomain.com/privkey.pem
  TLSVerifyClient          off
  TLSRequired              on
</IfModule>

# ── Activity logging — required for download tracking ─────────────────────────
# The custom_user format includes the authenticated username (%u).
# This is what separates named-user downloads from anonymous ones.
<IfModule mod_log.c>
  LogFormat custom      "%t|%a|%f|%m|%s|%b"
  LogFormat custom_user "%t|%a|%u|%f|%m|%s|%b"

  ExtendedLog /var/log/proftpd/full.log      ALL custom
  ExtendedLog /var/log/proftpd/full_user.log ALL custom_user
</IfModule>
```

Log format field reference:

| Field | Meaning | Example |
|---|---|---|
| `%t` | Timestamp | `[11/May/2026:18:04:13 +0000]` |
| `%a` | Client IP address | `140.235.237.1` |
| `%u` | Authenticated username | `testuser` or `anonftp` or `-` |
| `%f` | File path | `/mnt/ftp/3COM/file.tar.gz` |
| `%m` | FTP command | `RETR`, `STOR`, `MLSD`, etc. |
| `%s` | Response status code | `226` = transfer complete |
| `%b` | Bytes transferred | `6139031` |

### Step 2 — Deploy the virtual users config fragment

```bash
sudo cp proftpd/virtualusers.conf /etc/proftpd/conf.d/virtualusers.conf
sudo nano /etc/proftpd/conf.d/virtualusers.conf
```

Replace every occurrence of `/path/to/your/ftp/root` with your actual FTP root directory. The config sets up:

- `DefaultRoot` — chroots all virtual users to the FTP root
- Root directory — read and list only, no writes
- `upload/` subdirectory — uploads allowed, delete/rename denied

### Step 3 — Test and reload

```bash
sudo proftpd --configtest
sudo systemctl reload proftpd
```

### Step 4 — Verify

```bash
# Watch the log during a test FTP connection
tail -f /var/log/proftpd/full_user.log

# Named user download looks like:
# [11/May/2026:18:04:13 +0000]|140.235.237.1|testuser|/mnt/ftp/file.tar.gz|RETR|226|6139031

# Anonymous download looks like:
# [11/May/2026:18:04:13 +0000]|176.32.245.221|anonftp|/mnt/ftp/Axis/file.zip|RETR|226|897
```

### Understanding download tracking

The logtailer routes log lines to one of two tables based on the username field:

| Username in log | Routed to | Visible in panel |
|---|---|---|
| `anonftp` (configurable) | `anon_downloads` | Anon Downloads |
| Any other authenticated username | `user_downloads` | User Downloads |
| `-` (not yet authenticated) | Ignored | — |

Only `RETR` commands with status `226` (successful transfer complete) are recorded. Auth events, directory listings, failed transfers, and uploads are all ignored.

If your anonymous user has a different username than `anonftp`, update `ANON_USER` at the top of `logtailer/tailer.py` and rebuild:

```bash
docker compose up -d --build logtailer
```

---

## Download Activity Tracking

### Changing the active log file

The filename the logtailer reads defaults to `full_user.log` and is stored in the `db_logs` database. Change it from the **Settings** panel in the admin UI — the change takes effect within 10 seconds with no restart needed.

### Log retention

Records older than the configured retention period are deleted nightly. Default is 90 days. The retention can be **disabled entirely** from the Settings panel if you want logs kept indefinitely. Re-enabling it applies the configured day limit going forward.

### Backfilling historical data

On first start the logtailer begins from the current end of the log — it does not reprocess old entries. To backfill:

```bash
docker compose stop logtailer
docker run --rm -v ciosuseradd_tailer_pos:/pos alpine rm -f /pos/tailer.pos
docker compose start logtailer
docker compose logs -f logtailer
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in all values.

### Main database (user accounts)

| Variable | Example | Description |
|---|---|---|
| `POSTGRES_DB` | `ciosuseradd` | Database name |
| `POSTGRES_USER` | `ciosadmin` | Database username |
| `POSTGRES_PASSWORD` | — | Strong password |

### Activity log database (download tracking)

| Variable | Example | Description |
|---|---|---|
| `LOGS_POSTGRES_DB` | `ftplogs` | Database name |
| `LOGS_POSTGRES_USER` | `logsadmin` | Database username |
| `LOGS_POSTGRES_PASSWORD` | — | Strong password (different from main DB) |

### Application

| Variable | Example | Description |
|---|---|---|
| `SECRET_KEY` | `openssl rand -hex 32` | JWT signing key — must be 32+ chars |
| `ADMIN_USERNAME` | `admin` | Initial admin username — **only used on first start** |
| `ADMIN_PASSWORD` | — | Initial admin password — **only used on first start** |

> After first start, admin credentials are stored in the database and managed exclusively via the **Settings** panel. The env vars are ignored on subsequent starts.

### ProFTPd integration

| Variable | Example | Description |
|---|---|---|
| `FTP_UID` | `1001` | UID of the FTP system user on the host — `id anonftp` |
| `FTP_GID` | `33` | GID (typically www-data = 33) |
| `FTPD_PASSWD_DIR` | `/etc/proftpd` | Directory on the host containing `ftpd.passwd` |
| `FTP_LOG_DIR` | `/var/log/proftpd` | ProFTPd log **directory** on the host (not a single file) |

### Network

| Variable | Example | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | `https://ftp.yourdomain.com` | CORS origins — `*` for testing only |
| `BIND_IP` | `172.16.1.15` | IP nginx binds to |
| `LISTEN_PORT` | `8222` | Port nginx listens on |

---

## Reverse Proxy Setup (Nginx Proxy Manager)

Point your NPM proxy host to `http://HOST_IP:LISTEN_PORT`.

**Important:** NPM adds its own security headers which override the inner nginx container's headers. If you see Content Security Policy errors in the browser console, add this in NPM's **Advanced** tab for the proxy host:

```nginx
add_header Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self';" always;
```

Enable HTTPS in NPM. Once TLS is active, set `ALLOWED_ORIGINS` to your public HTTPS domain and restart the backend:

```bash
docker compose up -d backend
```

---

## Admin Dashboard

| Section | Description |
|---|---|
| **Dashboard** | User count stats and recent registrations |
| **All Users** | Searchable/filterable user table with quick enable/disable and Add User |
| **User Detail** | Per-user: email, password, notes, enable/disable, delete |
| **Audit Log** | Full history of all admin actions with timestamps and IPs |
| **User Downloads** | Paginated named-user RETR events — filter by user, IP, file, date |
| **Anon Downloads** | Paginated anonymous RETR events — filter by IP, file, date |
| **Charts & Stats** | Line/bar charts with 1d/7d/30d/90d range selector and trend cards |
| **Settings** | Admin credentials, log file target, retention toggle + days, user import/export, CSV export |

---

## User Database Backup & Restore

### Export

In the **Settings** panel, click **Export Users JSON**. The downloaded file contains every user account including the bcrypt password hash — users will not need to reset their passwords after a restore.

### Import

Click **Import Users JSON** and select a previously exported file. Import is **merge mode** — any username or email that already exists in the database is skipped. New users are inserted and `ftpd.passwd` is regenerated automatically.

The import result shows exactly how many accounts were imported, skipped, and errored.

### Manual restore (full wipe)

To completely replace the database with a backup:

```bash
# Stop everything
docker compose down

# Remove the user DB volume
docker volume rm ciosuseradd_db_data

# Start fresh — schema is recreated automatically
docker compose up -d

# Import your export file via the Settings panel
```

---

## Directory Structure

```
ciosuseradd/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
├── SECURITY.md
├── CHANGELOG.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py          # FastAPI routes (user mgmt, log queries, settings, import/export)
│   ├── models.py        # SQLAlchemy models (users, audit_log, admin_config)
│   ├── schemas.py       # Pydantic validation, reserved username list, response schemas
│   ├── auth.py          # bcrypt hashing, JWT, DB-backed admin credentials
│   ├── ftpfile.py       # Atomic ftpd.passwd writer
│   ├── logs.py          # Read-only queries against db_logs + retention + CSV export
│   ├── database.py      # SQLAlchemy engine + session
│   └── config.py        # Pydantic-settings from environment
├── frontend/
│   ├── Dockerfile
│   ├── index.html       # Single-file UI (registration + full admin dashboard)
│   └── static/
│       ├── chart.umd.min.js   # Chart.js served locally (no CDN dependency)
│       ├── favicon.ico        # Browser tab icon (replace with your own)
│       ├── favicon.png        # Favicon fallback + in-page logo
│       └── FAVICON_README.txt # Instructions for swapping the favicon
├── logtailer/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── tailer.py        # Tails log dir, polls DB for config changes, writes status back
├── nginx/
│   └── nginx.conf       # Reverse proxy, rate limiting, security headers
├── db/
│   ├── init.sql         # Main DB schema (users, audit_log, admin_config)
│   └── init_logs.sql    # Log DB schema (user_downloads, anon_downloads, tailer_config)
└── proftpd/
    └── virtualusers.conf # Drop-in ProFTPd config — edit FTP root path before deploying
```

---

## Backup

### User database

```bash
# Dump
docker compose exec db pg_dump -U $POSTGRES_USER $POSTGRES_DB > backup_users_$(date +%Y%m%d).sql

# Restore
docker compose exec -T db psql -U $POSTGRES_USER $POSTGRES_DB < backup_users.sql
```

### Activity log database

```bash
# Dump
docker compose exec db_logs pg_dump -U $LOGS_POSTGRES_USER $LOGS_POSTGRES_DB > backup_logs_$(date +%Y%m%d).sql

# Restore
docker compose exec -T db_logs psql -U $LOGS_POSTGRES_USER $LOGS_POSTGRES_DB < backup_logs.sql
```

---

## Updating

```bash
git pull
docker compose up -d --build
```

Schema migrations are not automated. Check the [CHANGELOG](CHANGELOG.md) for any manual steps required between versions.

---

## Troubleshooting

**ftpd.passwd not being written**
```bash
docker compose logs backend | grep -i passwd
docker compose exec backend ls -la /ftpshared/
```

**Logtailer not picking up downloads**
```bash
docker compose logs logtailer
# Verify the log directory is mounted
docker compose exec logtailer ls -la /logs/
# Check the log format — must be 7 pipe-delimited fields
tail -5 /var/log/proftpd/full_user.log
# Check what filename the tailer is configured to read
docker compose exec db_logs psql -U $LOGS_POSTGRES_USER -d $LOGS_POSTGRES_DB \
  -c "SELECT * FROM tailer_config;"
```

**Admin credentials not working**
```bash
# Check admin_config was seeded on first start
docker compose exec db psql -U $POSTGRES_USER -d $POSTGRES_DB \
  -c "SELECT key, updated_at FROM admin_config;"
```

**Charts not loading / CSP errors in browser**
Add the CSP header manually in NPM's Advanced tab — see [Reverse Proxy Setup](#reverse-proxy-setup-nginx-proxy-manager).

**db_logs not connecting**
```bash
docker compose logs db_logs
docker compose logs logtailer | grep -i "connect\|error"
```

---

## License

MIT
