from __future__ import annotations

import os, httpx
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Lead, LeadStatus
from app.routers.leads import get_or_create_lead
from app.schemas import CallDispatchRequest, CallDispatchResponse, LeadCreate


OMNIDIM_DISPATCH_URL = "https://backend.omnidim.io/api/v1/calls/dispatch"
OMNIDIM_TIMEOUT_SECONDS = 30.0

router = APIRouter(prefix="/calls", tags=["calls"])


def _omnidim_config() -> tuple[str, str, str]:
    api_key = os.getenv("OMNIDIM_API_KEY")
    agent_id = os.getenv("OMNIDIM_AGENT_ID")
    from_number_id = os.getenv("OMNIDIM_FROM_NUMBER_ID")

    missing = [
        name
        for name, value in (
            ("OMNIDIM_API_KEY", api_key),
            ("OMNIDIM_AGENT_ID", agent_id),
            ("OMNIDIM_FROM_NUMBER_ID", from_number_id),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Missing OmniDimension configuration: {', '.join(missing)}",
        )

    return api_key, agent_id, from_number_id


def _set_lead_status(db: Session, lead: Lead, lead_status: LeadStatus) -> Lead:
    lead.status = lead_status
    db.commit()
    db.refresh(lead)
    return lead


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _rollback_to_new(db: Session, lead: Lead) -> None:
    _set_lead_status(db, lead, LeadStatus.new)


@router.post("/dispatch", response_model=CallDispatchResponse)
def dispatch_call(payload: CallDispatchRequest, db: Session = Depends(get_db)):
    api_key, agent_id, from_number_id = _omnidim_config()

    lead, _ = get_or_create_lead(db, LeadCreate(phone_number=payload.phone_number))
    lead = _set_lead_status(db, lead, LeadStatus.in_call)

    try:
        response = httpx.post(
            OMNIDIM_DISPATCH_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "agent_id": agent_id,
                "to_number": payload.phone_number,
                "from_number_id": from_number_id,
            },
            timeout=OMNIDIM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _rollback_to_new(db, lead)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "OmniDimension dispatch failed",
                "status_code": exc.response.status_code,
                "response": _response_body(exc.response),
            },
        ) from exc
    except httpx.RequestError as exc:
        _rollback_to_new(db, lead)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Could not reach OmniDimension dispatch endpoint",
                "error": str(exc),
            },
        ) from exc

    response_json = _response_body(response)
    print(
        "OmniDimension dispatch response:",
        {"status_code": response.status_code, "body": response_json},
    )
    omnidim_call_id = response_json["requestId"]

    return {"lead": lead, "omnidim_call_id": omnidim_call_id}
