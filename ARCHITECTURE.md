# OpenClaw Hosted — Architecture

> MVP target: 3-day ship. This doc covers what we build now and flags what we defer.

---

## 1. System Overview

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────────┐
│  Landing     │────▶│  Backend API     │────▶│  Hetzner Cloud API      │
│  Page        │     │  (FastAPI)       │     │  (provision CAX11 VPS)  │
│  + Paddle    │     │                  │     └─────────────────────────┘
│  Checkout    │     │  SQLite DB       │              │
└─────────────┘     │  (customers,     │              ▼
                    │   instances)      │     ┌─────────────────────────┐
                    │                  │     │  Customer VPS (ARM)     │
                    │  Health Monitor  │────▶│  OpenClaw Gateway       │
                    │  (cron loop)     │     │  systemd managed        │
                    └──────────────────┘     └─────────────────────────┘
```

**Stack:**
- **Backend:** Python/FastAPI (fastest to ship, async-native, great for webhooks)
- **Database:** SQLite via `aiosqlite` (zero ops, MVP-appropriate, migrate to Postgres later)
- **Payments:** Paddle (handles tax, invoicing, subscriptions — no Stripe tax headaches)
- **Infra:** Hetzner Cloud API for VPS provisioning
- **Hosting the backend itself:** Single Hetzner VPS (CX22, €4.49/mo) or same machine we dev on

---

## 2. Isolation Model

### Decision: **1 Hetzner VPS per customer** (not Docker on shared VPS)

**Why not shared VPS with Docker containers?**
- OpenClaw runs a browser (Playwright/Chromium) — memory-hungry, ~1-2GB per instance
- Docker on shared VPS = noisy neighbor problems, OOM kills, complex networking
- Debugging customer issues on shared infra is painful
- CAX11 at €3.79/mo is absurdly cheap — the isolation is basically free

**Why 1 VPS per customer works for MVP:**
- Perfect isolation: customer crash doesn't affect others
- Simple mental model: 1 customer = 1 server = 1 IP
- Easy to debug, easy to migrate, easy to destroy
- Hetzner API makes creation/deletion trivial
- Cost math: €3.79 cost vs $29 revenue = ~87% margin

**Future optimization (not MVP):**
- Pack 2-3 light-usage customers per CAX21 (€7.49, 4 cores, 8GB)
- Use Docker Compose with memory limits
- Only when we have enough customers to justify the complexity

### Per-Customer VPS Spec

| Resource | CAX11 Value |
|----------|------------|
| CPU | 2 ARM cores (Ampere Altra) |
| RAM | 4 GB |
| Disk | 40 GB SSD |
| Traffic | 20 TB/mo |
| OS | Ubuntu 24.04 ARM |
| Cost | €3.79/mo (~$4.10/mo) |

---

## 3. Provisioning Flow

### Happy Path

```
Customer clicks "Subscribe" on landing page
        │
        ▼
Paddle processes payment, sends webhook
        │
        ▼
POST /api/webhook/paddle
  ├── Verify webhook signature
  ├── Create customer record (status: provisioning)
  ├── Kick off async provisioning task
  │       │
  │       ▼
  │   provisioning.sh runs:
  │     1. Create CAX11 via Hetzner API
  │     2. Wait for server to be ready (SSH accessible)
  │     3. SSH in, run OpenClaw installer
  │     4. Configure Gateway with generated setup password
  │     5. Set up systemd for auto-restart
  │     6. Verify health endpoint responds
  │       │
  │       ▼
  │   Update customer record:
  │     - status: active
  │     - server_ip: x.x.x.x
  │     - setup_url: http://x.x.x.x:18789/setup?password=xxxxx
  │       │
  │       ▼
  └── Send customer email with setup URL
        (or Telegram message if we have their handle)
```

### Timing Expectations
- Hetzner VPS creation: ~30 seconds
- SSH availability: ~60 seconds after creation
- OpenClaw install: ~2-3 minutes
- **Total: ~4-5 minutes from payment to ready**

### Error Handling
- If Hetzner API fails: retry 3x with exponential backoff, then mark as `failed`
- If install fails: mark as `failed`, alert us, keep the VPS for debugging
- If health check fails after install: retry install once, then mark `failed`
- All failures trigger a notification to our admin channel

---

## 4. API Design

### Base URL: `https://api.openclaw.host/` (or localhost:8000 for MVP)

### Endpoints

#### `POST /api/provision`
Create a new customer instance. Called internally after payment confirmation.

