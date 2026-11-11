"""OpenAI-compatible gateway in front of the Chattex upstream pair.

Modes
-----
mock  deterministic text + PNG, zero network.  Default when no key is present.
real  forwards to api.anthropic.com / generativelanguage.googleapis.com.
auto  real if the relevant key is configured, mock otherwise (default).

Endpoints
---------
GET  /health
GET  /v1/models
POST /v1/chat/completions        OpenAI shape, stream and non-stream
POST /v1/images/generations      OpenAI shape, response_format url|b64_json
POST /v1/images/caricature       Chattex-specific: photo -> subject -> caricature
GET  /files/{id}                 in-memory blob store for image URLs
GET  /                           demo page

Every response carries X-Chattex-Source: real|mock so a monitor can never
mistake a mock render for a real one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from PIL import Image

import chattex_client as cc
import prompts

MODE = os.environ.get("CHATTEX_MODE", "auto").strip().lower()
GATEWAY_KEY = os.environ.get("CHATTEX_GATEWAY_KEY", "").strip()
MAX_BODY_BYTES = int(os.environ.get("CHATTEX_MAX_BODY", str(24 * 1024 * 1024)))
MAX_CONCURRENCY = int(os.environ.get("CHATTEX_MAX_CONCURRENCY", "4"))
SLOT_WAIT = float(os.environ.get("CHATTEX_SLOT_WAIT", "30"))
MAX_UPLOAD_BYTES = int(os.environ.get("CHATTEX_MAX_UPLOAD", str(12 * 1024 * 1024)))
PUBLIC_BASE = os.environ.get("CHATTEX_PUBLIC_BASE", "").rstrip("/")

# OpenAI size -> Chattex aspect ratio.  Anything unmapped collapses to 1:1,
# matching GeminiImageGenerator.mapAspectRatio().
SIZE_TO_RATIO = {
    "1024x1024": "1:1", "512x512": "1:1", "256x256": "1:1",
    "1024x1536": "3:4", "768x1024": "3:4",
    "1536x1024": "4:3", "1024x768": "4:3",
    "1792x1024": "16:9", "1344x768": "16:9", "1024x576": "16:9",
    "1024x1792": "9:16", "768x1344": "9:16", "576x1024": "9:16",
}

_state: dict[str, Any] = {}
_blobs: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_BLOB_CAP = 128


# ---------------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["sem"] = asyncio.Semaphore(MAX_CONCURRENCY)
    _state["lock"] = asyncio.Lock()
    _state["counters"] = {"chat": 0, "image": 0, "caricature": 0, "errors": 0, "mock": 0}
    _state["anthropic"] = _try(lambda: cc.AnthropicClient())
    _state["gemini"] = _try(lambda: cc.GeminiClient())
    try:
        yield
    finally:
        for key in ("anthropic", "gemini"):
            client = _state.get(key)
            if client is not None:
                await client.aclose()


def _try(factory):
    try:
        return factory()
    except cc.MissingCredential:
        return None


app = FastAPI(title="Chattex 2API gateway", version="1.0.0", lifespan=lifespan)


def _effective_mode(need: str) -> str:
    """Resolve auto -> real/mock per capability."""
    if MODE in ("mock", "real"):
        return MODE
    client = _state.get("anthropic" if need == "chat" else "gemini")
    return "real" if client is not None else "mock"


def _bump(name: str) -> None:
    counters = _state.get("counters")
    if counters is not None:
        counters[name] = counters.get(name, 0) + 1


def _require(name: str, label: str):
    """A missing credential is a configuration fault, not an upstream fault."""
    client = _state.get(name)
    if client is None:
        raise cc.MissingCredential(
            f"{label} is not configured.",
            detail="Set the API key or run with CHATTEX_MODE=mock.",
        )
    return client


# ---------------------------------------------------------------------------------
# errors / guards
# ---------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: str | None = None) -> None:
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _envelope(code: str, message: str, detail: str | None = None) -> dict[str, Any]:
    err: dict[str, Any] = {"message": message, "type": code, "code": code, "param": None}
    if detail:
        err["detail"] = detail
    return {"error": err}


@app.exception_handler(ApiError)
async def _api_error_handler(_: Request, exc: ApiError):
    return JSONResponse(_envelope(exc.code, exc.message, exc.detail), status_code=exc.status)


@app.exception_handler(cc.UpstreamError)
async def _upstream_error_handler(_: Request, exc: cc.UpstreamError):
    _bump("errors")
    return JSONResponse(_envelope(exc.code, exc.message, exc.detail), status_code=exc.status)


@app.exception_handler(json.JSONDecodeError)
async def _json_error_handler(_: Request, exc: json.JSONDecodeError):
    return JSONResponse(_envelope("invalid_request", "Request body is not valid JSON."), status_code=400)


@app.middleware("http")
async def _guard(request: Request, call_next):
    if request.url.path.startswith("/v1"):
        if GATEWAY_KEY:
            auth = request.headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            if token != GATEWAY_KEY:
                return JSONResponse(
                    _envelope("invalid_api_key", "Missing or invalid gateway token."), status_code=401
                )
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse(
                _envelope("request_too_large", f"Request body exceeds {MAX_BODY_BYTES} bytes."), status_code=413
            )
    response = await call_next(request)
    return response


async def _read_object(request: Request) -> dict[str, Any]:
    """Read the body with a hard byte cap; reject anything that is not a JSON object."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise ApiError(413, "request_too_large", f"Request body exceeds {MAX_BODY_BYTES} bytes.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise ApiError(400, "invalid_request", "Empty request body.")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "invalid_request", "Request body is not valid JSON.") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_request", "Request body must be a JSON object.")
    return body


