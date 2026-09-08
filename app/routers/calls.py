from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Call, Lead, LeadStatus
from app.routers.leads import get_or_create_lead
from app.schemas import CallDispatchRequest, CallDispatchResponse, CallRead, LeadCreate


OMNIDIM_DISPATCH_URL = "https://backend.omnidim.io/api/v1/calls/dispatch"
OMNIDIM_TIMEOUT_SECONDS = 30.0

router = APIRouter(prefix="/calls", tags=["calls"])


def _omnidim_config() -> tuple[str, str, str]:
    values = {
        "OMNIDIM_API_KEY": os.getenv("OMNIDIM_API_KEY"),
        "OMNIDIM_AGENT_ID": os.getenv("OMNIDIM_AGENT_ID"),
        "OMNIDIM_FROM_NUMBER_ID": os.getenv("OMNIDIM_FROM_NUMBER_ID"),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Missing OmniDimension configuration: {', '.join(missing)}",
        )
    return values["OMNIDIM_API_KEY"], values["OMNIDIM_AGENT_ID"], values["OMNIDIM_FROM_NUMBER_ID"]


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _request_id(response_body: Any) -> int | str:
    if not isinstance(response_body, dict) or response_body.get("requestId") is None:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="OmniDimension response did not contain requestId")
    return response_body["requestId"]


@router.post("/dispatch", response_model=CallDispatchResponse)
def dispatch_call(payload: CallDispatchRequest, db: Session = Depends(get_db)):
    api_key, agent_id, from_number_id = _omnidim_config()
    lead, _ = get_or_create_lead(db, LeadCreate(phone_number=payload.phone_number))

    lead.status = LeadStatus.in_call
    db.commit()
    db.refresh(lead)

    try:
        response = httpx.post(
            OMNIDIM_DISPATCH_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "agent_id": agent_id,
                "to_number": lead.phone_number,
                "from_number_id": from_number_id,
            },
            timeout=OMNIDIM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        provider_call_id = _request_id(_response_body(response))
    except httpx.HTTPStatusError as exc:
        lead.status = LeadStatus.new
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": "OmniDimension dispatch failed", "status_code": exc.response.status_code, "response": _response_body(exc.response)},
        ) from exc
    except httpx.RequestError as exc:
        lead.status = LeadStatus.new
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"message": "Could not reach OmniDimension dispatch endpoint", "error": str(exc)},
        ) from exc

    call = Call(
        lead_id=lead.id,
        omnidim_call_id=str(provider_call_id),
        phone_number=lead.phone_number,
        status="initiated",
        started_at=datetime.now(UTC),
    )
    db.add(call)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        lead.status = LeadStatus.new
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A call with that OmniDimension ID already exists") from exc
    db.refresh(call)
    return {"lead": lead, "call": call, "omnidim_call_id": provider_call_id}


@router.get("/{call_id}", response_model=CallRead)
def get_call(call_id: UUID, db: Session = Depends(get_db)):
    call = db.get(Call, call_id)
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")
    return call