```json
// Request
{
  "customer_email": "user@example.com",
  "customer_name": "John Doe",
  "paddle_subscription_id": "sub_abc123",
  "plan": "monthly"  // "monthly" | "lifetime"
}

// Response 202 Accepted
{
  "instance_id": "inst_a1b2c3",
  "status": "provisioning",
  "estimated_ready_seconds": 300
}
```

#### `GET /api/instances`
List all customer instances. Admin-only (API key auth).

```json
// Response 200
{
  "instances": [
    {
      "instance_id": "inst_a1b2c3",
      "customer_email": "user@example.com",
      "status": "active",          // provisioning | active | suspended | failed | destroyed
      "server_ip": "49.13.x.x",
      "hetzner_server_id": 12345678,
      "setup_url": "http://49.13.x.x:18789",
      "plan": "monthly",
      "created_at": "2026-02-10T14:00:00Z",
      "last_health_check": "2026-02-10T14:05:00Z",
      "health_status": "healthy"   // healthy | unhealthy | unknown
    }
  ]
}
```

#### `POST /api/webhook/paddle`
Handle Paddle subscription events.

Events we care about:
- `subscription.created` → trigger provisioning
- `subscription.canceled` → mark for suspension (grace period)
- `subscription.past_due` → send warning, suspend after 7 days
- `transaction.completed` → for lifetime deals, trigger provisioning

#### `GET /api/health/{instance_id}`
Check if a customer's OpenClaw is running.

```json
// Response 200
{
  "instance_id": "inst_a1b2c3",
  "status": "healthy",
  "gateway_reachable": true,
  "uptime_seconds": 86400,
  "last_checked": "2026-02-10T14:05:00Z"
}
```

#### `POST /api/instances/{instance_id}/suspend`
Suspend a customer instance (stop the VPS, keep data).

#### `POST /api/instances/{instance_id}/destroy`
Destroy a customer instance (delete VPS, delete data after 30-day grace).

### Authentication
- Admin endpoints: `X-API-Key` header with a static secret (MVP)
- Paddle webhooks: Paddle signature verification
- No customer-facing API for MVP (they just get a setup URL)

---

## 5. Database Schema (SQLite)

```sql
CREATE TABLE customers (
    id TEXT PRIMARY KEY,                    -- "cust_" + nanoid
    email TEXT NOT NULL,
    name TEXT,
    paddle_subscription_id TEXT UNIQUE,
    paddle_customer_id TEXT,
    plan TEXT NOT NULL,                     -- "monthly" | "lifetime"
    status TEXT NOT NULL DEFAULT 'pending', -- pending | active | suspended | canceled
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE instances (
    id TEXT PRIMARY KEY,                    -- "inst_" + nanoid
    customer_id TEXT NOT NULL REFERENCES customers(id),
    hetzner_server_id INTEGER,
    server_ip TEXT,
    server_name TEXT,                       -- hetzner server name
    setup_password TEXT,                    -- generated password for OpenClaw setup
    status TEXT NOT NULL DEFAULT 'provisioning',
    -- provisioning | active | suspended | failed | destroyed
    health_status TEXT DEFAULT 'unknown',   -- healthy | unhealthy | unknown
    last_health_check TEXT,
    provision_log TEXT,                     -- stdout/stderr from provisioning
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT REFERENCES instances(id),
    customer_id TEXT REFERENCES customers(id),
    event_type TEXT NOT NULL,               -- provisioned | health_check | suspended | etc.
    payload TEXT,                           -- JSON blob
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

---

## 6. Monitoring & Alerting

### Health Checks (MVP)
- **Cron job every 5 minutes:** hit each active instance's Gateway health endpoint
- **Endpoint:** `http://{server_ip}:18789/api/health` (or just TCP connect to port 18789)
- **If unreachable 3 consecutive times:** mark as `unhealthy`, send us a Telegram notification
- **If unreachable 10 consecutive times:** attempt automatic restart via Hetzner API (reboot server)

### What We Monitor
| Check | Method | Frequency |
|-------|--------|-----------|
| Gateway up | HTTP GET to :18789 | Every 5 min |
| VPS alive | Hetzner API server status | Every 5 min |
| Disk usage | SSH command (df) | Daily |
| Our backend up | External uptime monitor (UptimeRobot free) | Every 5 min |

