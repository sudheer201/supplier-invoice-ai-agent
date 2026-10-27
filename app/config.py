"""Central configuration. Everything is driven by environment variables so the
same code runs locally (mock mode, no AWS cost) and on AWS (Bedrock + Textract).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    # "mock" = local PDF parsing + rule-based LLM stand-in (free, offline)
    # "aws"  = Amazon Textract + Amazon Bedrock (real models)
    mode: str = field(default_factory=lambda: _env("APP_MODE", "mock"))

    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))
    # Copy the exact model ID (or inference profile ID) from the Bedrock console.
    bedrock_model_id: str = field(
        default_factory=lambda: _env("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
    )
    # Optional Amazon Bedrock Guardrail (create it in the console, paste ID + version)
    bedrock_guardrail_id: str = field(default_factory=lambda: _env("BEDROCK_GUARDRAIL_ID", ""))
    bedrock_guardrail_version: str = field(default_factory=lambda: _env("BEDROCK_GUARDRAIL_VERSION", "DRAFT"))

    # USD per 1,000 tokens - check the Bedrock pricing page for your model and update.
    price_input_per_1k: float = field(default_factory=lambda: float(_env("PRICE_INPUT_PER_1K", "0.00025")))
    price_output_per_1k: float = field(default_factory=lambda: float(_env("PRICE_OUTPUT_PER_1K", "0.00125")))

    db_path: str = field(default_factory=lambda: _env("DB_PATH", str(ROOT / "data" / "invoices.db")))
    checkpoint_path: str = field(default_factory=lambda: _env("CHECKPOINT_PATH", str(ROOT / "data" / "checkpoints.db")))
    upload_dir: str = field(default_factory=lambda: _env("UPLOAD_DIR", str(ROOT / "data" / "uploads")))

    # Business rules
    price_tolerance_pct: float = field(default_factory=lambda: float(_env("PRICE_TOLERANCE_PCT", "2.0")))
    total_tolerance: float = field(default_factory=lambda: float(_env("TOTAL_TOLERANCE", "0.05")))
    auto_approve_limit: float = field(default_factory=lambda: float(_env("AUTO_APPROVE_LIMIT", "10000")))
    max_file_mb: float = field(default_factory=lambda: float(_env("MAX_FILE_MB", "5")))
    max_extract_retries: int = field(default_factory=lambda: int(_env("MAX_EXTRACT_RETRIES", "1")))

    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))


settings = Settings()


def reload_settings() -> Settings:
    """Re-read environment variables (used by tests and the eval harness)."""
    global settings
    new = Settings()
    settings.__dict__.update(new.__dict__)
    return settings
