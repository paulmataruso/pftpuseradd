import re
from datetime import datetime
from uuid import UUID
from typing import Optional, List
from pydantic import BaseModel, EmailStr, field_validator

USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,32}$')
PASSWORD_MIN = 8

# Usernames that must never be registered:
# - Linux system accounts that exist on the host
# - ProFTPd special users
# - Common admin/service names that could cause confusion
RESERVED_USERNAMES = {
    # Linux system
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
    "nobody", "systemd", "syslog", "messagebus", "uuidd", "dnsmasq",
    "usbmux", "rtkit", "cups", "avahi", "speech", "pulse", "saned",
    "colord", "hplip", "geoclue", "gnome", "gdm", "sshd", "ntp",
    # FTP specific
    "ftp", "anonftp", "anonymous", "ftpuser", "ftpadmin",
    # Common service accounts on this server
    "paulmataruso", "www", "nginx", "apache", "mysql", "postgres",
    "postgresql", "redis", "mongodb", "docker",
    # Generic reserved
    "admin", "administrator", "superuser", "su", "operator",
    "postmaster", "webmaster", "hostmaster", "abuse", "noc",
    "security", "support", "info", "contact", "help",
    "test", "guest", "demo", "user", "public",
}


class RegisterRequest(BaseModel):
    username: str
    email: EmailStr
    password: str
    confirm_password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v):
        v = v.lower().strip()
        if not USERNAME_RE.match(v):
            raise ValueError(
                "Username must be 3–32 characters: letters, numbers, underscores only"
            )
        if v in RESERVED_USERNAMES:
            raise ValueError("That username is not available")
        # Also block anything that starts with these prefixes
        reserved_prefixes = ("root", "admin", "sys", "ftp", "www")
        if any(v.startswith(p) for p in reserved_prefixes):
            raise ValueError("That username is not available")
        return v

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < PASSWORD_MIN:
            raise ValueError(f"Password must be at least {PASSWORD_MIN} characters")
        return v

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v, info):
        if "password" in info.data and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    id: UUID
    username: str
    email: str
    enabled: bool
    created_at: datetime
    last_login: Optional[datetime]
    notes: Optional[str]

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    users: List[UserResponse]
    total: int


class UpdateNotesRequest(BaseModel):
    notes: Optional[str] = None


class UpdateEmailRequest(BaseModel):
    email: EmailStr


class ChangePasswordRequest(BaseModel):
    password: str
    confirm_password: str

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < PASSWORD_MIN:
            raise ValueError(f"Password must be at least {PASSWORD_MIN} characters")
        return v

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v, info):
        if "password" in info.data and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


class MessageResponse(BaseModel):
    message: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuditLogEntry(BaseModel):
    id: int
    username: Optional[str]
    action: str
    detail: Optional[str]
    ip_address: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class SyncResponse(BaseModel):
    message: str
    users_written: int
