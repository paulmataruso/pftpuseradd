from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    secret_key: str
    admin_username: str
    admin_password: str
    ftpd_passwd_path: str = "/ftpshared/ftpd.passwd"
    ftp_uid: int = 1001
    ftp_gid: int = 33
    # Comma-separated list of allowed CORS origins
    # e.g. https://ftp.cios.dhitechnical.com,https://cios.dhitechnical.com
    allowed_origins_str: str = "*"

    @property
    def allowed_origins(self) -> List[str]:
        if self.allowed_origins_str.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.allowed_origins_str.split(",") if o.strip()]

    class Config:
        env_file = ".env"


settings = Settings()
