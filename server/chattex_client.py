"""Chattex upstream clients: Anthropic Messages + Gemini generateContent.

Protocols recovered from com.chatbot.chattexai 1.0.15 (see docs/PROTOCOL.md).
Both upstreams are stock public APIs - there is no private envelope, no signing,
no device registration. The only thing the app adds is the prompt layer.

Credentials are BYOK: pass them explicitly or set CHATTEX_ANTHROPIC_KEY /
CHATTEX_GEMINI_KEY.  Without a key the clients raise MissingCredential so the
gateway can fall back to mock mode.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from PIL import Image, ImageDraw

from prompts import ASPECT_RATIOS, DEFAULT_ASPECT_RATIO

ANTHROPIC_BASE = "https://api.anthropic.com/v1"
GEMINI_BASE = "https://generativelanguage.googleapis.com"

ANTHROPIC_VERSION = "2023-06-01"
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
GEMINI_VISION_MODEL = "gemini-3.7-flash"

# Models the client offers in its picker (hd0/l7/r9/ud1 all switch on this set).
CLAUDE_MODELS = {
    "claude-opus-5": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-haiku-4-5-20251001": "haiku",
}
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 4096

# The client's own connect/read/call timeouts for the two Retrofit stacks.
CLAUDE_TIMEOUT = httpx.Timeout(connect=60.0, read=120.0, write=60.0, pool=60.0)
GEMINI_TIMEOUT = httpx.Timeout(connect=60.0, read=120.0, write=60.0, pool=60.0)


class UpstreamError(Exception):
    """Base for everything the gateway maps onto an OpenAI error envelope."""

    status = 502
    code = "upstream_error"

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class MissingCredential(UpstreamError):
    status = 500
    code = "configuration_error"


class AuthError(UpstreamError):
    status = 502
    code = "upstream_auth_error"


class RateLimited(UpstreamError):
    status = 429
    code = "upstream_rate_limited"


class ContentBlocked(UpstreamError):
    status = 400
    code = "content_filter"


class BadRequest(UpstreamError):
    status = 400
    code = "invalid_request"


class ServerError(UpstreamError):
    status = 502
    code = "upstream_server_error"


class NoImage(UpstreamError):
    status = 502
    code = "upstream_empty_output"


@dataclass
class ChatResult:
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    source: str


@dataclass
class ImageResult:
    png: bytes
    media_type: str
    source: str
    note: str = ""


def _norm_aspect(ratio: str | None) -> str | None:
    """Client only forwards these five; anything else collapses to 1:1."""
    if ratio in ASPECT_RATIOS and ratio != DEFAULT_ASPECT_RATIO:
        return ratio
    return None  # 1:1 == omit imageConfig entirely, exactly like the app


def _map_http(status: int, what: str, body: str) -> UpstreamError:
    snippet = (body or "").strip()[:400]
    if status in (401, 403):
        return AuthError(f"{what} rejected the API key (HTTP {status}).", detail=snippet)
    if status == 429:
        return RateLimited(f"{what} rate limit or quota exceeded.", detail=snippet)
    if status == 400:
        low = snippet.lower()
        if "policy" in low or "safety" in low or "blocked" in low:
            return ContentBlocked(f"{what} refused the prompt under its safety policy.", detail=snippet)
        return BadRequest(f"{what} rejected the request (HTTP 400).", detail=snippet)
    if status == 404:
        return BadRequest(f"{what} endpoint or model not found (HTTP 404).", detail=snippet)
    if status >= 500:
        return ServerError(f"{what} server error (HTTP {status}).", detail=snippet)
    return UpstreamError(f"{what} returned unexpected HTTP {status}.", detail=snippet)


# ---------------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------------


class AnthropicClient:
    """POST /v1/messages, x-api-key + anthropic-version - the app's exact shape."""

    def __init__(self, api_key: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = (api_key or os.environ.get("CHATTEX_ANTHROPIC_KEY") or "").strip()
        if not self.api_key:
            raise MissingCredential(
                "No Anthropic API key configured.",
                detail="Set CHATTEX_ANTHROPIC_KEY or run the gateway in mock mode.",
            )
        self._client = client
        self._owns = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=CLAUDE_TIMEOUT, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    def _payload(
        self,
        messages: list[dict[str, Any]],
        model: str,
        system: str | None,
        max_tokens: int,
        temperature: float | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        return body

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = DEFAULT_CLAUDE_MODEL,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
    ) -> ChatResult:
        payload = self._payload(messages, model, system, max_tokens, temperature)
        client = await self._get_client()
        try:
            resp = await client.post(f"{ANTHROPIC_BASE}/messages", json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ServerError("Anthropic request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ServerError(f"Could not reach Anthropic: {exc}") from exc

        if resp.status_code != 200:
            raise _map_http(resp.status_code, "Anthropic", await _safe_text(resp))

        data = _load_json(resp)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ServerError("Anthropic response had no content array.", detail=str(data)[:300])
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            model=data.get("model", model),
            stop_reason=data.get("stop_reason"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            source="real",
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str = DEFAULT_CLAUDE_MODEL,
        system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield Anthropic SSE events verbatim (already wire-format, no repackaging)."""
        payload = self._payload(messages, model, system, max_tokens, temperature)
        payload["stream"] = True
        client = await self._get_client()
        try:
            async with client.stream(
                "POST", f"{ANTHROPIC_BASE}/messages", json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise _map_http(resp.status_code, "Anthropic", body)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        continue
        except httpx.TimeoutException as exc:
            raise ServerError("Anthropic stream timed out.") from exc
        except httpx.HTTPError as exc:
            raise ServerError(f"Anthropic stream failed: {exc}") from exc


# ---------------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------------


class GeminiClient:
    """v1beta/models/{model}:generateContent with x-goog-api-key."""

    def __init__(self, api_key: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = (api_key or os.environ.get("CHATTEX_GEMINI_KEY") or "").strip()
        if not self.api_key:
            raise MissingCredential(
                "No Gemini API key configured.",
                detail="Set CHATTEX_GEMINI_KEY or run the gateway in mock mode.",
            )
        self._client = client
        self._owns = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=GEMINI_TIMEOUT, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, model: str, body: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        url = f"{GEMINI_BASE}/v1beta/models/{model}:generateContent"
        try:
            resp = await client.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        except httpx.TimeoutException as exc:
            raise ServerError("Gemini request timed out.") from exc
        except httpx.HTTPError as exc:
            raise ServerError(f"Could not reach Gemini: {exc}") from exc

        if resp.status_code != 200:
            raise _map_http(resp.status_code, "Gemini", await _safe_text(resp))
        return _load_json(resp)

    async def describe_image(self, image_bytes: bytes, mime: str = "image/png") -> str:
        """The caricature pre-pass: Gemini 3.7 Flash reads the photo and describes it.

        The app sends the bitmap with no text part at all; we keep that shape.
        """
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime,
                                "data": base64.b64encode(image_bytes).decode("ascii"),
                            }
                        }
                    ]
                }
            ]
        }
        data = await self._post(GEMINI_VISION_MODEL, body)
        return _extract_text(data) or "A person"

    async def generate_image(
        self,
        prompt: str,
        *,
        aspect_ratio: str | None = None,
        model: str = GEMINI_IMAGE_MODEL,
    ) -> ImageResult:
        gen_config: dict[str, Any] = {"responseModalities": ["TEXT", "IMAGE"]}
        ratio = _norm_aspect(aspect_ratio)
        if ratio:
            gen_config["imageConfig"] = {"aspectRatio": ratio}

        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        data = await self._post(model, body)

        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ContentBlocked(
                f"Gemini blocked the prompt: {feedback.get('blockReason')}",
                detail=json.dumps(feedback)[:300],
            )

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise NoImage("Gemini returned no candidates.", detail=str(data)[:300])

        cand = candidates[0] if isinstance(candidates[0], dict) else {}
        finish = cand.get("finishReason")
        if finish and finish != "STOP":
            raise ContentBlocked(f"Gemini stopped early: {finish}")

        parts = (cand.get("content") or {}).get("parts") or []
        note = ""
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                raw = base64.b64decode(inline["data"])
                return ImageResult(png=raw, media_type=inline.get("mimeType") or "image/png", source="real")
            if part.get("text"):
                note = part["text"][:200]

        raise NoImage("Gemini replied without an image part.", detail=note or str(data)[:300])


# ---------------------------------------------------------------------------------
# mock providers - deterministic, so stress tests can assert on bytes
# ---------------------------------------------------------------------------------


def mock_chat_reply(prompt: str, model: str) -> ChatResult:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    text = (
        f"[MOCK] {model} echo #{digest}. "
        f"You said: {prompt.strip()[:160]}"
    )
    return ChatResult(
        text=text,
        model=model,
        stop_reason="end_turn",
        input_tokens=max(1, len(prompt) // 4),
        output_tokens=max(1, len(text) // 4),
        source="mock",
    )


def mock_image(prompt: str, aspect_ratio: str | None) -> ImageResult:
    ratio = aspect_ratio if aspect_ratio in ASPECT_RATIOS else DEFAULT_ASPECT_RATIO
    w, h = {
        "1:1": (512, 512),
        "3:4": (384, 512),
        "4:3": (512, 384),
        "16:9": (512, 288),
        "9:16": (288, 512),
    }[ratio]
    digest = hashlib.sha256(f"{prompt}|{ratio}".encode("utf-8")).hexdigest()[:8]
    img = Image.new("RGB", (w, h), (18, 18, 22))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        shade = int(24 + 70 * (y / max(1, h - 1)))
        draw.line([(0, y), (w, y)], fill=(shade, shade // 2 + 20, shade // 3 + 40))
    draw.rectangle([8, 8, w - 9, h - 9], outline=(230, 120, 60), width=2)
    draw.text((16, h - 46), "MOCK / NO UPSTREAM", fill=(255, 220, 200))
    draw.text((16, h - 30), f"{ratio}  {digest}", fill=(255, 220, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return ImageResult(png=buf.getvalue(), media_type="image/png", source="mock", note="mock render")


def mock_stress_png(seed: str, size: int = 256) -> bytes:
    """Cheap deterministic PNG used by the stress harness."""
    h = hashlib.sha256(seed.encode()).digest()
    img = Image.new("RGB", (size, size), (h[0], h[1], h[2]))
    draw = ImageDraw.Draw(img)
    for i in range(0, size, 8):
        draw.line([(i, 0), (size - i, size)], fill=(h[3], h[4], h[5]), width=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------


async def _safe_text(resp: httpx.Response) -> str:
    try:
        return (await resp.aread()).decode("utf-8", "replace")
    except Exception:
        return ""


def _load_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception as exc:
        raise ServerError("Upstream returned a non-JSON response.", detail=resp.text[:300]) from exc
    if not isinstance(data, dict):
        raise ServerError("Upstream returned a non-object JSON body.", detail=str(data)[:300])
    return data


def _extract_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return ""
    cand = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = (cand.get("content") or {}).get("parts") or []
    return "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()