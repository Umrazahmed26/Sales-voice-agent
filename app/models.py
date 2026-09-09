from __future__ import annotations

import enum
import uuid

from sqlalchemy import Boolean, Column, DateTime, Enum as SQLAlchemyEnum, ForeignKey, Integer, String, Text, false, func
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.types import CHAR, TypeDecorator

from app.database import Base


class GUID(TypeDecorator):
    """Platform-independent UUID column.

    PostgreSQL stores native UUID values; SQLite stores canonical UUID strings.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PostgreSQLUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value

        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))

        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class LeadStatus(str, enum.Enum):
    new = "new"
    in_call = "in_call"
    completed = "completed"
    follow_up_pending = "follow_up_pending"
    follow_up_sent = "follow_up_sent"


class LeadClassification(str, enum.Enum):
    hot = "hot"
    warm = "warm"
    cold = "cold"


class Lead(Base):
    __tablename__ = "leads"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    phone_number = Column(String(64), unique=True, index=True, nullable=False)
    status = Column(
        SQLAlchemyEnum(LeadStatus, native_enum=False, validate_strings=True, length=32),
        nullable=False,
        default=LeadStatus.new,
    )
    classification = Column(
        SQLAlchemyEnum(
            LeadClassification,
            native_enum=False,
            validate_strings=True,
            length=16,
        ),
        nullable=True,
    )
    budget = Column(Text, nullable=True)
    products = Column(Text, nullable=True)
    timeline = Column(Text, nullable=True)
    features = Column(Text, nullable=True)
    transcript = Column(Text, nullable=True)
    whatsapp_sent = Column(Boolean, nullable=False, default=False, server_default=false())
    callback_requested_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Call(Base):
    __tablename__ = "calls"

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    lead_id = Column(GUID(), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    omnidim_call_id = Column(String(128), nullable=False, unique=True, index=True)
    phone_number = Column(String(64), nullable=False, index=True)
    status = Column(String(64), nullable=False, default="initiated")
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    transcript = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    sentiment = Column(String(64), nullable=True)
    classification = Column(String(32), nullable=True)
    budget = Column(Text, nullable=True)
    products = Column(Text, nullable=True)
    timeline = Column(Text, nullable=True)
    features = Column(Text, nullable=True)
    objections = Column(Text, nullable=True)
    next_action = Column(String(255), nullable=True)
    recording_url = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)