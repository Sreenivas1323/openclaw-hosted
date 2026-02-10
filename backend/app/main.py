"""
OpenClaw Hosted — Backend API

FastAPI application for managing customer OpenClaw instances on Hetzner Cloud.
"""

import asyncio
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import get_db, init_db
from .models import (
    ErrorResponse,
    HealthResponse,
    InstanceListResponse,
    InstanceResponse,
    ProvisionRequest,
    ProvisionResponse,
)
from .provisioner import check_instance_health, provision_instance

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))


# ── ID Generation ───────────────────────────────
def generate_id(prefix: str) -> str:
    """Generate a short unique ID like 'cust_a1b2c3d4'."""
    import secrets
    return f"{prefix}_{secrets.token_hex(6)}"


# ── Auth Dependencies ───────────────────────────
async def require_admin(x_api_key: str = Header(..., alias="X-API-Key")):
    """Verify admin API key."""
    if x_api_key != settings.ADMIN_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


# ── App Lifecycle ───────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    await init_db()
    logger.info("Database initialized")
    yield


# ── App Setup ───────────────────────────────────
app = FastAPI(
    title="OpenClaw Hosted",
    description="Managed OpenClaw hosting platform API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════


@app.get("/")
async def root():
    """Health check for the backend itself."""
    return {"service": "openclaw-hosted", "status": "ok"}


# ── Provision ───────────────────────────────────

@app.post(
    "/api/provision",
    response_model=ProvisionResponse,
    status_code=202,
    dependencies=[Depends(require_admin)],
)
async def provision(req: ProvisionRequest, background_tasks: BackgroundTasks):
    """
    Create a new customer + instance and start async provisioning.
    Returns immediately with instance_id; provisioning happens in background.
    """
    customer_id = generate_id("cust")
    instance_id = generate_id("inst")

    db = await get_db()
    try:
        # Create customer
        await db.execute(
            """INSERT INTO customers (id, email, name, paddle_subscription_id, paddle_customer_id, plan, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (
                customer_id,
                req.customer_email,
                req.customer_name,
                req.paddle_subscription_id,
                req.paddle_customer_id,
                req.plan,
            ),
        )

        # Create instance
        await db.execute(
            """INSERT INTO instances (id, customer_id, status)
            VALUES (?, ?, 'provisioning')""",
            (instance_id, customer_id),
        )

        # Log event
        await db.execute(
            "INSERT INTO events (instance_id, customer_id, event_type, payload) VALUES (?, ?, ?, ?)",
            (
                instance_id,
                customer_id,
                "provision_requested",
                json.dumps({"email": req.customer_email, "plan": req.plan}),
            ),
        )

        await db.commit()
    finally:
        await db.close()

    # Start provisioning in background
    background_tasks.add_task(provision_instance, instance_id, customer_id)

    return ProvisionResponse(
        instance_id=instance_id,
        customer_id=customer_id,
        status="provisioning",
        estimated_ready_seconds=300,
    )


# ── List Instances ──────────────────────────────

@app.get(
    "/api/instances",
    response_model=InstanceListResponse,
    dependencies=[Depends(require_admin)],
)
async def list_instances(
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """List all customer instances, optionally filtered by status."""
    db = await get_db()
    try:
        query = """
            SELECT i.id as instance_id, i.customer_id, c.email as customer_email,
                   i.status, i.server_ip, i.hetzner_server_id,
                   i.setup_password, c.plan, i.created_at,
                   i.health_status, i.last_health_check
            FROM instances i
            JOIN customers c ON i.customer_id = c.id
        """
        params = []

        if status:
            query += " WHERE i.status = ?"
            params.append(status)

        query += " ORDER BY i.created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        # Count total
        count_query = "SELECT COUNT(*) FROM instances"
        if status:
            count_query += " WHERE status = ?"
            count_cursor = await db.execute(count_query, [status] if status else [])
        else:
            count_cursor = await db.execute(count_query)
        total = (await count_cursor.fetchone())[0]

        instances = []
        for row in rows:
            setup_url = f"http://{row['server_ip']}:18789" if row["server_ip"] else None
            instances.append(
                InstanceResponse(
                    instance_id=row["instance_id"],
                    customer_id=row["customer_id"],
                    customer_email=row["customer_email"],
                    status=row["status"],
                    server_ip=row["server_ip"],
                    hetzner_server_id=row["hetzner_server_id"],
                    setup_url=setup_url,
                    setup_password=row["setup_password"],
                    plan=row["plan"],
                    created_at=row["created_at"],
                    health_status=row["health_status"] or "unknown",
                    last_health_check=row["last_health_check"],
                )
            )

        return InstanceListResponse(instances=instances, total=total)
    finally:
        await db.close()


# ── Health Check ────────────────────────────────

@app.get(
    "/api/health/{instance_id}",
    response_model=HealthResponse,
    dependencies=[Depends(require_admin)],
)
async def health_check(instance_id: str):
    """Check if a customer's OpenClaw instance is reachable."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, server_ip, health_status, last_health_check, status FROM instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    if row["status"] != "active" or not row["server_ip"]:
        return HealthResponse(
            instance_id=instance_id,
            status=row["status"],
            gateway_reachable=False,
            last_checked=row["last_health_check"],
        )

    # Actually check health
    is_healthy = await check_instance_health(instance_id, row["server_ip"])

    return HealthResponse(
        instance_id=instance_id,
        status="healthy" if is_healthy else "unhealthy",
        gateway_reachable=is_healthy,
        last_checked=datetime.now(timezone.utc).isoformat(),
    )


# ── Paddle Webhook ──────────────────────────────

@app.post("/api/webhook/paddle")
async def paddle_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Handle Paddle webhook events.

    Events we handle:
    - subscription.created / transaction.completed → provision new instance
    - subscription.canceled → schedule suspension
    - subscription.past_due → mark for warning
    """
    body = await request.body()

    # Verify webhook signature (if secret is configured)
    if settings.PADDLE_WEBHOOK_SECRET:
        signature = request.headers.get("paddle-signature", "")
        if not _verify_paddle_signature(body, signature, settings.PADDLE_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("event_type", "")
    data = payload.get("data", {})

    logger.info(f"Paddle webhook: {event_type}")

    if event_type == "subscription.created":
        # New subscription — provision instance
        customer_email = (
            data.get("customer", {}).get("email")
            or data.get("custom_data", {}).get("email", "unknown@unknown.com")
        )
        paddle_sub_id = data.get("id", "")
        paddle_customer_id = data.get("customer_id", "")

        # Determine plan from price
        items = data.get("items", [])
        plan = "monthly"  # default
        if items:
            price = items[0].get("price", {})
            billing_cycle = price.get("billing_cycle")
            if billing_cycle is None:
                plan = "lifetime"

        # Check if already provisioned (idempotency)
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT id FROM customers WHERE paddle_subscription_id = ?",
                (paddle_sub_id,),
            )
            existing = await cursor.fetchone()
        finally:
            await db.close()

        if existing:
            logger.info(f"Subscription {paddle_sub_id} already provisioned, skipping")
            return {"status": "already_provisioned"}

        # Trigger provisioning via the provision endpoint logic
        customer_id = generate_id("cust")
        instance_id = generate_id("inst")

        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO customers (id, email, paddle_subscription_id, paddle_customer_id, plan, status)
                VALUES (?, ?, ?, ?, ?, 'pending')""",
                (customer_id, customer_email, paddle_sub_id, paddle_customer_id, plan),
            )
            await db.execute(
                "INSERT INTO instances (id, customer_id, status) VALUES (?, ?, 'provisioning')",
                (instance_id, customer_id),
            )
            await db.execute(
                "INSERT INTO events (instance_id, customer_id, event_type, payload) VALUES (?, ?, ?, ?)",
                (instance_id, customer_id, "paddle_subscription_created", json.dumps(data)),
            )
            await db.commit()
        finally:
            await db.close()

        background_tasks.add_task(provision_instance, instance_id, customer_id)
        return {"status": "provisioning", "instance_id": instance_id}

    elif event_type == "subscription.canceled":
        paddle_sub_id = data.get("id", "")
        db = await get_db()
        try:
            await db.execute(
                "UPDATE customers SET status = 'canceled', updated_at = datetime('now') WHERE paddle_subscription_id = ?",
                (paddle_sub_id,),
            )
            # Get customer to find instance
            cursor = await db.execute(
                "SELECT id FROM customers WHERE paddle_subscription_id = ?",
                (paddle_sub_id,),
            )
            customer = await cursor.fetchone()
            if customer:
                await db.execute(
                    "INSERT INTO events (customer_id, event_type, payload) VALUES (?, ?, ?)",
                    (customer["id"], "subscription_canceled", json.dumps(data)),
                )
            await db.commit()
        finally:
            await db.close()

        logger.info(f"Subscription {paddle_sub_id} canceled — instance will be suspended after grace period")
        return {"status": "cancellation_noted"}

    elif event_type == "subscription.past_due":
        paddle_sub_id = data.get("id", "")
        logger.warning(f"Subscription {paddle_sub_id} is past due!")
        db = await get_db()
        try:
            await db.execute(
                "INSERT INTO events (customer_id, event_type, payload) VALUES ((SELECT id FROM customers WHERE paddle_subscription_id = ?), ?, ?)",
                (paddle_sub_id, "subscription_past_due", json.dumps(data)),
            )
            await db.commit()
        finally:
            await db.close()
        return {"status": "past_due_noted"}

    elif event_type == "transaction.completed":
        # Could be a one-time lifetime purchase
        custom_data = data.get("custom_data", {})
        if custom_data.get("plan") == "lifetime":
            # Handle similar to subscription.created
            logger.info("Lifetime purchase detected — would provision here")
            # TODO: implement lifetime provisioning (same flow, different plan tag)

        return {"status": "transaction_noted"}

    else:
        logger.info(f"Unhandled Paddle event: {event_type}")
        return {"status": "ignored", "event_type": event_type}


def _verify_paddle_signature(body: bytes, signature_header: str, secret: str) -> bool:
    """
    Verify Paddle webhook signature.
    See: https://developer.paddle.com/webhooks/signature-verification
    """
    if not signature_header:
        return False

    try:
        # Parse "ts=xxx;h1=xxx" format
        parts = dict(part.split("=", 1) for part in signature_header.split(";"))
        ts = parts.get("ts", "")
        h1 = parts.get("h1", "")

        if not ts or not h1:
            return False

        # Build signed payload
        signed_payload = f"{ts}:{body.decode('utf-8')}"
        expected = hmac.new(
            secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, h1)
    except Exception:
        logger.exception("Failed to verify Paddle signature")
        return False


# ── Suspend / Destroy ───────────────────────────

@app.post(
    "/api/instances/{instance_id}/suspend",
    dependencies=[Depends(require_admin)],
)
async def suspend_instance(instance_id: str):
    """Suspend (power off) a customer's VPS."""
    import httpx

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT hetzner_server_id, status FROM instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    if row["hetzner_server_id"]:
        # Power off via Hetzner API
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.hetzner.cloud/v1/servers/{row['hetzner_server_id']}/actions/poweroff",
                headers={"Authorization": f"Bearer {settings.HETZNER_API_TOKEN}"},
            )
            if resp.status_code >= 400:
                logger.error(f"Failed to power off server: {resp.text}")

    db = await get_db()
    try:
        await db.execute(
            "UPDATE instances SET status = 'suspended', updated_at = datetime('now') WHERE id = ?",
            (instance_id,),
        )
        await db.commit()
    finally:
        await db.close()

    return {"status": "suspended", "instance_id": instance_id}


