from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Lead, LeadStatus
from app.schemas import (
    LeadRead,
    WebhookClassifyRequest,
    WebhookWhatsappRequest,
    WebhookWhatsappResponse,
)


WHATSAPP_TIMEOUT_SECONDS = 30.0

router = APIRouter(prefix="/webhook", tags=["webhook"])


def _lead_by_phone(db: Session, phone_number: str) -> Lead | None:
    return db.scalar(select(Lead).where(Lead.phone_number == phone_number))


def _require_lead(db: Session, phone_number: str) -> Lead:
    lead = _lead_by_phone(db, phone_number)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return lead


def _append_transcript_line(lead: Lead, payload: WebhookClassifyRequest) -> None:
    entry = {
        "received_at": datetime.now(UTC).isoformat(),
        "source": "webhook/classify",
        "data": payload.model_dump(exclude_unset=True, exclude_none=True, mode="json"),
    }
    line = json.dumps(entry, ensure_ascii=True)
    if lead.transcript:
        lead.transcript = f"{lead.transcript.rstrip()}\n{line}"
    else:
        lead.transcript = line


def _whatsapp_config() -> tuple[str, str, str, str, str | None]:
    unipile_dsn = os.getenv("UNIPILE_DSN")
    unipile_api_key = os.getenv("UNIPILE_API_KEY")
    unipile_account_id = os.getenv("UNIPILE_ACCOUNT_ID")
    sender_phone_number = os.getenv("SENDER_PHONE_NUMBER")
    resume_file_path = os.getenv("RESUME_FILE_PATH")

    missing = [
        name
        for name, value in (
            ("UNIPILE_DSN", unipile_dsn),
            ("UNIPILE_API_KEY", unipile_api_key),
            ("UNIPILE_ACCOUNT_ID", unipile_account_id),
            ("SENDER_PHONE_NUMBER", sender_phone_number),
        )
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Missing WhatsApp configuration: {', '.join(missing)}",
        )

    return unipile_dsn, unipile_api_key, unipile_account_id, sender_phone_number, resume_file_path


def _message_body(
    lead: Lead,
    sender_phone_number: str,
) -> str:
    details = []

    if lead.budget:
        details.append(f"a budget around {lead.budget}")
    if lead.timeline:
        details.append(f"a timeline of {lead.timeline}")
    if lead.products:
        details.append(lead.products)
    if lead.features:
        details.append(f"with {lead.features}")

    summary = ", ".join(details) if details else "the solution we discussed"

    return (
        f"Hi, I wanted to follow up on {summary}. "
        "I have attached my resume for reference. "
        f"You can reach me directly at {sender_phone_number}."
    )


def _whatsapp_recipient(phone_number: str) -> str:
    return f"{phone_number.replace('+', '')}@s.whatsapp.net"


def _send_unipile_message(
    dsn: str,
    api_key: str,
    account_id: str,
    recipient: str,
    message: str,
    attachment_path: str | None = None,
) -> httpx.Response:
    normalized_dsn = dsn.removeprefix("https://").rstrip("/")
    files = {}
    path = Path(attachment_path) if attachment_path else None
    if path and path.is_file():
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files["attachments"] = (path.name, path.open("rb"), content_type)

    try:
        response = httpx.post(
            f"https://{normalized_dsn}/api/v1/chats",
            headers={
                "X-API-KEY": api_key,
                "accept": "application/json",
            },
            data={
                "account_id": account_id,
                "attendees_ids": recipient,
                "text": message,
            },
            files=files,
            timeout=WHATSAPP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response
    finally:
        for file_tuple in files.values():
            file_tuple[1].close()


def _response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _lead_payload(lead: Lead) -> dict[str, Any]:
    return LeadRead.model_validate(lead).model_dump(mode="json")


def _whatsapp_error_response(lead: Lead, whatsapp_status: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content=jsonable_encoder(
            {
                "lead": _lead_payload(lead),
                "whatsapp_status": whatsapp_status,
            }
        ),
    )


@router.post("/classify", response_model=LeadRead)
def classify_lead(payload: WebhookClassifyRequest, db: Session = Depends(get_db)):
    lead = _require_lead(db, payload.phone_number)
    update_data = payload.model_dump(
        exclude={"phone_number"},
        exclude_none=True,
        exclude_unset=True,
    )

    for field_name, value in update_data.items():
        setattr(lead, field_name, value)

    _append_transcript_line(lead, payload)
    db.commit()
    db.refresh(lead)

    return lead


@router.post("/whatsapp", response_model=WebhookWhatsappResponse)
def send_whatsapp(payload: WebhookWhatsappRequest, db: Session = Depends(get_db)):
    lead = _require_lead(db, payload.phone_number)
    unipile_dsn, unipile_api_key, unipile_account_id, sender_phone_number, resume_file_path = (
        _whatsapp_config()
    )
    message = _message_body(lead, sender_phone_number)
    try:
        _send_unipile_message(
            unipile_dsn,
            unipile_api_key,
            unipile_account_id,
            _whatsapp_recipient(lead.phone_number),
            message,
            resume_file_path,
        )
    except httpx.HTTPStatusError as exc:
        return _whatsapp_error_response(
            lead,
            {
                "status": "error",
                "message": "Unipile request failed",
                "status_code": exc.response.status_code,
                "response": _response_body(exc.response),
            },
        )
    except httpx.RequestError as exc:
        return _whatsapp_error_response(
            lead,
            {
                "status": "error",
                "message": "Could not reach Unipile",
                "error": str(exc),
            },
        )
    lead.whatsapp_sent = True
    lead.status = LeadStatus.follow_up_sent
    db.commit()
    db.refresh(lead)

    return {"lead": lead, "whatsapp_status": "sent"}
