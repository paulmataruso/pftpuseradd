import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import func
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from database import engine, get_db, Base
from models import User, AuditLog
from schemas import (
    RegisterRequest, AdminLoginRequest, AdminCreateUserRequest,
    UserResponse, UserListResponse,
    UpdateNotesRequest, UpdateEmailRequest, ChangePasswordRequest,
    AdminChangePasswordRequest, AdminChangeUsernameRequest,
    LogSettingsRequest, SystemSettingsResponse,
    MessageResponse, TokenResponse, AuditLogEntry, SyncResponse,
    UserDownloadPage, AnonDownloadPage,
    UserDownloadStats, AnonDownloadStats, SummaryResponse,
)
from auth import (
    hash_password, verify_admin, create_access_token, verify_token,
    seed_admin_config, change_admin_password, change_admin_username,
    get_admin_username,
)
from ftpfile import regenerate_ftpd_passwd
from config import settings
import logs as ftplogs

logger = logging.getLogger("uvicorn.error")


# ── Startup / shutdown ────────────────────────────────────────────────────────

def validate_config():
    if len(settings.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters.")
    import os
    passwd_dir = os.path.dirname(settings.ftpd_passwd_path)
    if not os.path.isdir(passwd_dir):
        raise RuntimeError(f"FTPD_PASSWD_PATH directory does not exist: {passwd_dir}")


async def _daily_prune():
    """Background task — prunes old log records once per day."""
    while True:
        await asyncio.sleep(86400)
        try:
            ud, ad = ftplogs.prune_old_records()
            logger.info(f"Log retention pruning: removed {ud} user + {ad} anon rows")
        except Exception as e:
            logger.error(f"Log retention pruning failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    Base.metadata.create_all(bind=engine)
    # Seed admin credentials from env on first start
    db = next(get_db())
    try:
        seed_admin_config(db)
    finally:
        db.close()
    # Start background pruning task
    task = asyncio.create_task(_daily_prune())
    yield
    task.cancel()


# ── App setup ─────────────────────────────────────────────────────────────────

def get_real_ip(request: Request) -> str:
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=get_real_ip)

app = FastAPI(
    title="FTP User Manager",
    lifespan=lifespan,
    docs_url=None, redoc_url=None, openapi_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=False,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/admin/login", auto_error=False)


def get_current_admin(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sub = verify_token(token)
    if not sub or sub != "admin":
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return sub


def audit(db: Session, username: Optional[str], action: str,
          detail: Optional[str] = None, ip: Optional[str] = None):
    db.add(AuditLog(username=username, action=action, detail=detail, ip_address=ip))
    db.commit()


def get_user_or_404(user_id: str, db: Session) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def sync(db: Session):
    try:
        regenerate_ftpd_passwd(db)
    except Exception as e:
        logger.error(f"ftpd.passwd regeneration failed: {e}")


def _logs_err(e: Exception):
    raise HTTPException(status_code=503, detail=f"Activity log database unavailable: {e}")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Public registration ───────────────────────────────────────────────────────

@app.post("/register", response_model=MessageResponse, status_code=201)
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    ip = get_real_ip(request)
    if db.query(User).filter(User.username == body.username).first():
        audit(db, body.username, "register_fail", "username taken", ip)
        raise HTTPException(status_code=409, detail="Username already taken")
    if db.query(User).filter(User.email == body.email).first():
        audit(db, body.username, "register_fail", "email taken", ip)
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        username=body.username, email=body.email,
        password_hash=hash_password(body.password), enabled=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    sync(db)
    audit(db, body.username, "register", "account created", ip)
    return {"message": "Account created successfully. You can now log in to the FTP server."}


# ── Admin auth ────────────────────────────────────────────────────────────────

@app.post("/admin/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def admin_login(request: Request, body: AdminLoginRequest, db: Session = Depends(get_db)):
    ip = get_real_ip(request)
    if not verify_admin(body.username, body.password, db):
        audit(db, body.username[:64], "admin_login_fail", None, ip)
        secrets.token_bytes(32)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": "admin"})
    audit(db, body.username, "admin_login", None, ip)
    return {"access_token": token}


# ── Admin — user management ───────────────────────────────────────────────────

@app.get("/admin/stats")
def get_stats(db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    total    = db.query(func.count(User.id)).scalar()
    enabled  = db.query(func.count(User.id)).filter(User.enabled == True).scalar()
    disabled = db.query(func.count(User.id)).filter(User.enabled == False).scalar()
    return {"total": total, "enabled": enabled, "disabled": disabled}


@app.post("/admin/sync", response_model=SyncResponse)
def force_sync(db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    count = regenerate_ftpd_passwd(db)
    audit(db, "admin", "manual_sync", f"{count} users written")
    return {"message": "ftpd.passwd regenerated", "users_written": count}


@app.get("/admin/audit", response_model=list[AuditLogEntry])
def get_audit_log(
    limit: int = 200, db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    limit = min(limit, 500)
    return db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()


@app.get("/admin/users", response_model=UserListResponse)
def list_users(db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return {"users": users, "total": len(users)}


@app.post("/admin/users", response_model=UserResponse, status_code=201)
def admin_create_user(
    body: AdminCreateUserRequest,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="Username already taken")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        username=body.username, email=body.email,
        password_hash=hash_password(body.password), enabled=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    sync(db)
    audit(db, body.username, "admin_create", "account created by admin")
    return user


@app.get("/admin/users/{user_id}", response_model=UserResponse)
def get_user(user_id: str, db: Session = Depends(get_db),
             _: str = Depends(get_current_admin)):
    return get_user_or_404(user_id, db)


@app.put("/admin/users/{user_id}/toggle", response_model=MessageResponse)
def toggle_user(user_id: str, db: Session = Depends(get_db),
                _: str = Depends(get_current_admin)):
    user = get_user_or_404(user_id, db)
    user.enabled = not user.enabled
    db.commit()
    sync(db)
    action = "enabled" if user.enabled else "disabled"
    audit(db, user.username, f"admin_{action}", "by admin")
    return {"message": f"User {user.username} {action}"}


@app.put("/admin/users/{user_id}/password", response_model=MessageResponse)
def change_password(
    user_id: str, body: ChangePasswordRequest,
    db: Session = Depends(get_db), _: str = Depends(get_current_admin),
):
    user = get_user_or_404(user_id, db)
    user.password_hash = hash_password(body.password)
    db.commit()
    sync(db)
    audit(db, user.username, "admin_password_change", "password changed by admin")
    return {"message": f"Password updated for {user.username}"}


@app.put("/admin/users/{user_id}/email", response_model=MessageResponse)
def change_email(
    user_id: str, body: UpdateEmailRequest,
    db: Session = Depends(get_db), _: str = Depends(get_current_admin),
):
    user = get_user_or_404(user_id, db)
    existing = db.query(User).filter(User.email == body.email,
                                     User.id != user_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already in use")
    old = user.email
    user.email = body.email
    db.commit()
    audit(db, user.username, "admin_email_change", f"{old} → {body.email}")
    return {"message": f"Email updated for {user.username}"}


@app.put("/admin/users/{user_id}/notes", response_model=MessageResponse)
def update_notes(
    user_id: str, body: UpdateNotesRequest,
    db: Session = Depends(get_db), _: str = Depends(get_current_admin),
):
    user = get_user_or_404(user_id, db)
    user.notes = body.notes
    db.commit()
    return {"message": "Notes updated"}


@app.delete("/admin/users/{user_id}", response_model=MessageResponse)
def delete_user(user_id: str, db: Session = Depends(get_db),
                _: str = Depends(get_current_admin)):
    user = get_user_or_404(user_id, db)
    username = user.username
    db.delete(user)
    db.commit()
    sync(db)
    audit(db, username, "admin_delete", "account deleted by admin")
    return {"message": f"User {username} deleted"}


@app.get("/admin/users/{user_id}/downloads")
def user_download_history(
    user_id: str, limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db), _: str = Depends(get_current_admin),
):
    user = get_user_or_404(user_id, db)
    try:
        return ftplogs.get_user_downloads_for_user(user.username, limit=limit)
    except Exception as e:
        _logs_err(e)


# ── Admin — user import/export ───────────────────────────────────────────────

@app.get("/admin/users/export")
def export_users(
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    users = db.query(User).order_by(User.created_at).all()
    data = [{
        "id":            str(u.id),
        "username":      u.username,
        "email":         u.email,
        "password_hash": u.password_hash,
        "enabled":       u.enabled,
        "created_at":    u.created_at.isoformat() if u.created_at else None,
        "last_login":    u.last_login.isoformat() if u.last_login else None,
        "notes":         u.notes,
    } for u in users]
    import json
    from fastapi.responses import Response
    payload = json.dumps({"version": 1, "exported_at": __import__('datetime').datetime.utcnow().isoformat(), "users": data}, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=users_export.json"},
    )


@app.post("/admin/users/import", response_model=MessageResponse)
async def import_users(
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    import json
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    users_data = body.get("users") if isinstance(body, dict) else body
    if not isinstance(users_data, list):
        raise HTTPException(status_code=400, detail="Expected a list of users or {\"users\": [...]}")

    imported = skipped = errors = 0
    for row in users_data:
        try:
            username = str(row.get("username", "")).strip().lower()
            email    = str(row.get("email", "")).strip()
            pw_hash  = str(row.get("password_hash", "")).strip()
            if not username or not email or not pw_hash:
                errors += 1
                continue
            # Skip if username or email already exists
            if db.query(User).filter(
                (User.username == username) | (User.email == email)
            ).first():
                skipped += 1
                continue
            from datetime import datetime, timezone
            def _parse_dt(v):
                if not v: return None
                try: return datetime.fromisoformat(v.replace("Z", "+00:00"))
                except Exception: return None
            user = User(
                username      = username,
                email         = email,
                password_hash = pw_hash,
                enabled       = bool(row.get("enabled", True)),
                notes         = row.get("notes"),
                created_at    = _parse_dt(row.get("created_at")),
                last_login    = _parse_dt(row.get("last_login")),
            )
            db.add(user)
            imported += 1
        except Exception:
            errors += 1
            continue

    db.commit()
    if imported > 0:
        sync(db)
    audit(db, "admin", "user_import",
          f"imported={imported}, skipped={skipped}, errors={errors}",
          get_real_ip(request))
    return {"message": f"Import complete — {imported} imported, {skipped} skipped (already exist), {errors} errors"}


# ── Admin — settings ──────────────────────────────────────────────────────────

@app.get("/admin/settings", response_model=SystemSettingsResponse)
def get_settings(db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    try:
        status = ftplogs.get_system_status()
    except Exception:
        status = {
            "log_filename": "unavailable", "log_retention_days": 90,
            "tailer_status": "unavailable", "tailer_last_write": "",
            "tailer_pos": 0, "tailer_total_rows": 0,
        }
    return {
        "admin_username":    get_admin_username(db),
        **status,
    }


@app.put("/admin/settings/password", response_model=MessageResponse)
def admin_change_password(
    body: AdminChangePasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    admin_user = get_admin_username(db)
    if not verify_admin(admin_user, body.current_password, db):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    change_admin_password(body.new_password, db)
    audit(db, "admin", "admin_password_change", "admin changed their own password",
          get_real_ip(request))
    return {"message": "Admin password updated successfully"}


@app.put("/admin/settings/username", response_model=MessageResponse)
def admin_change_username(
    body: AdminChangeUsernameRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    old = get_admin_username(db)
    change_admin_username(body.new_username, db)
    audit(db, "admin", "admin_username_change", f"{old} → {body.new_username}",
          get_real_ip(request))
    return {"message": f"Admin username changed to {body.new_username}"}


@app.put("/admin/settings/logs", response_model=MessageResponse)
def update_log_settings(
    body: LogSettingsRequest,
    request: Request,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    try:
        ftplogs.update_log_settings(body.log_filename, body.log_retention_days,
                                    body.log_retention_enabled)
    except Exception as e:
        _logs_err(e)
    parts = []
    if body.log_filename:
        parts.append(f"log_filename={body.log_filename}")
    if body.log_retention_days:
        parts.append(f"retention={body.log_retention_days}d")
    audit(db, "admin", "log_settings_change", ", ".join(parts), get_real_ip(request))
    return {"message": "Log settings updated"}


# ── Admin — FTP activity: paginated tables ────────────────────────────────────

@app.get("/admin/logs/users", response_model=UserDownloadPage)
def log_user_downloads(
    page: int = Query(1, ge=1), limit: int = Query(100, ge=1, le=500),
    username: Optional[str] = Query(None), ip: Optional[str] = Query(None),
    filename: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None), date_to: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_user_downloads(page=page, limit=limit, username=username,
                                          ip=ip, filename=filename,
                                          date_from=date_from, date_to=date_to)
    except Exception as e:
        _logs_err(e)


@app.get("/admin/logs/users/stats", response_model=UserDownloadStats)
def log_user_stats(
    date_from: Optional[str] = Query(None), date_to: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_user_download_stats(date_from=date_from, date_to=date_to)
    except Exception as e:
        _logs_err(e)


@app.get("/admin/logs/users/export")
def export_user_downloads(
    username: Optional[str] = Query(None), ip: Optional[str] = Query(None),
    filename: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None), date_to: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
):
    try:
        csv_data = ftplogs.export_user_downloads_csv(
            username=username, ip=ip, filename=filename,
            date_from=date_from, date_to=date_to)
    except Exception as e:
        _logs_err(e)
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=user_downloads.csv"},
    )


@app.get("/admin/logs/anon", response_model=AnonDownloadPage)
def log_anon_downloads(
    page: int = Query(1, ge=1), limit: int = Query(100, ge=1, le=500),
    ip: Optional[str] = Query(None), filename: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None), date_to: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_anon_downloads(page=page, limit=limit, ip=ip,
                                          filename=filename,
                                          date_from=date_from, date_to=date_to)
    except Exception as e:
        _logs_err(e)


@app.get("/admin/logs/anon/stats", response_model=AnonDownloadStats)
def log_anon_stats(
    date_from: Optional[str] = Query(None), date_to: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_anon_download_stats(date_from=date_from, date_to=date_to)
    except Exception as e:
        _logs_err(e)


@app.get("/admin/logs/anon/export")
def export_anon_downloads(
    ip: Optional[str] = Query(None), filename: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None), date_to: Optional[str] = Query(None),
    _: str = Depends(get_current_admin),
):
    try:
        csv_data = ftplogs.export_anon_downloads_csv(
            ip=ip, filename=filename, date_from=date_from, date_to=date_to)
    except Exception as e:
        _logs_err(e)
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=anon_downloads.csv"},
    )


# ── Admin — FTP activity: time-series & dashboard ─────────────────────────────

@app.get("/admin/logs/summary", response_model=SummaryResponse)
def log_summary(
    days: int = Query(30, ge=1, le=365), _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_summary(days=days)
    except Exception as e:
        _logs_err(e)


@app.get("/admin/logs/users/timeline")
def log_user_timeline(
    days: int = Query(30, ge=1, le=365),
    bucket: str = Query("day", pattern="^(hour|day|week)$"),
    _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_user_download_timeline(days=days, bucket=bucket)
    except Exception as e:
        _logs_err(e)


@app.get("/admin/logs/anon/timeline")
def log_anon_timeline(
    days: int = Query(30, ge=1, le=365),
    bucket: str = Query("day", pattern="^(hour|day|week)$"),
    _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_anon_download_timeline(days=days, bucket=bucket)
    except Exception as e:
        _logs_err(e)


@app.get("/admin/logs/users/breakdown")
def log_user_breakdown(
    days: int = Query(30, ge=1, le=365), _: str = Depends(get_current_admin),
):
    try:
        return ftplogs.get_user_download_breakdown(days=days)
    except Exception as e:
        _logs_err(e)
