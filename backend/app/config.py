"""Configuration loaded from environment variables."""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Hetzner
    HETZNER_API_TOKEN: str = os.getenv("HETZNER_API_TOKEN", "")
    SSH_KEY_ID: str = os.getenv("SSH_KEY_ID", "")
    FIREWALL_ID: str = os.getenv("FIREWALL_ID", "")

    # Paddle
    PADDLE_WEBHOOK_SECRET: str = os.getenv("PADDLE_WEBHOOK_SECRET", "")
    PADDLE_API_KEY: str = os.getenv("PADDLE_API_KEY", "")

    # Admin
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "changeme")

    # App
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./openclaw_hosted.db")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "info")

    # Paths
    PROVISIONING_SCRIPT: str = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "provisioning.sh",
    )


settings = Settings()
