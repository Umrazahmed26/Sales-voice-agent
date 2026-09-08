from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import unicodedata
from pathlib import Path
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from typing import Any
import tempfile
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Lead, LeadClassification, LeadStatus
from app.schemas import (
    LeadRead,
    WebhookClassifyRequest,
    WebhookScheduleRequest,
    WebhookScheduleResponse,
    WebhookWhatsappRequest,
    WebhookWhatsappResponse,
)


WHATSAPP_TIMEOUT_SECONDS = 30.0
ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS = 10.0
IST_TIMEZONE = "Asia/Kolkata"
JSON_CONTENT_TYPE = "application/json"
IST = ZoneInfo(IST_TIMEZONE)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])


def _lead_by_phone(db: Session, phone_number: str) -> Lead | None:
    return db.scalar(select(Lead).where(Lead.phone_number == phone_number))


def _require_lead(db: Session, phone_number: str) -> Lead:
    lead = _lead_by_phone(db, phone_number)
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")
    return lead


def _normalize_webhook_phone_number(value: str) -> str:
    number_words = {
        "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
        "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
        "nine": "9",
    }
    value = re.sub(
        r"\b(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)\b",
        lambda match: number_words[match.group(0).lower()],
        value,
        flags=re.IGNORECASE,
    )
    digits = []
    for character in value:
        try:
            digits.append(str(unicodedata.digit(character)))
        except (TypeError, ValueError):
            continue
    return f"+{''.join(digits)}"


def _groq_completion(prompt: str, model: str | None = None) -> str:
    api_key = _env_value("GROQ_API_KEY")
    if not api_key:
        raise ValueError("missing GROQ_API_KEY configuration")
    response = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": JSON_CONTENT_TYPE,
        },
        json={
            "model": model or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=30.0,
    )
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]["content"].strip()
    if not message:
        raise ValueError("Groq returned an empty message")
    return message


def _resolve_requested_datetime(requested_time_phrase: str) -> datetime:
    now = datetime.now(IST)
    fallback = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    prompt = (
        f"The current date and time is {now.isoformat()} (IST). Convert this spoken phrase "
        "into a specific date and time in ISO 8601 format, in IST:\n"
        f"'{requested_time_phrase}'. If genuinely ambiguous, default to 10:00 AM on the "
        "most reasonable interpreted day. Return ONLY the ISO 8601 datetime, nothing else."
    )
    try:
        value = _groq_completion(prompt, "openai/gpt-oss-120b").strip().strip('`')
        resolved = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=IST)
        return resolved.astimezone(IST)
    except Exception as exc:
        logger.warning("Could not resolve callback time %r: %s; using fallback", requested_time_phrase, exc)
        return fallback


def _ordinal(day: int) -> str:
    suffix = "th" if 10 < day % 100 < 14 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _human_datetime(value: datetime) -> str:
    local = value.astimezone(IST)
    tomorrow = (datetime.now(IST) + timedelta(days=1)).date()
    time_text = local.strftime("%-I:%M %p") if os.name != "nt" else local.strftime("%#I:%M %p")
    if local.date() == tomorrow:
        return f"tomorrow at {time_text}"
    return f"{local.strftime('%A, %B')} {_ordinal(local.day)} at {time_text}"


def _confirmation_message(resolved_datetime_human_readable: str) -> str:
    prompt = (
        "Write a short (1-2 sentence) natural WhatsApp confirmation message telling "
        f"someone their callback has been scheduled for {resolved_datetime_human_readable}. "
        "Sound warm and casual, like a real person confirming a plan, not a formal notification. "
        "Return only the message text, with no quotation marks or explanation."
    )
    try:
        return _groq_completion(prompt)
    except Exception as exc:
        logger.warning("Could not generate callback confirmation: %s", exc)
        return f"Sounds good, your callback is scheduled for {resolved_datetime_human_readable}. Talk soon!"


