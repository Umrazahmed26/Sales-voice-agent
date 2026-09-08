from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Call, Lead, LeadClassification, LeadStatus
from app.schemas import CallRead, LeadCreate, LeadRead, LeadUpdate


router = APIRouter(prefix="/leads", tags=["leads"])


def _lead_by_phone(db: Session, phone_number: str) -> Lead | None:
    return db.scalar(select(Lead).where(Lead.phone_number == phone_number))


def _lead_by_identifier(db: Session, identifier: str) -> Lead | None:
    try:
        return db.get(Lead, UUID(identifier))
    except ValueError:
        return _lead_by_phone(db, identifier)


def get_or_create_lead(db: Session, payload: LeadCreate) -> tuple[Lead, bool]:
    existing = _lead_by_phone(db, payload.phone_number)
    if existing is not None:
        return existing, False

    lead = Lead(**payload.model_dump())
    db.add(lead)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _lead_by_phone(db, payload.phone_number)
        if existing is not None:
            return existing, False
        raise

    db.refresh(lead)
    return lead, True


@router.post("", response_model=LeadRead)
def create_lead(payload: LeadCreate, response: Response, db: Session = Depends(get_db)):
    lead, created = get_or_create_lead(db, payload)
    response.status_code = status.HTTP_201_CREATED
    if not created:
        response.status_code = status.HTTP_200_OK
    return lead


@router.get("/{phone_number}/calls", response_model=list[CallRead])
def list_lead_calls(phone_number: str, db: Session = Depends(get_db)):
    query = (
        select(Call)
        .join(Lead, Call.lead_id == Lead.id)
        .where(Lead.phone_number == phone_number)
        .order_by(Call.started_at.desc())
    )
    return list(db.scalars(query).all())


@router.get("/{identifier}", response_model=LeadRead)
def get_lead(identifier: str, db: Session = Depends(get_db)):
    lead = _lead_by_identifier(db, identifier)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return lead


@router.get("", response_model=list[LeadRead])
def list_leads(
    status: LeadStatus | None = None,
    classification: LeadClassification | None = None,
    db: Session = Depends(get_db),
):
    query = select(Lead).order_by(Lead.created_at.desc())

    if status is not None:
        query = query.where(Lead.status == status)

    if classification is not None:
        query = query.where(Lead.classification == classification)

    return list(db.scalars(query).all())


@router.patch("/{identifier}", response_model=LeadRead)
def update_lead(identifier: str, payload: LeadUpdate, db: Session = Depends(get_db)):
    lead = _lead_by_identifier(db, identifier)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(lead, field_name, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A lead with that phone number already exists",
        ) from exc

    db.refresh(lead)
    return lead
