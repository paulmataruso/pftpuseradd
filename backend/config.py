from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Main user-management database (PostgreSQL)
    database_url: str

    # FTP activity log database (separate PostgreSQL instance)
    logs_database_url: str

    # Auth — used only to seed admin_config on first start.
    # After first start, credentials are managed via the admin UI.
    secret_key: str
    admin_username: str = "admin"
    admin_password: str = "changeme"

    # ProFTPd passwd file
    ftpd_passwd_path: str = "/ftpshared/ftpd.passwd"
    ftp_uid: int = 1001
    ftp_gid: int = 33

    # CORS — comma-separated list or bare *
    allowed_origins_str: str = "*"

    @property
    def allowed_origins(self) -> List[str]:
        if self.allowed_origins_str.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins_str.split(",") if o.strip()]

    class Config:
        env_file = ".env"


settings = Settings()
