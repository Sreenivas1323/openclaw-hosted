"""Async provisioning logic — runs provisioning.sh as a subprocess."""

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone

from .config import settings
from .database import get_db

logger = logging.getLogger(__name__)


async def provision_instance(instance_id: str, customer_id: str):
    """
    Run provisioning.sh in the background for a given instance.
    Updates the database with results.
    """
    setup_password = secrets.token_urlsafe(24)

    # Store the setup password
    db = await get_db()
    try:
        await db.execute(
            "UPDATE instances SET setup_password = ?, updated_at = datetime('now') WHERE id = ?",
            (setup_password, instance_id),
        )
        await db.commit()
    finally:
        await db.close()

    logger.info(f"Starting provisioning for instance {instance_id} (customer {customer_id})")

    try:
        # Run the provisioning script
        env = {
            "HETZNER_API_TOKEN": settings.HETZNER_API_TOKEN,
            "SSH_KEY_ID": settings.SSH_KEY_ID,
            "FIREWALL_ID": settings.FIREWALL_ID,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/root",
        }

        process = await asyncio.create_subprocess_exec(
            "bash",
            settings.PROVISIONING_SCRIPT,
            customer_id,
            setup_password,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=600,  # 10 minute timeout
        )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        full_log = f"STDOUT:\n{stdout_text}\n\nSTDERR:\n{stderr_text}"

        db = await get_db()
        try:
            if process.returncode == 0:
                # Parse JSON output from stdout (last line that looks like JSON)
                result = None
                for line in stdout_text.strip().split("\n"):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            result = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                if result and result.get("status") == "success":
                    await db.execute(
                        """UPDATE instances SET
                            status = 'active',
                            hetzner_server_id = ?,
                            server_ip = ?,
                            server_name = ?,
                            setup_password = ?,
                            health_status = 'healthy',
                            last_health_check = datetime('now'),
                            provision_log = ?,
                            updated_at = datetime('now')
                        WHERE id = ?""",
                        (
                            result.get("server_id"),
                            result.get("server_ip"),
                            result.get("server_name"),
                            result.get("setup_password", setup_password),
                            full_log,
                            instance_id,
                        ),
                    )
                    await db.execute(
                        "UPDATE customers SET status = 'active', updated_at = datetime('now') WHERE id = ?",
                        (customer_id,),
                    )

                    # Log event
                    await db.execute(
                        "INSERT INTO events (instance_id, customer_id, event_type, payload) VALUES (?, ?, ?, ?)",
                        (instance_id, customer_id, "provisioned", json.dumps(result)),
                    )
                    logger.info(
                        f"✅ Instance {instance_id} provisioned successfully: {result.get('server_ip')}"
                    )
                else:
                    # Script returned 0 but output wasn't valid
                    await _mark_failed(db, instance_id, customer_id, full_log)
                    logger.error(f"Provisioning returned 0 but invalid output for {instance_id}")
            else:
                await _mark_failed(db, instance_id, customer_id, full_log)
                logger.error(
                    f"Provisioning failed for {instance_id} (exit code {process.returncode})"
                )

            await db.commit()
        finally:
            await db.close()

    except asyncio.TimeoutError:
        logger.error(f"Provisioning timed out for {instance_id}")
        db = await get_db()
        try:
            await _mark_failed(db, instance_id, customer_id, "Provisioning timed out after 600s")
            await db.commit()
        finally:
            await db.close()

    except Exception as e:
        logger.exception(f"Unexpected error provisioning {instance_id}")
        db = await get_db()
        try:
            await _mark_failed(db, instance_id, customer_id, str(e))
            await db.commit()
        finally:
            await db.close()


async def _mark_failed(db, instance_id: str, customer_id: str, log: str):
    """Mark an instance as failed."""
    await db.execute(
        """UPDATE instances SET
            status = 'failed',
            provision_log = ?,
            updated_at = datetime('now')
        WHERE id = ?""",
        (log, instance_id),
    )
    await db.execute(
        "INSERT INTO events (instance_id, customer_id, event_type, payload) VALUES (?, ?, ?, ?)",
        (instance_id, customer_id, "provision_failed", json.dumps({"log_preview": log[:500]})),
    )


async def check_instance_health(instance_id: str, server_ip: str) -> bool:
    """Check if an instance's OpenClaw gateway is reachable."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"http://{server_ip}:18789/")
            is_healthy = 200 <= resp.status_code < 500
    except Exception:
        is_healthy = False

    db = await get_db()
    try:
        health_status = "healthy" if is_healthy else "unhealthy"
        await db.execute(
            """UPDATE instances SET
                health_status = ?,
                last_health_check = datetime('now'),
                updated_at = datetime('now')
            WHERE id = ?""",
            (health_status, instance_id),
        )
        await db.commit()
    finally:
        await db.close()

    return is_healthy
