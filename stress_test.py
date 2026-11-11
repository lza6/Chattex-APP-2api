"""Local stress harness for the Chattex gateway.

Defaults are deliberately safe: it only hammers mock mode unless you pass
--allow-real, because every real call spends real upstream quota.

    python stress_test.py                       # mock, 4 rounds
    python stress_test.py --rounds 6 --concurrency 1,4,8,16
    python stress_test.py --allow-real          # explicit opt-in, costs money
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from PIL import Image

DEFAULT_BASE = os.environ.get("CHATTEX_BASE", "http://127.0.0.1:18611")
DEFAULT_ROUNDS = 4
DEFAULT_CONCURRENCY = (1, 4, 8)


@dataclass
class Outcome:
    ok: bool
    kind: str
    latency: float
    status: int = 0
    note: str = ""


@dataclass
class Report:
    tier: str
    wall: float
    outcomes: list[Outcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def ok(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    def p(self, pct: float) -> float:
        lat = sorted(o.latency for o in self.outcomes)
        if not lat:
            return 0.0
        idx = min(len(lat) - 1, max(0, int(round(pct / 100.0 * (len(lat) - 1)))))
        return lat[idx]


async def _health(client: httpx.AsyncClient) -> dict[str, Any]:
    try:
        r = await client.get("/health", timeout=10)
        return r.json() if r.status_code == 200 else {"error": r.status_code}
    except Exception as exc:
        return {"error": str(exc)}


async def _one_chat(client: httpx.AsyncClient, idx: int, stream: bool) -> Outcome:
    payload = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": f"stress probe {idx}: reply with one sentence."}],
        "stream": stream,
        "max_tokens": 128,
    }
    t0 = time.perf_counter()
    try:
        if stream:
            async with client.stream("POST", "/v1/chat/completions", json=payload) as r:
                if r.status_code != 200:
                    body = await r.aread()
                    return Outcome(False, "chat-stream", time.perf_counter() - t0, r.status_code,
                                   body.decode("utf-8", "replace")[:160])
                got = 0
                async for line in r.aiter_lines():
                    if line.startswith("data:") and "[DONE]" not in line:
                        got += 1
                return Outcome(got > 0, "chat-stream", time.perf_counter() - t0, 200,
                               "" if got else "empty stream")
        r = await client.post("/v1/chat/completions", json=payload)
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            return Outcome(False, "chat", dt, r.status_code, r.text[:160])
        d = r.json()
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        return Outcome(bool(text), "chat", dt, 200, "" if text else "empty content")
    except Exception as exc:
        return Outcome(False, "chat", time.perf_counter() - t0, 0, f"{type(exc).__name__}: {exc}")


async def _one_image(client: httpx.AsyncClient, idx: int) -> Outcome:
    payload = {
        "model": "gemini-2.5-flash-image",
        "prompt": f"stress probe {idx}: a single orange cube on a dark table, studio light",
        "aspect_ratio": "1:1",
        "response_format": "b64_json" if idx % 5 == 0 else "url",
    }
    t0 = time.perf_counter()
    try:
        r = await client.post("/v1/images/generations", json=payload)
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            return Outcome(False, "image", dt, r.status_code, r.text[:160])
        d = r.json()
        item = (d.get("data") or [{}])[0]
        if not item.get("url") and not item.get("b64_json"):
            return Outcome(False, "image", dt, 200, "no url/b64 in payload")
        return Outcome(True, "image", dt, 200)
    except Exception as exc:
        return Outcome(False, "image", time.perf_counter() - t0, 0, f"{type(exc).__name__}: {exc}")


async def _one_caricature(client: httpx.AsyncClient, idx: int) -> Outcome:
    buf = io.BytesIO()
    Image.new("RGB", (256, 256), (40 + idx % 60, 80, 120)).save(buf, format="PNG")
    files = {"photo": ("probe.png", buf.getvalue(), "image/png")}
    data = {"style_id": "pop_art", "exaggeration": "Medium", "color_style": "Vibrant"}
    t0 = time.perf_counter()
    try:
        r = await client.post("/v1/images/caricature", files=files, data=data)
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            return Outcome(False, "caricature", dt, r.status_code, r.text[:160])
        return Outcome(len(r.content) > 128, "caricature", dt, 200,
                       "" if len(r.content) > 128 else "tiny body")
    except Exception as exc:
        return Outcome(False, "caricature", time.perf_counter() - t0, 0, f"{type(exc).__name__}: {exc}")


_WORKERS = {"chat": _one_chat, "image": _one_image, "caricature": _one_caricature}


async def run_tier(base: str, kind: str, concurrency: int, rounds: int, headers: dict[str, str]) -> Report:
    limits = httpx.Limits(max_connections=concurrency * 4, max_keepalive_connections=concurrency * 2)
    async with httpx.AsyncClient(base_url=base, timeout=180.0, limits=limits,
                                 trust_env=False, headers=headers) as client:
        tasks: list[tuple[int, asyncio.Task]] = []
        t0 = time.perf_counter()
        for r in range(rounds):
            for i in range(concurrency):
                idx = r * concurrency + i
                if kind == "chat":
                    task = asyncio.create_task(_one_chat(client, idx, stream=(idx % 2 == 1)))
                else:
                    task = asyncio.create_task(_WORKERS[kind](client, idx))
                tasks.append((idx, task))
        results = await asyncio.gather(*(t for _, t in tasks))
        wall = time.perf_counter() - t0
    return Report(tier=f"{kind} c={concurrency} r={rounds}", wall=wall, outcomes=list(results))


def print_report(rep: Report) -> None:
    rate = 100.0 * rep.ok / rep.total if rep.total else 0.0
    rps = rep.total / rep.wall if rep.wall else 0.0
    print(f"  {rep.tier:<24} ok {rep.ok:>3}/{rep.total:<3} ({rate:5.1f}%)  "
          f"wall {rep.wall:6.2f}s  rps {rps:6.2f}  "
          f"p50 {rep.p(50)*1000:7.1f}ms  p95 {rep.p(95)*1000:7.1f}ms")
    for o in rep.outcomes:
        if not o.ok:
            print(f"      !! {o.kind} status={o.status} {o.note}")


async def determinism_check(base: str, headers: dict[str, str]) -> None:
    """Mock mode must be byte-deterministic; anything else hides regressions."""
    async with httpx.AsyncClient(base_url=base, timeout=60.0, trust_env=False, headers=headers) as client:
        payload = {"model": "claude-sonnet-5",
                   "messages": [{"role": "user", "content": "determinism probe"}],
                   "stream": False}
        texts = set()
        for _ in range(3):
            r = await client.post("/v1/chat/completions", json=payload)
            texts.add(json.dumps(r.json().get("choices"), sort_keys=True))
        print(f"  chat determinism: {'PASS' if len(texts) == 1 else 'FAIL'} ({len(texts)} distinct replies)")

        img_payload = {"prompt": "determinism probe image", "aspect_ratio": "1:1", "response_format": "b64_json"}
        digests = set()
        for _ in range(3):
            r = await client.post("/v1/images/generations", json=img_payload)
            item = (r.json().get("data") or [{}])[0]
            digests.add(hash(item.get("b64_json", "")))
        print(f"  image determinism: {'PASS' if len(digests) == 1 else 'FAIL'} ({len(digests)} distinct payloads)")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--kinds", default="chat,image,caricature")
    ap.add_argument("--concurrency", default=",".join(str(c) for c in DEFAULT_CONCURRENCY))
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    ap.add_argument("--allow-real", action="store_true")
    ap.add_argument("--token", default=os.environ.get("CHATTEX_GATEWAY_KEY", ""))
    args = ap.parse_args()

    headers = {"authorization": f"Bearer {args.token}"} if args.token else {}
    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    conc = [int(c) for c in args.concurrency.split(",") if c.strip()]

    print(f"target {args.base}  rounds={args.rounds}  tiers={conc}")
    async with httpx.AsyncClient(base_url=args.base, timeout=20.0, trust_env=False, headers=headers) as c:
        health = await _health(c)
    print(f"health: {json.dumps(health, ensure_ascii=False)}")

    effective = health.get("effective", {}) if isinstance(health, dict) else {}
    if not args.allow_real and any(v == "real" for v in effective.values()):
        print("gateway is in REAL mode but --allow-real was not passed; aborting.", file=sys.stderr)
        return 2

    print("\ndeterminism:")
    await determinism_check(args.base, headers)

    print("\nload:")
    reports: list[Report] = []
    for kind in kinds:
        for c in conc:
            reports.append(await run_tier(args.base, kind, c, args.rounds, headers))
            print_report(reports[-1])

    total = sum(r.total for r in reports)
    ok = sum(r.ok for r in reports)
    print(f"\nsummary: {ok}/{total} ok ({100.0*ok/total:.1f}%)")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))