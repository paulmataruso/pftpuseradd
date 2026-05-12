# Security Policy

## Supported Versions

Only the latest version receives security fixes.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report them privately via GitHub's Security Advisory feature (Security → Report a vulnerability) or by emailing the repository owner directly.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 72 hours.

---

## Security Design

### Authentication

- Admin credentials are stored in the database (hashed with bcrypt) and seeded from environment variables on first start only. After first start, credentials are managed exclusively via the admin UI.
- Admin credential checks use `hmac.compare_digest` to prevent timing attacks
- JWT tokens expire after 8 hours and are stored in `sessionStorage` (cleared on browser close)
- Admin login is rate-limited to 5 attempts per minute per IP at both the nginx and application layers
- API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled — no schema is exposed in production

### Passwords

- All FTP user passwords are hashed with bcrypt at work factor 12
- Bcrypt hashes are directly compatible with ProFTPd's `mod_auth_file` — no conversion needed
- Passwords are never stored, logged, or transmitted in plaintext
- The `ftpd.passwd` file is written atomically (write to temp, then rename) to avoid partial reads by ProFTPd
- User database exports include bcrypt hashes — these are safe to store offline as they cannot be reversed

### Network Isolation

Three separate Docker networks are used:

| Network | Members | Purpose |
|---|---|---|
| `internal` | nginx, frontend, backend | UI and API traffic |
| `db` | backend, db | User account database — no external routing |
| `db_logs` | backend, logtailer, db_logs | Activity log database — no external routing |

Neither database container is reachable from outside Docker. The `logtailer` container has no access to the `internal` network or the user database — it only touches `db_logs`.

### Input Validation

- Usernames validated against `^[a-zA-Z0-9_]{3,32}$` and a reserved name blocklist (Linux system users, FTP service accounts, common admin names)
- Request body size capped at 64KB at the nginx layer before reaching the application
- All database queries use parameterised inputs — no string interpolation in SQL
- Log filename changes validated server-side: must end in `.log`, no path separators or `..`

### Rate Limiting

Applied at two layers:

| Endpoint | nginx limit | Application limit |
|---|---|---|
| `/api/register` | 5 req/min | 5 req/min |
| `/api/admin/login` | 5 req/min | 5 req/min |
| All other `/api/*` | 30 req/min | — |

Rate limiting keys on the real client IP from `X-Real-IP` (set by NPM upstream). `X-Forwarded-For` spoofing has no effect.

### Security Headers

Set on all responses by the nginx container:

```
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; ...
```

**Note:** If running behind Nginx Proxy Manager, NPM's headers override the inner nginx container's headers. Set the CSP manually in NPM's Advanced tab — see README for the correct value.

### Log Tailer Security

- The logtailer mounts the ProFTPd log directory **read-only** (`ro` flag in docker-compose.yml)
- The logtailer has no access to the `internal` network or the user account database
- No FTP user passwords or credentials appear in the activity log — only usernames, IPs, and file paths
- The logtailer reads its target filename from `db_logs` — it cannot access or modify any other host path beyond the mounted log directory

### Static Assets

Chart.js is served from the local filesystem (`/static/chart.umd.min.js`) rather than a CDN. This means no external script sources are needed and the CSP does not need to whitelist any third-party domains for scripts.

---

## Known Limitations

- **Single admin account** — there is no multi-admin system. All admins share one credential set. Admin actions are attributed to "admin" in the audit log but cannot be attributed to individual people.
- **No email verification** — accounts are active immediately on registration. There is no confirmation email flow.
- **`last_login` is not populated** — the column exists in the schema but ProFTPd does not call back into the application on login. It will always show "Never" unless a custom ProFTPd `ExtendedLog` hook or `mod_exec` integration is added.
- **`'unsafe-inline'` in CSP** — required because the frontend is a single HTML file with inline scripts. Splitting JS into a separate file would allow this to be removed in a future version.
- **Log tailer processes only RETR 226** — only successful completed downloads are tracked. Uploads (STOR), failed transfers, directory listings, and partial transfers are not recorded. This is by design.
