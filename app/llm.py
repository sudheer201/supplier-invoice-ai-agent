"""Amazon Bedrock client (Converse API) with:
- forced tool use, so the model returns JSON that matches a schema
- retries with exponential backoff on throttling
- optional Amazon Bedrock Guardrails
- token, latency and cost recording for every call (llm_calls table)

In mock mode a deterministic Python function stands in for the model, so the whole
pipeline, tests and eval run offline for free. Token counts in mock mode are
estimated (chars / 4) and costed at your configured Bedrock prices.
"""
from __future__ import annotations

import json
import random
import time
from typing import Any, Callable

from app import db
from app.config import settings
from app.observability import get_logger, log_event, timed

log = get_logger("llm")

_client = None


def _bedrock():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "bedrock-runtime",
            region_name=settings.aws_region,
            config=Config(retries={"max_attempts": 2, "mode": "standard"}, read_timeout=60),
        )
    return _client


def _cost(tin: int, tout: int) -> float:
    return round(tin / 1000 * settings.price_input_per_1k + tout / 1000 * settings.price_output_per_1k, 8)


def call_structured(
    *,
    agent: str,
    system: str,
    user: str,
    tool_name: str,
    tool_description: str,
    json_schema: dict,
    invoice_id: int | None,
    mock_fn: Callable[[str], dict],
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """Ask the model to call `tool_name`; return the tool input (a dict)."""
    if settings.mode == "aws":
        return _call_bedrock(agent, system, user, tool_name, tool_description, json_schema, invoice_id, max_tokens)
    return _call_mock(agent, system, user, invoice_id, mock_fn)


def _call_mock(agent, system, user, invoice_id, mock_fn) -> dict:
    with timed() as t:
        result = mock_fn(user)
    tin = (len(system) + len(user)) // 4
    tout = len(json.dumps(result)) // 4
    cost = _cost(tin, tout)
    db.record_llm_call(invoice_id, agent, "mock", tin, tout, t["ms"], cost, True)
    log_event(log, "llm_call", agent=agent, model="mock", input_tokens=tin, output_tokens=tout,
              latency_ms=t["ms"], est_cost_usd=cost)
    return result


def _call_bedrock(agent, system, user, tool_name, tool_description, json_schema, invoice_id, max_tokens) -> dict:
    from botocore.exceptions import ClientError

    req: dict[str, Any] = {
        "modelId": settings.bedrock_model_id,
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": [{"text": user}]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
        "toolConfig": {
            "tools": [{"toolSpec": {
                "name": tool_name,
                "description": tool_description,
                "inputSchema": {"json": json_schema},
            }}],
            "toolChoice": {"tool": {"name": tool_name}},
        },
    }
    if settings.bedrock_guardrail_id:
        req["guardrailConfig"] = {
            "guardrailIdentifier": settings.bedrock_guardrail_id,
            "guardrailVersion": settings.bedrock_guardrail_version,
        }

    attempts = 4
    for attempt in range(1, attempts + 1):
        try:
            with timed() as t:
                resp = _bedrock().converse(**req)
            usage = resp.get("usage", {})
            tin, tout = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
            cost = _cost(tin, tout)
            if resp.get("stopReason") == "guardrail_intervened":
                db.record_llm_call(invoice_id, agent, settings.bedrock_model_id, tin, tout, t["ms"], cost, False)
                raise RuntimeError("Bedrock Guardrail blocked the request/response")
            for block in resp["output"]["message"]["content"]:
                if "toolUse" in block:
                    db.record_llm_call(invoice_id, agent, settings.bedrock_model_id, tin, tout, t["ms"], cost, True)
                    log_event(log, "llm_call", agent=agent, model=settings.bedrock_model_id, input_tokens=tin,
                              output_tokens=tout, latency_ms=t["ms"], cost_usd=cost,
                              server_latency_ms=resp.get("metrics", {}).get("latencyMs"))
                    return block["toolUse"]["input"]
            db.record_llm_call(invoice_id, agent, settings.bedrock_model_id, tin, tout, t["ms"], cost, False)
            raise ValueError("Model did not return a tool call")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            retryable = code in {"ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException"}
            db.record_llm_call(invoice_id, agent, settings.bedrock_model_id, 0, 0, 0, 0, False)
            if not retryable or attempt == attempts:
                raise
            sleep = min(8, 0.5 * 2 ** attempt) + random.random() * 0.25
            log_event(log, "llm_retry", agent=agent, code=code, attempt=attempt, sleep_s=round(sleep, 2))
            time.sleep(sleep)
    raise RuntimeError("unreachable")
