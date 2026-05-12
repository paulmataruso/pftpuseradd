import re
from datetime import datetime
from uuid import UUID
from typing import Optional, List, Any
from pydantic import BaseModel, EmailStr, field_validator

USERNAME_RE = re.compile(r'^[a-zA-Z0-9_]{3,32}$')
PASSWORD_MIN = 8

RESERVED_USERNAMES = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "gnats",
    "nobody", "systemd", "syslog", "messagebus", "uuidd", "dnsmasq",
    "usbmux", "rtkit", "cups", "avahi", "speech", "pulse", "saned",
    "colord", "hplip", "geoclue", "gnome", "gdm", "sshd", "ntp",
    "ftp", "anonftp", "anonymous", "ftpuser", "ftpadmin",
    "paulmataruso", "www", "nginx", "apache", "mysql", "postgres",
    "postgresql", "redis", "mongodb", "docker",
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
            raise ValueError("Username must be 3–32 characters: letters, numbers, underscores only")
        if v in RESERVED_USERNAMES:
            raise ValueError("That username is not available")
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


class AdminCreateUserRequest(BaseModel):
    username: str
    email: EmailStr
    password: str

    @field_validator("username")
    @classmethod
    def validate_username(cls, v):
        v = v.lower().strip()
        if not USERNAME_RE.match(v):
            raise ValueError("Username must be 3–32 characters: letters, numbers, underscores only")
        if v in RESERVED_USERNAMES:
            raise ValueError("That username is not available")
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


# ── Admin settings schemas ─────────────────────────────────────────────────────

class AdminChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < PASSWORD_MIN:
            raise ValueError(f"Password must be at least {PASSWORD_MIN} characters")
        return v

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v, info):
        if "new_password" in info.data and v != info.data["new_password"]:
            raise ValueError("Passwords do not match")
        return v


class AdminChangeUsernameRequest(BaseModel):
    new_username: str

    @field_validator("new_username")
    @classmethod
    def validate(cls, v):
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        return v


class LogSettingsRequest(BaseModel):
    log_filename: Optional[str] = None
    log_retention_days: Optional[int] = None
    log_retention_enabled: Optional[bool] = None

    @field_validator("log_filename")
    @classmethod
    def validate_filename(cls, v):
        if v is not None:
            v = v.strip()
            if "/" in v or "\\" in v or ".." in v:
                raise ValueError("Filename must not contain path separators")
            if not v.endswith(".log"):
                raise ValueError("Filename must end in .log")
        return v

    @field_validator("log_retention_days")
    @classmethod
    def validate_retention(cls, v):
        if v is not None and (v < 1 or v > 3650):
            raise ValueError("Retention must be between 1 and 3650 days")
        return v


class SystemSettingsResponse(BaseModel):
    admin_username: str
    log_filename: str
    log_retention_days: int
    log_retention_enabled: bool
    tailer_status: str
    tailer_last_write: str
    tailer_pos: int
    tailer_total_rows: int


# ── FTP Activity Log Schemas ───────────────────────────────────────────────────

class UserDownloadRow(BaseModel):
    id: int
    logged_at: str
    ip_address: str
    username: str
    filepath: str
    filename: str
    bytes: Optional[int]


class AnonDownloadRow(BaseModel):
    id: int
    logged_at: str
    ip_address: str
    filepath: str
    filename: str
    bytes: Optional[int]


class UserDownloadPage(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    rows: List[UserDownloadRow]


class AnonDownloadPage(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    rows: List[AnonDownloadRow]


class UserDownloadStats(BaseModel):
    total: int
    unique_users: int
    unique_ips: int
    total_bytes: int
    top_files: List[Any]
    top_users: List[Any]
    top_ips: List[Any]


class AnonDownloadStats(BaseModel):
    total: int
    unique_ips: int
    total_bytes: int
    top_files: List[Any]
    top_ips: List[Any]


class TimelineBucket(BaseModel):
    bucket: str
    downloads: int
    bytes: int


class SummaryMetric(BaseModel):
    current: int
    previous: int
    change: Optional[float]


class SummaryTimelineBucket(BaseModel):
    bucket: str
    user_downloads: int
    anon_downloads: int
    bytes: int


class SummaryResponse(BaseModel):
    days: int
    user_downloads: SummaryMetric
    user_bytes: SummaryMetric
    anon_downloads: SummaryMetric
    anon_bytes: SummaryMetric
    timeline: List[SummaryTimelineBucket]


class UserBreakdownRow(BaseModel):
    username: str
    downloads: int
    bytes: int
