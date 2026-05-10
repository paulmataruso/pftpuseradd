# FTP User Manager

A self-hosted, Dockerized web portal that lets users register their own accounts for a ProFTPd FTP server. Accounts are stored in PostgreSQL and synced automatically to ProFTPd's virtual user flat file (`ftpd.passwd`). Includes a full admin dashboard for managing users, resetting passwords, and reviewing audit logs.

---

## Features

- **Self-service registration** — users sign up with username, email, and password
- **ProFTPd virtual users** — no Linux system accounts created; all users map to a single FTP system user
- **Automatic sync** — `ftpd.passwd` is regenerated atomically on every account change
- **Admin dashboard** — enable/disable accounts, change passwords, update emails, add notes, view audit log
- **Rate limiting** — per-IP limits on registration and admin login
- **Reserved username blocklist** — prevents registration of system usernames (`root`, `admin`, `ftp`, etc.)
- **Security hardened** — bcrypt passwords, JWT sessions, CSP headers, isolated DB network, no API docs exposed

---

## Stack

| Container | Purpose |
|---|---|
| `nginx` | Reverse proxy, rate limiting, security headers |
| `frontend` | Static HTML/JS registration + admin UI |
| `backend` | FastAPI — REST API, business logic, ftpd.passwd writer |
| `db` | PostgreSQL 16 — user store and audit log |

---

## Requirements

- Docker + Docker Compose v2
- ProFTPd with `mod_auth_file` (compiled in — standard on Ubuntu/Debian packages)
- The Docker host must have read/write access to the ProFTPd config directory (typically `/etc/proftpd`)

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/ftp-user-manager.git
cd ftp-user-manager
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set all `CHANGEME` values. At minimum:

```env
POSTGRES_PASSWORD=a_strong_database_password
SECRET_KEY=<output of: openssl rand -hex 32>
ADMIN_PASSWORD=a_strong_admin_password
FTP_UID=<uid of your FTP system user>
FTP_GID=<gid of www-data or equivalent>
FTPD_PASSWD_DIR=/etc/proftpd
BIND_IP=127.0.0.1
LISTEN_PORT=8080
```

To get the correct UID/GID for your FTP system user:
```bash
id your_ftp_user
```

### 3. Configure ProFTPd

Drop the included config fragment on your FTP server:

```bash
sudo cp proftpd/virtualusers.conf /etc/proftpd/conf.d/virtualusers.conf
sudo proftpd --configtest   # verify no errors
sudo systemctl reload proftpd
```

This sets up:
- `DefaultRoot` to chroot all virtual users to your FTP root
- Read-only access everywhere except the upload directory
- Full deny on the upload directory for delete/rename operations

Edit `virtualusers.conf` to match your FTP root path before copying.

### 4. Start the stack

```bash
docker compose up -d
```

### 5. Verify

```bash
docker compose ps
curl http://127.0.0.1:8080/api/health
```

Access the web UI at `http://your-host:8080`

---

## ProFTPd Requirements

Your `proftpd.conf` must have these directives active:

```apache
AuthOrder          mod_auth_file.c mod_auth_unix.c
AuthUserFile       /etc/proftpd/ftpd.passwd
RequireValidShell  off
```

`mod_auth_file` is compiled into ProFTPd on standard Ubuntu/Debian packages — no `LoadModule` needed.

---

## Directory Structure

```
ftp-user-manager/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py          # FastAPI routes
│   ├── models.py        # SQLAlchemy models
│   ├── schemas.py       # Pydantic validation + reserved username list
│   ├── auth.py          # bcrypt + JWT
│   ├── ftpfile.py       # ftpd.passwd writer
│   ├── database.py      # DB connection
│   └── config.py        # Settings from environment
├── frontend/
│   ├── Dockerfile
│   └── index.html       # Single-file UI (registration + admin)
├── nginx/
│   └── nginx.conf       # Reverse proxy + security headers
├── db/
│   └── init.sql         # Schema (users + audit_log tables)
└── proftpd/
    └── virtualusers.conf # Drop-in ProFTPd config fragment
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `POSTGRES_DB` | Yes | Database name |
| `POSTGRES_USER` | Yes | Database username |
| `POSTGRES_PASSWORD` | Yes | Database password |
| `SECRET_KEY` | Yes | JWT signing secret — min 32 chars |
| `ADMIN_USERNAME` | Yes | Admin UI username |
| `ADMIN_PASSWORD` | Yes | Admin UI password |
| `FTP_UID` | Yes | UID of the FTP system user on the host |
| `FTP_GID` | Yes | GID (typically www-data or equivalent) |
| `FTPD_PASSWD_DIR` | Yes | Directory containing `ftpd.passwd` on the host |
| `ALLOWED_ORIGINS` | Yes | CORS origins — use `*` for testing, your domain in production |
| `BIND_IP` | Yes | IP address nginx binds to |
| `LISTEN_PORT` | Yes | Port nginx listens on |

---

## How Virtual Users Work

1. User registers via the web form
2. Password is hashed with bcrypt (compatible with ProFTPd's `mod_auth_file`)
3. Backend writes all enabled users to `ftpd.passwd` in the format:
   ```
   username:$2b$12$hash...:UID:GID:FTP User username:/:/sbin/nologin
   ```
4. ProFTPd reads this file on each authentication attempt — no reload required
5. All virtual users run as the FTP system user on the filesystem

---

## Reverse Proxy Setup (Nginx Proxy Manager)

Point your proxy host to `http://YOUR_HOST_IP:LISTEN_PORT`.

NPM will set `X-Real-IP` to the actual client IP automatically. The application uses this for rate limiting — spoofing `X-Forwarded-For` has no effect.

Enable HTTPS in NPM. Set `ALLOWED_ORIGINS` in `.env` to your public domain once TLS is active.

---

## Admin Dashboard

Navigate to the web UI and click **Admin** in the top navigation.

| Feature | Description |
|---|---|
| Dashboard | User count stats + recent registrations |
| All Users | Searchable/filterable user table |
| Manage User | Per-user page: change email, reset password, add notes, enable/disable, delete |
| Audit Log | Full history of all actions with timestamps and IP addresses |
| Sync passwd | Force-regenerate `ftpd.passwd` manually |

---

## Backup

```bash
# Database dump
docker compose exec db pg_dump -U $POSTGRES_USER $POSTGRES_DB > backup_$(date +%Y%m%d).sql

# Restore
docker compose exec -T db psql -U $POSTGRES_USER $POSTGRES_DB < backup.sql
```

---

## Updating

```bash
docker compose pull
docker compose up -d --build
```

---

## License

MIT
