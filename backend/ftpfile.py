"""
Generates the ProFTPd virtual user flat file (ftpd.passwd).

Format per line:
  username:hashed_password:uid:gid:gecos:home_dir:shell

- password_hash is bcrypt — ProFTPd mod_auth_file supports bcrypt natively.
- All virtual users map to the anonftp UID/GID.
- Home dir is / (users are chrooted to FTP root by DefaultRoot in proftpd.conf).
- Shell is /sbin/nologin — RequireValidShell off is set in proftpd.conf.
"""

import os
import stat
from sqlalchemy.orm import Session
from config import settings
from models import User


def regenerate_ftpd_passwd(db: Session) -> int:
    """Write ftpd.passwd from all enabled users. Returns user count written."""
    users = db.query(User).filter(User.enabled == True).order_by(User.username).all()

    lines = []
    for u in users:
        line = (
            f"{u.username}:{u.password_hash}:"
            f"{settings.ftp_uid}:{settings.ftp_gid}:"
            f"FTP User {u.username}:/:/sbin/nologin"
        )
        lines.append(line)

    content = "\n".join(lines) + ("\n" if lines else "")

    passwd_path = settings.ftpd_passwd_path
    tmp_path = passwd_path + ".tmp"

    # Atomic write — ProFTPd never reads a partial file
    with open(tmp_path, "w") as f:
        f.write(content)

    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    os.rename(tmp_path, passwd_path)

    return len(users)
