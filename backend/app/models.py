"""Pydantic models for request/response validation."""

from pydantic import BaseModel, EmailStr
from typing import Optional, Literal
from datetime import datetime


# ── Request Models ──────────────────────────────

class ProvisionRequest(BaseModel):
    customer_email: str
    customer_name: Optional[str] = None
    paddle_subscription_id: Optional[str] = None
    paddle_customer_id: Optional[str] = None
    plan: Literal["monthly", "lifetime"]


# ── Response Models ─────────────────────────────

class ProvisionResponse(BaseModel):
    instance_id: str
    customer_id: str
    status: str
    estimated_ready_seconds: int = 300


class InstanceResponse(BaseModel):
    instance_id: str
    customer_id: str
    customer_email: str
    status: str
    server_ip: Optional[str]
    hetzner_server_id: Optional[int]
    setup_url: Optional[str]
    setup_password: Optional[str]
    plan: str
    created_at: str
    health_status: str
    last_health_check: Optional[str]


class InstanceListResponse(BaseModel):
    instances: list[InstanceResponse]
    total: int


class HealthResponse(BaseModel):
    instance_id: str
    status: str
    gateway_reachable: bool
    last_checked: Optional[str]


class ErrorResponse(BaseModel):
    detail: str