@app.post(
    "/api/instances/{instance_id}/destroy",
    dependencies=[Depends(require_admin)],
)
async def destroy_instance(instance_id: str):
    """Destroy a customer's VPS (permanent, deletes server)."""
    import httpx

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT hetzner_server_id FROM instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    if row["hetzner_server_id"]:
        # Delete via Hetzner API
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"https://api.hetzner.cloud/v1/servers/{row['hetzner_server_id']}",
                headers={"Authorization": f"Bearer {settings.HETZNER_API_TOKEN}"},
            )
            if resp.status_code >= 400:
                logger.error(f"Failed to delete server: {resp.text}")

    db = await get_db()
    try:
        await db.execute(
            "UPDATE instances SET status = 'destroyed', updated_at = datetime('now') WHERE id = ?",
            (instance_id,),
        )
        await db.commit()
    finally:
        await db.close()

    return {"status": "destroyed", "instance_id": instance_id}


# ── Health Check All (for cron) ─────────────────

@app.post(
    "/api/health-check-all",
    dependencies=[Depends(require_admin)],
)
async def health_check_all():
    """
    Run health checks on all active instances.
    Call this from a cron job every 5 minutes.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, server_ip FROM instances WHERE status = 'active' AND server_ip IS NOT NULL"
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()

    results = {}
    for row in rows:
        is_healthy = await check_instance_health(row["id"], row["server_ip"])
        results[row["id"]] = "healthy" if is_healthy else "unhealthy"

    unhealthy = [k for k, v in results.items() if v == "unhealthy"]
    if unhealthy:
        logger.warning(f"⚠️ Unhealthy instances: {unhealthy}")

    return {
        "checked": len(results),
        "healthy": sum(1 for v in results.values() if v == "healthy"),
        "unhealthy": len(unhealthy),
        "unhealthy_instances": unhealthy,
    }
