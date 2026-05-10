import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import func
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from database import engine, get_db, Base
from models import User, AuditLog
from schemas import (
    RegisterRequest, AdminLoginRequest, UserResponse, UserListResponse,
    UpdateNotesRequest, UpdateEmailRequest, ChangePasswordRequest,
    MessageResponse, TokenResponse, AuditLogEntry, SyncResponse
)
from auth import hash_password, verify_admin, create_access_token, verify_token
from ftpfile import regenerate_ftpd_passwd
from config import settings

logger = logging.getLogger("uvicorn.error")


def validate_config():
    if len(settings.secret_key) < 32:
        raise RuntimeError("SECRET_KEY must be at least 32 characters. Run: openssl rand -hex 32")
    if settings.admin_password in ("changeme", "password", "admin", ""):
        raise RuntimeError("ADMIN_PASSWORD is insecure. Set a strong password in .env")
    import os
    passwd_dir = os.path.dirname(settings.ftpd_passwd_path)
    if not os.path.isdir(passwd_dir):
        raise RuntimeError(f"FTPD_PASSWD_PATH directory does not exist: {passwd_dir}")


def get_real_ip(request: Request) -> str:
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=get_real_ip)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="FTP User Manager",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
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


def client_ip(request: Request) -> str:
    return get_real_ip(request)


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


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/register", response_model=MessageResponse, status_code=201)
@limiter.limit("5/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    ip = client_ip(request)
    if db.query(User).filter(User.username == body.username).first():
        audit(db, body.username, "register_fail", "username taken", ip)
        raise HTTPException(status_code=409, detail="Username already taken")
    if db.query(User).filter(User.email == body.email).first():
        audit(db, body.username, "register_fail", "email taken", ip)
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        enabled=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    sync(db)
    audit(db, body.username, "register", "account created", ip)
    return {"message": "Account created successfully. You can now log in to the FTP server."}


@app.post("/admin/login", response_model=TokenResponse)
@limiter.limit("5/minute")
def admin_login(request: Request, body: AdminLoginRequest, db: Session = Depends(get_db)):
    ip = client_ip(request)
    valid = verify_admin(body.username, body.password)
    if not valid:
        audit(db, body.username[:64], "admin_login_fail", None, ip)
        secrets.token_bytes(32)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token({"sub": "admin"})
    audit(db, body.username, "admin_login", None, ip)
    return {"access_token": token}


@app.get("/admin/stats")
def get_stats(db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    total    = db.query(func.count(User.id)).scalar()
    enabled  = db.query(func.count(User.id)).filter(User.enabled == True).scalar()
    disabled = db.query(func.count(User.id)).filter(User.enabled == False).scalar()
    return {"total": total, "enabled": enabled, "disabled": disabled}


@app.post("/admin/sync", response_model=SyncResponse)
def force_sync(db: Session = Depends(get_db), admin: str = Depends(get_current_admin)):
    count = regenerate_ftpd_passwd(db)
    audit(db, "admin", "manual_sync", f"{count} users written", None)
    return {"message": "ftpd.passwd regenerated", "users_written": count}


@app.get("/admin/audit", response_model=list[AuditLogEntry])
def get_audit_log(
    limit: int = 200,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    limit = min(limit, 500)
    return db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).all()


@app.get("/admin/users", response_model=UserListResponse)
def list_users(db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    users = db.query(User).order_by(User.created_at.desc()).all()
    return {"users": users, "total": len(users)}


@app.get("/admin/users/{user_id}", response_model=UserResponse)
def get_user(user_id: str, db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    return get_user_or_404(user_id, db)


@app.put("/admin/users/{user_id}/toggle", response_model=MessageResponse)
def toggle_user(user_id: str, db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    user = get_user_or_404(user_id, db)
    user.enabled = not user.enabled
    db.commit()
    sync(db)
    action = "enabled" if user.enabled else "disabled"
    audit(db, user.username, f"admin_{action}", "by admin")
    return {"message": f"User {user.username} {action}"}


@app.put("/admin/users/{user_id}/password", response_model=MessageResponse)
def change_password(
    user_id: str,
    body: ChangePasswordRequest,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    user = get_user_or_404(user_id, db)
    user.password_hash = hash_password(body.password)
    db.commit()
    sync(db)
    audit(db, user.username, "admin_password_change", "password changed by admin")
    return {"message": f"Password updated for {user.username}"}


@app.put("/admin/users/{user_id}/email", response_model=MessageResponse)
def change_email(
    user_id: str,
    body: UpdateEmailRequest,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    user = get_user_or_404(user_id, db)
    existing = db.query(User).filter(User.email == body.email, User.id != user_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already in use")
    old_email = user.email
    user.email = body.email
    db.commit()
    audit(db, user.username, "admin_email_change", f"{old_email} → {body.email}")
    return {"message": f"Email updated for {user.username}"}


@app.put("/admin/users/{user_id}/notes", response_model=MessageResponse)
def update_notes(
    user_id: str,
    body: UpdateNotesRequest,
    db: Session = Depends(get_db),
    _: str = Depends(get_current_admin),
):
    user = get_user_or_404(user_id, db)
    user.notes = body.notes
    db.commit()
    return {"message": "Notes updated"}


@app.delete("/admin/users/{user_id}", response_model=MessageResponse)
def delete_user(user_id: str, db: Session = Depends(get_db), _: str = Depends(get_current_admin)):
    user = get_user_or_404(user_id, db)
    username = user.username
    db.delete(user)
    db.commit()
    sync(db)
    audit(db, username, "admin_delete", "account deleted by admin")
    return {"message": f"User {username} deleted"}
