from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import LeadClassification, LeadStatus


class LeadCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone_number: str = Field(..., min_length=1, max_length=64)
    status: LeadStatus = LeadStatus.new
    classification: LeadClassification | None = None
    budget: str | None = None
    products: str | None = None
    timeline: str | None = None
    features: str | None = None
    transcript: str | None = None
    callback_requested_at: datetime | None = None


class LeadUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone_number: str | None = Field(default=None, min_length=1, max_length=64)
    status: LeadStatus | None = None
    classification: LeadClassification | None = None
    budget: str | None = None
    products: str | None = None
    timeline: str | None = None
    features: str | None = None
    transcript: str | None = None
    callback_requested_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_for_required_columns(cls, data):
        if isinstance(data, dict):
            for field_name in ("phone_number", "status"):
                if field_name in data and data[field_name] is None:
                    raise ValueError(f"{field_name} cannot be null")
        return data


class LeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    phone_number: str
    status: LeadStatus
    classification: LeadClassification | None
    budget: str | None
    products: str | None
    timeline: str | None
    features: str | None
    transcript: str | None
    whatsapp_sent: bool
    callback_requested_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CallDispatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    phone_number: str = Field(..., min_length=1, max_length=64)


class CallRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    lead_id: UUID
    omnidim_call_id: str
    phone_number: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: int | None
    transcript: str | None
    summary: str | None
    sentiment: str | None
    classification: str | None
    objections: str | None
    next_action: str | None
    recording_url: str | None
    created_at: datetime
    updated_at: datetime


class CallDispatchResponse(BaseModel):
    lead: LeadRead
    call: CallRead
    omnidim_call_id: int | str


class WebhookClassifyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    phone_number: str = Field(..., min_length=1, max_length=64)
    budget: str | None = None
    products: str | None = None
    timeline: str | None = None
    features: str | None = None
    classification: str | None = None


class WebhookWhatsappRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    phone_number: str = Field(..., min_length=1, max_length=64)


class WebhookWhatsappResponse(BaseModel):
    lead: LeadRead
    whatsapp_status: str | dict[str, Any]


class WebhookScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    phone_number: str = Field(..., min_length=1, max_length=64)
    requested_time_phrase: str = Field(..., min_length=1, max_length=500)


class WebhookScheduleResponse(BaseModel):
    lead: LeadRead
    resolved_datetime: datetime
    calendar_event_link: str | None
    confirmation_sent: bool