# Security Policy

## Supported Versions

Only the latest version receives security fixes.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report them privately via GitHub's Security Advisory feature (Security → Report a vulnerability) or by emailing the repository owner directly.

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 72 hours.

## Security Design Notes

### Authentication
- Admin credentials are compared using `hmac.compare_digest` to prevent timing attacks
- JWT tokens expire after 8 hours
- Admin login is rate-limited to 5 attempts per minute per IP
- API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled in production

### Passwords
- All user passwords are hashed with bcrypt (work factor 12)
- Passwords are never stored or logged in plaintext
- The `ftpd.passwd` file is written `chmod 640`

### Network
- The PostgreSQL container is on an isolated internal Docker network with no external routing
- nginx only binds to the configured `BIND_IP` — not `0.0.0.0` by default
- All requests behind a reverse proxy use `X-Real-IP` for rate limiting — `X-Forwarded-For` is overwritten before reaching the backend

### Input Validation
- Usernames are validated against a strict regex and a reserved name blocklist
- Request body size is capped at 64KB at the nginx layer
- Rate limiting is applied per real client IP at the nginx layer before requests reach the application

### Headers
The following security headers are set on all responses:
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()`
- `Content-Security-Policy` (restricts script/style/font sources)

### Known Limitations
- The admin interface uses a single shared credential (no multi-user admin accounts)
- Sessions are stored in `sessionStorage` — cleared on browser close but accessible to same-origin JavaScript
- No email verification on registration — all accounts are active immediately
- `'unsafe-inline'` is present in the CSP for scripts due to the single-file frontend architecture
