"""initial_schema

Revision ID: 17cd9654022e
Revises: 
Create Date: 2026-09-08 22:00:45.274684

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '17cd9654022e'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Baseline migration for the already-fixed SQLite schema.

    This app already contains a live SQLite database that matches the current
    SQLAlchemy models, including the missing columns on calls. The migration is
    intentionally a no-op so it can be stamped as the baseline without dropping
    or altering real data.
    """
    pass


def downgrade() -> None:
    """Downgrade is intentionally a no-op for the same reason."""
    pass
