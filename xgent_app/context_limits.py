"""Explicit/discovered request limits. Never trim a request to make it fit."""

from __future__ import annotations

import json
import math
from typing import Any

from xgent_app.attachments import AttachmentContextError


DEFAULT_OUTPUT_RESERVE = 4096


class ConversationRequest(list):
    def __init__(self, history: list, limits: dict, system_prompt: str):
        super().__init__(history)
        self.limits = limits
        self.system_prompt = system_prompt


def limits_from_model_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for source, target in (
        ("context_length", "context_window"),
        ("context_window", "context_window"),
        ("inputTokenLimit", "max_input_tokens"),
        ("outputTokenLimit", "max_output_tokens"),
        ("max_images", "max_images"),
        ("max_request_bytes", "max_request_bytes"),
    ):
        value = metadata.get(source)
        minimum = 0 if target == "max_images" else 1
        if type(value) is int and value >= minimum:
            result[target] = value
    if isinstance(metadata.get("supports_images"), bool):
        result["supports_images"] = metadata["supports_images"]
    architecture = metadata.get("architecture") or {}
    modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
    if isinstance(modalities, list) and modalities:
        result["supports_images"] = "image" in modalities
    return result


def validate_limits(limits: dict) -> dict:
    if not isinstance(limits, dict):
        raise AttachmentContextError("Model request limits must be a JSON object")
    result = dict(limits)
    numeric_keys = (
        "context_window", "max_input_tokens", "max_output_tokens", "max_images",
        "max_request_bytes", "output_reserve_tokens", "image_token_budget",
    )
    unknown = result.keys() - {*numeric_keys, "supports_images"}
    if unknown:
        raise AttachmentContextError(f"Unknown model request limits: {sorted(unknown, key=str)}")
    for key in numeric_keys:
        if key not in result:
            continue
        value = result[key]
        minimum = 0 if key == "max_images" else 1
        if type(value) is not int or value < minimum:
            raise AttachmentContextError(f"Invalid model request limit: {key}={value!r}")
    if "supports_images" in result and not isinstance(result["supports_images"], bool):
        raise AttachmentContextError("supports_images must be a boolean")
    return result


def reserved_output_tokens(limits: dict, max_tokens: int | None) -> int | None:
    if max_tokens is not None:
        if type(max_tokens) is not int or max_tokens <= 0:
            raise AttachmentContextError("Output token reservation must be a positive integer")
        return max_tokens
    if any(key in limits for key in (
        "context_window", "max_input_tokens", "max_output_tokens", "output_reserve_tokens",
    )):
        return limits.get("output_reserve_tokens", min(
            DEFAULT_OUTPUT_RESERVE, limits.get("max_output_tokens", DEFAULT_OUTPUT_RESERVE),
        ))
    return None


def estimate_input_tokens(history: list, system_prompt: str, limits: dict) -> int:
    # Without a provider tokenizer, UTF-8 bytes give a conservative text budget.
    # Images are estimated from their pixel patches; upstream remains authoritative.
    tokens = len(system_prompt.encode("utf-8")) + 16
    for message in history:
        tokens += 16
        content = message.get("content")
        if isinstance(content, str):
            tokens += len(content.encode("utf-8"))
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    tokens += len(str(part.get("text", "")).encode("utf-8"))
                elif part.get("type") == "image":
                    width, height = part.get("width") or 1024, part.get("height") or 1024
                    tokens += limits.get(
                        "image_token_budget",
                        max(4096, math.ceil(width / 32) * math.ceil(height / 32) * 4),
                    )
    return tokens


def validate_request_body(body: dict, history: list) -> None:
    if not isinstance(history, ConversationRequest):
        return
    limits = history.limits
    images = [part for msg in history if isinstance(msg.get("content"), list)
              for part in msg["content"] if part.get("type") == "image"]
    if images and limits.get("supports_images") is False:
        raise AttachmentContextError("The selected model does not support images")
    if limits.get("max_images") is not None and len(images) > limits["max_images"]:
        raise AttachmentContextError(
            f"Image count {len(images)} exceeds model limit {limits['max_images']}"
        )
    if limits.get("max_request_bytes") is not None:
        size = len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > limits["max_request_bytes"]:
            raise AttachmentContextError(
                f"Full request body is {size} bytes; limit is {limits['max_request_bytes']}"
            )
    output = (body.get("max_completion_tokens") or body.get("max_tokens")
              or body.get("generationConfig", {}).get("maxOutputTokens")
              or limits.get("output_reserve_tokens", DEFAULT_OUTPUT_RESERVE))
    if limits.get("max_output_tokens") is not None and output > limits["max_output_tokens"]:
        raise AttachmentContextError(
            f"Output reservation {output} exceeds model limit {limits['max_output_tokens']}"
        )
    if "context_window" not in limits and "max_input_tokens" not in limits:
        return
    tokens = estimate_input_tokens(history, history.system_prompt, limits)
    if limits.get("max_input_tokens") is not None and tokens > limits["max_input_tokens"]:
        raise AttachmentContextError(
            f"Full input conservative estimate {tokens} tokens exceeds input limit "
            f"{limits['max_input_tokens']} (output reservation: {output})"
        )
    if limits.get("context_window") is not None and tokens + output > limits["context_window"]:
        raise AttachmentContextError(
            f"Full input conservative estimate {tokens} + output reservation {output} "
            f"exceeds context window {limits['context_window']}"
        )