def _create_calendar_event(lead: Lead, resolved_datetime: datetime) -> str | None:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    service_account_file = _env_value("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not service_account_file:
        raise ValueError("missing GOOGLE_SERVICE_ACCOUNT_FILE configuration")
    credentials = service_account.Credentials.from_service_account_file(
        service_account_file,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    details = ", ".join(
        value for value in (
            f"budget: {lead.budget}" if lead.budget else None,
            f"products: {lead.products}" if lead.products else None,
            f"timeline: {lead.timeline}" if lead.timeline else None,
            f"features: {lead.features}" if lead.features else None,
        ) if value
    ) or "No additional lead details were recorded."
    start = resolved_datetime.astimezone(IST)
    end = start + timedelta(minutes=30)
    event = {
        "summary": f"Callback: {lead.phone_number}",
        "description": details,
        "start": {"dateTime": start.isoformat(), "timeZone": IST_TIMEZONE},
        "end": {"dateTime": end.isoformat(), "timeZone": IST_TIMEZONE},
    }
    calendar = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    created = calendar.events().insert(
        calendarId=_env_value("GOOGLE_CALENDAR_ID") or "primary", body=event
    ).execute()
    return created.get("htmlLink")


def _normalize_classification(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    translations = {
        "हॉट": "hot",
        "गरम": "hot",
        "హాట్": "hot",
        "వేడి": "hot",
        "वार्म": "warm",
        "गुनगुना": "warm",
        "వార్మ్": "warm",
        "వెచ్చగా": "warm",
        "గోరువెచ్చగా": "warm",
        "कोल्ड": "cold",
        "ठंडा": "cold",
        "కోల్డ్": "cold",
        "చల్లగా": "cold",
        "చల్లని": "cold",
    }
    normalized = translations.get(normalized, normalized)
    return normalized if normalized in {"hot", "warm", "qualified", "cold"} else "warm"


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


def _whatsapp_config() -> tuple[str, str, str, str, str | None, str | None, str | None]:
    unipile_dsn = _env_value("UNIPILE_DSN")
    unipile_api_key = _env_value("UNIPILE_API_KEY")
    unipile_account_id = _env_value("UNIPILE_ACCOUNT_ID")
    sender_phone_number = _env_value("SENDER_PHONE_NUMBER")
    resume_file_path = _env_value("RESUME_FILE_PATH")
    build_image_file_path = _env_value("BUILD_IMAGE_FILE_PATH")
    followup_link_url = _env_value("FOLLOWUP_LINK_URL")

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

    return (
        unipile_dsn,
        unipile_api_key,
        unipile_account_id,
        sender_phone_number,
        resume_file_path,
        build_image_file_path,
        followup_link_url,
    )


def _env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    return value.strip().strip('"').strip("'").strip()


def _fallback_message_body(
    lead: Lead,
    sender_phone_number: str,
    followup_link_url: str | None,
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

    message = (
        f"Hi, I wanted to follow up on {summary}. "
        "I have attached my resume for reference. "
        f"You can reach me directly at {sender_phone_number}."
    )
    return f"{message}\n{followup_link_url}" if followup_link_url else message


def _message_body(
    lead: Lead,
    sender_phone_number: str,
    followup_link_url: str | None,
) -> str:
    fallback = _fallback_message_body(lead, sender_phone_number, followup_link_url)
    api_key = _env_value("GROQ_API_KEY")
    if not api_key:
        print("Groq message generation failed: missing GROQ_API_KEY configuration")
        return fallback

    lead_context = ", ".join(
        value
        for value in (
            f"budget: {lead.budget}" if lead.budget else None,
            f"products: {lead.products}" if lead.products else None,
            f"timeline: {lead.timeline}" if lead.timeline else None,
            f"features: {lead.features}" if lead.features else None,
        )
        if value
    ) or "No additional details were recorded."
    prompt = (
        "Write a short (2-3 sentences, under 40 words) natural WhatsApp follow-up message "
        "to a lead. "
        "Use the lead details naturally rather than dumping fields or sounding like a template. "
        "Mention that the resume, a build overview, and a link are attached below, "
        "and include the sender's phone number. "
        "Return only the message text, with no quotation marks or explanation.\n\n"
        f"Lead details: {lead_context}\n"
        f"Sender phone number: {sender_phone_number}"
    )

    try:
        response = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": JSON_CONTENT_TYPE,
            },
            json={
                "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
                "temperature": 0.7,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]["content"].strip()
        if not message:
            raise ValueError("Groq returned an empty message")
        return f"{message}\n{followup_link_url}" if followup_link_url else message
    except Exception as exc:
        print(f"Groq message generation failed: {exc}")
        return fallback


def _whatsapp_recipient(phone_number: str) -> str:
    return f"{phone_number.replace('+', '')}@s.whatsapp.net"


def _send_unipile_message(
    dsn: str,
    api_key: str,
    account_id: str,
    recipient: str,
    message: str,
    attachment_paths: list[str | None] | None = None,
) -> httpx.Response:
    normalized_dsn = dsn.removeprefix("https://").rstrip("/")
    files = []
    temporary_paths = []
    for attachment_path in attachment_paths or []:
        if not attachment_path:
            continue
        path, temporary_path = _prepare_attachment(attachment_path)
        if temporary_path:
            temporary_paths.append(temporary_path)
        if not path:
            continue
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files.append(("attachments", (path.name, path.open("rb"), content_type)))

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
        for _, file_tuple in files:
            file_tuple[1].close()
        for temporary_path in temporary_paths:
            try:
                Path(temporary_path).unlink()
            except OSError as exc:
                print(f"WhatsApp temporary attachment cleanup failed: {exc}")


def _google_drive_download_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc not in {"drive.google.com", "www.drive.google.com"}:
        return url
    match = re.search(r"/file/d/([^/]+)", parsed.path)
    file_id = match.group(1) if match else parse_qs(parsed.query).get("id", [None])[0]
    return f"https://drive.google.com/uc?export=download&id={file_id}" if file_id else url


def _prepare_attachment(value: str) -> tuple[Path | None, str | None]:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        download_url = _google_drive_download_url(value)
        try:
            response = httpx.get(
                download_url,
                follow_redirects=True,
                timeout=ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type:
                print(f"WhatsApp attachment skipped: URL returned HTML: {value}")
                return None, None
            suffix = Path(parsed.path).suffix or mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".bin"
            temporary_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            temporary_file.write(response.content)
            temporary_file.close()
            return Path(temporary_file.name), temporary_file.name
        except (httpx.HTTPError, OSError, ValueError) as exc:
            print(f"WhatsApp attachment skipped: could not download {value}: {exc}")
            return None, None

    path = Path(value)
    if path.is_file():
        return path, None
    print(f"WhatsApp attachment skipped: local file not found: {path}")
    return None, None


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
    payload.phone_number = _normalize_webhook_phone_number(payload.phone_number)
    if payload.classification is not None:
        payload.classification = _normalize_classification(payload.classification)
    lead = _require_lead(db, payload.phone_number)
    update_data = payload.model_dump(
        exclude={"phone_number"}, exclude_none=True, exclude_unset=True
    )

    for field_name, value in update_data.items():
        if field_name == "classification":
            value = LeadClassification(value)
        setattr(lead, field_name, value)

    _append_transcript_line(lead, payload)
    db.commit()
    db.refresh(lead)

    return lead


@router.post("/whatsapp", response_model=WebhookWhatsappResponse)
def send_whatsapp(payload: WebhookWhatsappRequest, db: Session = Depends(get_db)):
    payload.phone_number = _normalize_webhook_phone_number(payload.phone_number)
    lead = _require_lead(db, payload.phone_number)
    (
        unipile_dsn,
        unipile_api_key,
        unipile_account_id,
        sender_phone_number,
        resume_file_path,
        build_image_file_path,
        followup_link_url,
    ) = _whatsapp_config()
    message = _message_body(lead, sender_phone_number, followup_link_url)
    try:
        _send_unipile_message(
            unipile_dsn,
            unipile_api_key,
            unipile_account_id,
            _whatsapp_recipient(lead.phone_number),
            message,
            [resume_file_path, build_image_file_path],
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


@router.post("/schedule", response_model=WebhookScheduleResponse)
def schedule_callback(payload: WebhookScheduleRequest, db: Session = Depends(get_db)):
    payload.phone_number = _normalize_webhook_phone_number(payload.phone_number)
    lead = _require_lead(db, payload.phone_number)
    resolved_datetime = _resolve_requested_datetime(payload.requested_time_phrase)

    calendar_event_link = None
    try:
        calendar_event_link = _create_calendar_event(lead, resolved_datetime)
    except Exception as exc:
        logger.warning("Calendar callback sync failed for %s: %s", lead.phone_number, exc)

    lead.callback_requested_at = resolved_datetime
    lead.status = LeadStatus.follow_up_pending
    db.commit()
    db.refresh(lead)

    confirmation_sent = False
    try:
        unipile_dsn = _env_value("UNIPILE_DSN")
        unipile_api_key = _env_value("UNIPILE_API_KEY")
        unipile_account_id = _env_value("UNIPILE_ACCOUNT_ID")
        missing = [
            name for name, value in (
                ("UNIPILE_DSN", unipile_dsn),
                ("UNIPILE_API_KEY", unipile_api_key),
                ("UNIPILE_ACCOUNT_ID", unipile_account_id),
            ) if not value
        ]
        if missing:
            raise ValueError(f"Missing WhatsApp configuration: {', '.join(missing)}")
        _send_unipile_message(
            unipile_dsn,
            unipile_api_key,
            unipile_account_id,
            _whatsapp_recipient(lead.phone_number),
            _confirmation_message(_human_datetime(resolved_datetime)),
        )
        confirmation_sent = True
    except Exception as exc:
        logger.warning("Callback WhatsApp confirmation failed for %s: %s", lead.phone_number, exc)

    return {
        "lead": lead,
        "resolved_datetime": resolved_datetime,
        "calendar_event_link": calendar_event_link,
        "confirmation_sent": confirmation_sent,
    }








# from __future__ import annotations

# import json
# import mimetypes
# import os
# import re
# import tempfile
# import unicodedata
# from datetime import UTC, datetime
# from pathlib import Path
# from typing import Any
# from urllib.parse import parse_qs, urlparse

# import httpx
# from fastapi import APIRouter, Depends, HTTPException, status
# from fastapi.encoders import jsonable_encoder
# from fastapi.responses import JSONResponse
# from sqlalchemy import select
# from sqlalchemy.orm import Session

# from app.database import get_db
# from app.models import Lead, LeadClassification, LeadStatus
# from app.schemas import LeadRead, WebhookClassifyRequest, WebhookWhatsappRequest, WebhookWhatsappResponse

# WHATSAPP_TIMEOUT_SECONDS = 30.0
# ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS = 10.0
# router = APIRouter(prefix="/webhook", tags=["webhook"])


# def _lead_by_phone(db: Session, phone_number: str) -> Lead | None:
#     return db.scalar(select(Lead).where(Lead.phone_number == phone_number))


# def _require_lead(db: Session, phone_number: str) -> Lead:
#     lead = _lead_by_phone(db, phone_number)
#     if lead is None:
#         raise HTTPException(status_code=404, detail="Lead not found")
#     return lead


# def _normalize_phone(value: str) -> str:
#     digits = []
#     for character in value:
#         try:
#             digits.append(str(unicodedata.digit(character)))
#         except (TypeError, ValueError):
#             continue
#     return f"+{''.join(digits)}"


# def _normalize_classification(value: str | None) -> str:
#     normalized = (value or "").strip().lower()
#     translations = {
#         "हॉट": "hot", "गरम": "hot", "హాట్": "hot", "వేడి": "hot",
#         "वार्म": "warm", "गुनगुना": "warm", "వార్మ్": "warm",
#         "వెచ్చగా": "warm", "గోరువెచ్చగా": "warm",
#         "कोल्ड": "cold", "ठंडा": "cold", "కోల్డ్": "cold",
#         "చల్లగా": "cold", "చల్లని": "cold",
#     }
#     normalized = translations.get(normalized, normalized)
#     return normalized if normalized in {"hot", "warm", "cold"} else "warm"


# def _append_transcript_line(lead: Lead, payload: WebhookClassifyRequest) -> None:
#     entry = {
#         "received_at": datetime.now(UTC).isoformat(),
#         "source": "webhook/classify",
#         "data": payload.model_dump(exclude_unset=True, exclude_none=True, mode="json"),
#     }
#     line = json.dumps(entry, ensure_ascii=True)
#     lead.transcript = f"{lead.transcript.rstrip()}\n{line}" if lead.transcript else line


# def _env_value(name: str) -> str | None:
#     value = os.getenv(name)
#     return value.strip().strip('"').strip("'").strip() if value else None


# def _whatsapp_config() -> tuple[str, str, str, str, str | None, str | None, str | None]:
#     values = {
#         "UNIPILE_DSN": _env_value("UNIPILE_DSN"),
#         "UNIPILE_API_KEY": _env_value("UNIPILE_API_KEY"),
#         "UNIPILE_ACCOUNT_ID": _env_value("UNIPILE_ACCOUNT_ID"),
#         "SENDER_PHONE_NUMBER": _env_value("SENDER_PHONE_NUMBER"),
#     }
#     missing = [name for name, value in values.items() if not value]
#     if missing:
#         raise HTTPException(status_code=500, detail=f"Missing WhatsApp configuration: {', '.join(missing)}")
#     return (
#         values["UNIPILE_DSN"], values["UNIPILE_API_KEY"], values["UNIPILE_ACCOUNT_ID"],
#         values["SENDER_PHONE_NUMBER"], _env_value("RESUME_FILE_PATH"),
#         _env_value("BUILD_IMAGE_FILE_PATH"), _env_value("FOLLOWUP_LINK_URL"),
#     )


# def _fallback_message_body(lead: Lead, sender_phone_number: str, link: str | None) -> str:
#     details = []
#     if lead.budget:
#         details.append(f"a budget around {lead.budget}")
#     if lead.timeline:
#         details.append(f"a timeline of {lead.timeline}")
#     if lead.products:
#         details.append(lead.products)
#     if lead.features:
#         details.append(f"with {lead.features}")
#     summary = ", ".join(details) if details else "the solution we discussed"
#     message = f"Hi, I wanted to follow up on {summary}. I have attached my resume for reference. You can reach me directly at {sender_phone_number}."
#     return f"{message}\n{link}" if link else message


# def _message_body(lead: Lead, sender_phone_number: str, link: str | None) -> str:
#     fallback = _fallback_message_body(lead, sender_phone_number, link)
#     api_key = _env_value("GROQ_API_KEY")
#     if not api_key:
#         return fallback
#     details = ", ".join(value for value in (
#         f"budget: {lead.budget}" if lead.budget else None,
#         f"products: {lead.products}" if lead.products else None,
#         f"timeline: {lead.timeline}" if lead.timeline else None,
#         f"features: {lead.features}" if lead.features else None,
#     ) if value) or "No additional details were recorded."
#     try:
#         response = httpx.post(
#             "https://api.groq.com/openai/v1/chat/completions",
#             headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
#             json={
#                 "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
#                 "temperature": 0.7,
#                 "messages": [{"role": "user", "content": (
#                     "Write a short natural WhatsApp follow-up message under 40 words. "
#                     "Mention the resume, build overview, link, and sender phone number. "
#                     "Return only the message.\n\n"
#                     f"Lead details: {details}\nSender phone number: {sender_phone_number}"
#                 )}],
#             },
#             timeout=30.0,
#         )
#         response.raise_for_status()
#         message = response.json()["choices"][0]["message"]["content"].strip()
#         return f"{message}\n{link}" if link else message
#     except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
#         return fallback


# def _google_drive_download_url(url: str) -> str:
#     parsed = urlparse(url)
#     if parsed.netloc not in {"drive.google.com", "www.drive.google.com"}:
#         return url
#     match = re.search(r"/file/d/([^/]+)", parsed.path)
#     file_id = match.group(1) if match else parse_qs(parsed.query).get("id", [None])[0]
#     return f"https://drive.google.com/uc?export=download&id={file_id}" if file_id else url


# def _prepare_attachment(value: str) -> tuple[Path | None, str | None]:
#     parsed = urlparse(value)
#     if parsed.scheme in {"http", "https"}:
#         try:
#             response = httpx.get(_google_drive_download_url(value), follow_redirects=True, timeout=ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS)
#             response.raise_for_status()
#             if "text/html" in response.headers.get("content-type", "").lower():
#                 return None, None
#             suffix = Path(parsed.path).suffix or mimetypes.guess_extension(response.headers.get("content-type", "").split(";", 1)[0]) or ".bin"
#             temporary_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
#             temporary_file.write(response.content)
#             temporary_file.close()
#             return Path(temporary_file.name), temporary_file.name
#         except (httpx.HTTPError, OSError, ValueError):
#             return None, None
#     path = Path(value)
#     return (path, None) if path.is_file() else (None, None)


# def _send_unipile_message(dsn: str, api_key: str, account_id: str, recipient: str, message: str, attachments: list[str | None]) -> httpx.Response:
#     normalized_dsn = dsn.removeprefix("https://").rstrip("/")
#     files = []
#     temporary_paths = []
#     for attachment in attachments:
#         if not attachment:
#             continue
#         path, temporary_path = _prepare_attachment(attachment)
#         if temporary_path:
#             temporary_paths.append(temporary_path)
#         if path:
#             content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
#             files.append(("attachments", (path.name, path.open("rb"), content_type)))
#     try:
#         response = httpx.post(
#             f"https://{normalized_dsn}/api/v1/chats",
#             headers={"X-API-KEY": api_key, "accept": "application/json"},
#             data={"account_id": account_id, "attendees_ids": recipient, "text": message},
#             files=files,
#             timeout=WHATSAPP_TIMEOUT_SECONDS,
#         )
#         response.raise_for_status()
#         return response
#     finally:
#         for _, file_tuple in files:
#             file_tuple[1].close()
#         for temporary_path in temporary_paths:
#             try:
#                 Path(temporary_path).unlink()
#             except OSError:
#                 pass


# def _response_body(response: httpx.Response) -> Any:
#     try:
#         return response.json()
#     except ValueError:
#         return response.text


# def _lead_payload(lead: Lead) -> dict[str, Any]:
#     return LeadRead.model_validate(lead).model_dump(mode="json")


# @router.post("/classify", response_model=LeadRead)
# def classify_lead(payload: WebhookClassifyRequest, db: Session = Depends(get_db)):
#     payload.phone_number = _normalize_phone(payload.phone_number)
#     if payload.classification is not None:
#         payload.classification = _normalize_classification(payload.classification)
#     lead = _require_lead(db, payload.phone_number)
#     for field_name, value in payload.model_dump(exclude={"phone_number"}, exclude_none=True, exclude_unset=True).items():
#         setattr(lead, field_name, LeadClassification(value) if field_name == "classification" else value)
#     _append_transcript_line(lead, payload)
#     db.commit()
#     db.refresh(lead)
#     return lead


# @router.post("/whatsapp", response_model=WebhookWhatsappResponse)
# def send_whatsapp(payload: WebhookWhatsappRequest, db: Session = Depends(get_db)):
#     lead = _require_lead(db, _normalize_phone(payload.phone_number))
#     dsn, api_key, account_id, sender, resume, build_image, link = _whatsapp_config()
#     message = _message_body(lead, sender, link)
#     try:
#         _send_unipile_message(dsn, api_key, account_id, f"{lead.phone_number.replace('+', '')}@s.whatsapp.net", message, [resume, build_image])
#     except httpx.HTTPStatusError as exc:
#         return JSONResponse(status_code=502, content=jsonable_encoder({"lead": _lead_payload(lead), "whatsapp_status": {"status": "error", "message": "Unipile request failed", "status_code": exc.response.status_code, "response": _response_body(exc.response)}}))
#     except httpx.RequestError as exc:
#         return JSONResponse(status_code=502, content=jsonable_encoder({"lead": _lead_payload(lead), "whatsapp_status": {"status": "error", "message": "Could not reach Unipile", "error": str(exc)}}))
#     lead.whatsapp_sent = True
#     lead.status = LeadStatus.follow_up_sent
#     db.commit()
#     db.refresh(lead)
#     return {"lead": lead, "whatsapp_status": "sent"}

