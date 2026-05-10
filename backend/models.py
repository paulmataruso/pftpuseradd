import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, Text, DateTime, BigInteger
from sqlalchemy.dialects.postgresql import UUID
from database import Base

class User(Base):
    __tablename__ = "users"

    id           = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username     = Column(String(64), unique=True, nullable=False, index=True)
    email        = Column(String(255), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    enabled      = Column(Boolean, nullable=False, default=True)
    created_at   = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
    last_login   = Column(DateTime(timezone=True), nullable=True)
    notes        = Column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id         = Column(BigInteger, primary_key=True, autoincrement=True)
    username   = Column(String(64), index=True)
    action     = Column(String(64), nullable=False)
    detail     = Column(Text)
    ip_address = Column(String(45))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
