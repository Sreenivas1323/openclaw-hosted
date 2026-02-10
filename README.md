# OpenClaw Hosted — Backend

FastAPI service for provisioning and managing per-customer OpenClaw instances on Hetzner.

## What’s here
- `app/` — FastAPI app
- `provisioning.sh` — Hetzner provision + install OpenClaw (called by backend)
- `ARCHITECTURE.md` — system design + API + security notes

## Local dev
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

> NOTE: Provisioning requires Hetzner API token and SSH key; see `.env.example`.