### Alerting (MVP)
- Send alerts to a private Telegram group (we're already on Telegram)
- Use the OpenClaw instance on our own infra to send these alerts
- **Future:** PagerDuty, Slack, email escalation

---

## 7. Backup Strategy

### MVP (Week 1)
- **No automated backups.** OpenClaw's data is mostly config + conversation history.
- Customers can re-setup if something breaks (it's an AI assistant, not a database).
- Hetzner snapshots available manually if needed (€0.012/GB/mo).

### Post-MVP (Month 1)
- Weekly Hetzner snapshots via API (automatic, ~€0.50/customer/mo)
- Daily backup of SQLite DB + OpenClaw config to object storage (Hetzner S3-compatible)
- Retention: 7 daily + 4 weekly

### What Gets Backed Up
- `/home/*/.openclaw/` — all OpenClaw data, workspace files, config
- Customer's connected channel configs (Telegram bot tokens, etc.)
- **NOT backed up:** Chromium cache, temp files, logs older than 7 days

---

## 8. Security Model

### Secrets Management (MVP)
- **Hetzner API token:** environment variable on backend server
- **Paddle webhook secret:** environment variable
- **Admin API key:** environment variable
- **Per-customer setup passwords:** generated randomly, stored in DB (hashed after first use)
- **SSH keys:** one keypair for provisioning, stored on backend server only
- **Future:** HashiCorp Vault or similar

### Network Security
- Each customer VPS has a firewall (Hetzner Cloud Firewall):
  - Port 22: SSH (restricted to our backend IP only)
  - Port 18789: OpenClaw Gateway (open, but password-protected)
  - Port 443: Future HTTPS
  - All other ports: blocked
- Backend server: SSH + API port only

### Customer Isolation
- Full VPS isolation (separate kernel, separate everything)
- No shared filesystem, no shared network
- Customer can't access other customers' data (physically impossible)
- We have SSH access for support/debugging (disclosed in ToS)

### API Security
- All endpoints behind HTTPS (Let's Encrypt via Caddy, trivial to set up)
- Paddle webhooks verified via signature
- Admin endpoints require API key
- Rate limiting: 10 req/s per IP (MVP, nginx or FastAPI middleware)

---

## 9. Cost Analysis

### Per Customer
| Item | Monthly Cost |
|------|-------------|
| Hetzner CAX11 | €3.79 (~$4.10) |
| Bandwidth | Included (20TB) |
| Backups (post-MVP) | ~$0.50 |
| **Total** | **~$4.60** |

### Revenue vs Cost
| Plan | Revenue | Cost | Margin |
|------|---------|------|--------|
| Monthly ($29/mo) | $29.00 | $4.60 | 84% |
| Lifetime ($149) | $149 one-time | $4.60/mo | Breaks even at month 32 |

### Fixed Costs
| Item | Monthly Cost |
|------|-------------|
| Backend server (CX22) | €4.49 |
| Domain | ~$1/mo amortized |
| UptimeRobot | Free |
| **Total fixed** | **~$6/mo** |

### Lifetime Plan Risk
- At $149, lifetime customers break even at ~32 months
- Mitigation: cap lifetime plan availability (first 100 customers or time-limited)
- Most SaaS lifetime deals have 40-60% churn in year 1 anyway
- The upfront cash helps fund growth

---

## 10. What Ships in 3 Days

### Day 1
- [x] ARCHITECTURE.md (this doc)
- [x] provisioning.sh (tested manually)
- [x] Backend API skeleton (FastAPI, SQLite, all endpoints)

### Day 2
- [ ] Paddle webhook integration (test with Paddle sandbox)
- [ ] Health check cron job
- [ ] Basic admin dashboard (or just curl commands + SQLite CLI)

### Day 3
- [ ] Landing page with Paddle checkout button
- [ ] End-to-end test: payment → provision → setup URL delivered
- [ ] Deploy backend to Hetzner VPS
- [ ] First beta customer

### Deferred (Post-MVP)
- Customer dashboard (manage their instance, see status)
- Automated backups
- Custom domains for customer instances
- HTTPS on customer instances (Caddy reverse proxy)
- Auto-scaling / load balancing
- Proper logging (ELK, Loki, etc.)
- Multi-region (Hetzner has US, EU, Asia)
- Terraform/Pulumi for infra-as-code

---

## 11. Open Questions

1. **Do we give customers SSH access?** Probably not for MVP. They get the OpenClaw web UI only.
2. **How do customers connect their Telegram/Discord?** Via the OpenClaw setup wizard at the setup URL. We just need to document this well.
3. **What if a customer's instance runs out of disk?** Alert us, we manually resize or clean up. Automate later.
4. **Lifetime plan: do we set a sunset date?** Recommend yes — "lifetime = lifetime of the product" with a 5-year guarantee.
5. **Do we need a status page?** Not for MVP. Add a simple one (Upptime on GitHub Pages, free) when we hit 10+ customers.