async def _acquire() -> asyncio.Semaphore:
    sem: asyncio.Semaphore = _state["sem"]
    if SLOT_WAIT <= 0:
        if sem.locked():
            raise ApiError(503, "gateway_busy", "Gateway concurrency limit reached.")
        await sem.acquire()
        return sem
    try:
        await asyncio.wait_for(sem.acquire(), timeout=SLOT_WAIT)
    except asyncio.TimeoutError as exc:
        raise ApiError(503, "gateway_busy", "Timed out waiting for a gateway slot.") from exc
    return sem


# ---------------------------------------------------------------------------------
# blob store
# ---------------------------------------------------------------------------------


def _store_blob(data: bytes, media_type: str) -> str:
    blob_id = hashlib.sha256(data).hexdigest()[:24]
    _blobs[blob_id] = (data, media_type)
    _blobs.move_to_end(blob_id)
    while len(_blobs) > _BLOB_CAP:
        _blobs.popitem(last=False)
    return f"{PUBLIC_BASE}/files/{blob_id}.png"


def _openai_image_payload(png: bytes, media_type: str, response_format: str, revised: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {}
    if response_format == "b64_json":
        item["b64_json"] = base64.b64encode(png).decode("ascii")
    else:
        item["url"] = _store_blob(png, media_type)
    if revised:
        item["revised_prompt"] = revised
    return item


# ---------------------------------------------------------------------------------
# request validation helpers
# ---------------------------------------------------------------------------------


def _coerce_messages(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "invalid_request", "'messages' must be a non-empty array.")
    out: list[dict[str, Any]] = []
    for idx, m in enumerate(raw):
        if not isinstance(m, dict):
            raise ApiError(400, "invalid_request", f"messages[{idx}] must be an object.")
        role = m.get("role")
        if role not in ("system", "user", "assistant", "developer"):
            raise ApiError(400, "invalid_request", f"messages[{idx}].role must be system/user/assistant.")
        content = m.get("content")
        if isinstance(content, list):
            text = "".join(
                str(p.get("text", "")) for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            )
        elif isinstance(content, str):
            text = content
        else:
            raise ApiError(400, "invalid_request", f"messages[{idx}].content must be a string or array.")
        out.append({"role": role, "content": text})
    return out


def _split_system(messages: list[dict[str, Any]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts = [m["content"] for m in messages if m["role"] in ("system", "developer")]
    rest = [m for m in messages if m["role"] in ("user", "assistant")]
    if not rest:
        rest = [{"role": "user", "content": ""}]
    system = "\n\n".join(p for p in system_parts if p) or None
    return system, rest


def _normalize_model(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "invalid_request", "'model' is required.")
    model = raw.strip()
    if model.startswith("chattex/"):
        model = model.split("/", 1)[1]
    if model not in cc.CLAUDE_MODELS:
        raise ApiError(
            404,
            "model_not_found",
            f"Unknown model '{model}'. Available: {', '.join(sorted(cc.CLAUDE_MODELS))}.",
        )
    return model


def _resolve_ratio(body: dict[str, Any]) -> str | None:
    ratio = body.get("aspect_ratio")
    if isinstance(ratio, str) and ratio in prompts.ASPECT_RATIOS:
        return ratio
    size = body.get("size")
    if isinstance(size, str):
        return SIZE_TO_RATIO.get(size.strip())
    return None


# ---------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------


@app.get("/health")
async def health():
    counters = _state.get("counters", {})
    return {
        "status": "ok",
        "mode": MODE,
        "effective": {"chat": _effective_mode("chat"), "image": _effective_mode("image")},
        "anthropic_key": bool(_state.get("anthropic")),
        "gemini_key": bool(_state.get("gemini")),
        "max_concurrency": MAX_CONCURRENCY,
        "limits": {"max_body_bytes": MAX_BODY_BYTES, "max_upload_bytes": MAX_UPLOAD_BYTES},
        "counters": counters,
    }


@app.get("/v1/models")
async def models():
    data = [
        {"id": name, "object": "model", "created": 0, "owned_by": f"chattex-{tier}"}
        for name, tier in cc.CLAUDE_MODELS.items()
    ]
    data += [
        {"id": cc.GEMINI_IMAGE_MODEL, "object": "model", "created": 0, "owned_by": "chattex-image"},
        {"id": cc.GEMINI_VISION_MODEL, "object": "model", "created": 0, "owned_by": "chattex-vision"},
    ]
    return {"object": "list", "data": data}


@app.get("/v1/personas")
async def personas():
    """The eight built-in AiCharacter entries, exposed for the demo page picker."""
    return {
        "data": [
            {"id": p["id"], "name": p["name"], "role": p["role"], "opening": p["opening"]}
            for p in prompts.PERSONAS
        ]
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await _read_object(request)
    messages = _coerce_messages(body.get("messages"))
    model = _normalize_model(body.get("model"))
    system, convo = _split_system(messages)

    want_stream = body.get("stream") is True
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or not (1 <= max_tokens <= 32000):
        max_tokens = cc.DEFAULT_MAX_TOKENS
    temperature = body.get("temperature")
    if temperature is not None and not isinstance(temperature, (int, float)):
        raise ApiError(400, "invalid_request", "'temperature' must be a number.")

    _bump("chat")
    mode = _effective_mode("chat")
    created = int(time.time())
    response_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    if mode == "mock":
        prompt = "\n".join(m["content"] for m in convo)
        sem = await _acquire()
        try:
            result = cc.mock_chat_reply(prompt, model)
        finally:
            sem.release()
        _bump("mock")
        if not want_stream:
            return JSONResponse(
                {
                    "id": response_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": result.text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": result.input_tokens,
                        "completion_tokens": result.output_tokens,
                        "total_tokens": result.input_tokens + result.output_tokens,
                    },
                },
                headers={"X-Chattex-Source": "mock"},
            )

        async def mock_stream():
            yield _sse({"id": response_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
            for piece in _chunk_text(result.text, 24):
                yield _sse({"id": response_id, "object": "chat.completion.chunk", "created": created,
                            "model": model, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            yield _sse({"id": response_id, "object": "chat.completion.chunk", "created": created,
                        "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            mock_stream(), media_type="text/event-stream", headers={"X-Chattex-Source": "mock", "Connection": "close"}
        )

    client: cc.AnthropicClient = _require("anthropic", "Anthropic API key")
    if not want_stream:
        sem = await _acquire()
        try:
            result = await client.complete(convo, model=model, system=system,
                                           max_tokens=max_tokens, temperature=temperature)
        finally:
            sem.release()
        return JSONResponse(
            {
                "id": response_id,
                "object": "chat.completion",
                "created": created,
                "model": result.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.text},
                        "finish_reason": "stop" if result.stop_reason in (None, "end_turn") else result.stop_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": result.input_tokens,
                    "completion_tokens": result.output_tokens,
                    "total_tokens": result.input_tokens + result.output_tokens,
                },
            },
            headers={"X-Chattex-Source": "real", "X-Chattex-Upstream-Model": result.model},
        )

    sem = await _acquire()
    stream_iter = client.stream(convo, model=model, system=system, max_tokens=max_tokens, temperature=temperature)
    return StreamingResponse(
        _anthropic_sse(stream_iter, response_id, created, model, sem),
        media_type="text/event-stream",
        headers={"X-Chattex-Source": "real", "Connection": "close"},
    )


async def _anthropic_sse(events, response_id: str, created: int, model: str, sem: asyncio.Semaphore):
    try:
        async for event in events:
            etype = event.get("type")
            if etype == "content_block_delta":
                delta = event.get("delta") or {}
                text = delta.get("text")
                if text:
                    yield _sse({"id": response_id, "object": "chat.completion.chunk", "created": created,
                                "model": model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
            elif etype == "message_stop":
                yield _sse({"id": response_id, "object": "chat.completion.chunk", "created": created,
                            "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        yield "data: [DONE]\n\n"
    finally:
        sem.release()


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    body = await _read_object(request)
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ApiError(400, "invalid_request", "'prompt' is required.")
    if len(prompt) > 8000:
        raise ApiError(400, "invalid_request", "'prompt' exceeds 8000 characters.")

    style = body.get("style")
    style_id = style if isinstance(style, str) and style in prompts.STYLE_MODIFIERS else "none"
    ratio = _resolve_ratio(body)
    fmt = body.get("response_format", "url")
    if fmt not in ("url", "b64_json"):
        raise ApiError(400, "invalid_request", "'response_format' must be 'url' or 'b64_json'.")

    final_prompt = prompts.build_image_gen_prompt(prompt, style_id, ratio or prompts.DEFAULT_ASPECT_RATIO)

    _bump("image")
    mode = _effective_mode("image")
    sem = await _acquire()
    try:
        if mode == "mock":
            _bump("mock")
            # PNG rasterisation is CPU-bound; keep it off the event loop.
            result = await asyncio.to_thread(cc.mock_image, final_prompt, ratio)
        else:
            result = await _require("gemini", "Gemini API key").generate_image(
                final_prompt, aspect_ratio=ratio
            )
    finally:
        sem.release()

    payload = _openai_image_payload(result.png, result.media_type, fmt, final_prompt)
    return JSONResponse(
        {"created": int(time.time()), "data": [payload]}, headers={"X-Chattex-Source": mode}
    )


@app.post("/v1/images/caricature")
async def caricature(
    photo: UploadFile = File(...),
    style_id: str = Form("pop_art"),
    exaggeration: str = Form("Medium"),
    color_style: str = Form("Vibrant"),
    regenerate: bool = Form(False),
    subject: str = Form(""),
):
    raw = await photo.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ApiError(413, "request_too_large", f"Upload exceeds {MAX_UPLOAD_BYTES} bytes.")
    if not raw:
        raise ApiError(400, "invalid_request", "Empty upload.")
    try:
        media_type = await asyncio.to_thread(_decode_upload, raw)
    except Exception as exc:
        raise ApiError(400, "invalid_request", "Uploaded file is not a decodable image.") from exc

    if style_id not in prompts.STYLE_MODIFIERS:
        style_id = "pop_art"
    if exaggeration not in prompts.EXAGGERATION_MODIFIERS:
        exaggeration = "Medium"
    if color_style not in prompts.COLOR_MODIFIERS:
        color_style = "Vibrant"

    _bump("caricature")
    mode = _effective_mode("image")

    if mode == "mock":
        _bump("mock")
        final = prompts.build_caricature_prompt(style_id, exaggeration, color_style, regenerate)
        sem = await _acquire()
        try:
            result = await asyncio.to_thread(cc.mock_image, final, "1:1")
        finally:
            sem.release()
        return Response(
            content=result.png,
            media_type="image/png",
            headers={
                "X-Chattex-Source": "mock",
                "X-Chattex-Prompt": _b64(final),
                "X-Chattex-Subject": _b64(subject or "MOCK subject"),
            },
        )

    client: cc.GeminiClient = _require("gemini", "Gemini API key")
    sem = await _acquire()
    try:
        subject_text = subject.strip()
        if not subject_text:
            subject_text = await client.describe_image(raw, media_type)
        # i8.c(): final = "<caricature prompt> Subject Description: <vision text>"
        full = prompts.build_caricature_prompt(
            style_id, exaggeration, color_style, regenerate
        ) + " Subject Description: " + subject_text
        result = await client.generate_image(full, aspect_ratio="1:1")
    finally:
        sem.release()

    return Response(
        content=result.png,
        media_type=result.media_type,
        headers={
            "X-Chattex-Source": "real",
            "X-Chattex-Subject": _b64(subject_text),
            "X-Chattex-Prompt": _b64(full),
        },
    )


@app.get("/files/{blob_id}")
async def get_blob(blob_id: str):
    key = blob_id.split(".", 1)[0]
    entry = _blobs.get(key)
    if entry is None:
        raise ApiError(404, "not_found", "Blob not found or evicted.")
    data, media_type = entry
    return Response(content=data, media_type=media_type, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/", response_class=HTMLResponse)
async def index():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "demo.html")
    with open(path, "r", encoding="utf-8") as fh:
        return HTMLResponse(fh.read())


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------


def _sse(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _decode_upload(raw: bytes) -> str:
    """verify() alone accepts truncated files; load() forces a real decode."""
    img = Image.open(io.BytesIO(raw))
    img.verify()
    img = Image.open(io.BytesIO(raw))
    img.load()
    return Image.MIME.get(img.format or "", "image/png")


def _chunk_text(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("CHATTEX_PORT", "18611"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")