"""Edge-case contract tests: every user-controllable input must fail cleanly.

A 500 from malformed input is worse than a 400 - it burns the caller's retry
budget and looks like a gateway fault. This script pins the whole matrix.

    python test_edges.py                       # against the running gateway
    python test_edges.py --gateway-key abc     # also checks inbound auth
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys

import httpx
from PIL import Image

BASE = os.environ.get("CHATTEX_BASE", "http://127.0.0.1:18611")
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(name)


def post_raw(client: httpx.Client, path: str, raw: bytes, ctype: str = "application/json"):
    return client.post(path, content=raw, headers={"content-type": ctype})


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--gateway-key", default="")
    args = ap.parse_args()

    print(f"target {args.base}")
    auth = {"authorization": f"Bearer {args.gateway_key}"} if args.gateway_key else {}
    with httpx.Client(base_url=args.base, trust_env=False, timeout=60, headers=auth) as c:
        # --- body shape -------------------------------------------------------
        r = post_raw(c, "/v1/chat/completions", b"[1,2,3]")
        check("array body -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = post_raw(c, "/v1/chat/completions", b"")
        check("empty body -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = post_raw(c, "/v1/chat/completions", b"{not json")
        check("malformed json -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = post_raw(c, "/v1/chat/completions", b'"a string"')
        check("string body -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        # --- field types ------------------------------------------------------
        cases = [
            ("missing messages", {"model": "claude-sonnet-5"}),
            ("messages not array", {"model": "claude-sonnet-5", "messages": {"role": "user"}}),
            ("empty messages", {"model": "claude-sonnet-5", "messages": []}),
            ("bad role", {"model": "claude-sonnet-5", "messages": [{"role": "wizard", "content": "x"}]}),
            ("numeric content", {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": 42}]}),
            ("message not object", {"model": "claude-sonnet-5", "messages": ["hi"]}),
            ("missing model", {"messages": [{"role": "user", "content": "x"}]}),
            ("blank model", {"model": "   ", "messages": [{"role": "user", "content": "x"}]}),
            ("bad temperature", {"model": "claude-sonnet-5",
                                 "messages": [{"role": "user", "content": "x"}], "temperature": "hot"}),
        ]
        for name, payload in cases:
            r = c.post("/v1/chat/completions", json=payload)
            check(f"{name} -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = c.post("/v1/chat/completions",
                   json={"model": "gpt-9-ultra", "messages": [{"role": "user", "content": "x"}]})
        check("unknown model -> 404", r.status_code == 404, f"{r.status_code} {r.text[:80]}")

        # --- stream coercion --------------------------------------------------
        r = c.post("/v1/chat/completions",
                   json={"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "x"}],
                         "stream": "false"})
        check("stream as string 'false' -> non-stream JSON",
              r.status_code == 200 and "choices" in r.json(), f"{r.status_code}")

        # --- image endpoint ---------------------------------------------------
        r = c.post("/v1/images/generations", json={"aspect_ratio": "1:1"})
        check("image: missing prompt -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = c.post("/v1/images/generations", json={"prompt": "x" * 9000})
        check("image: oversized prompt -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = c.post("/v1/images/generations", json={"prompt": "x", "response_format": "webp"})
        check("image: bad response_format -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = c.post("/v1/images/generations", json={"prompt": "x", "aspect_ratio": "7:5"})
        check("image: unknown aspect collapses, not 500", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

        # --- caricature uploads -----------------------------------------------
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, format="PNG")
        r = c.post("/v1/images/caricature", files={"photo": ("a.png", buf.getvalue(), "image/png")})
        check("caricature: valid png -> 200", r.status_code == 200 and r.content[:4] == b"\x89PNG",
              f"{r.status_code} {r.content[:8]!r}")

        r = c.post("/v1/images/caricature", files={"photo": ("a.png", b"not-an-image", "image/png")})
        check("caricature: garbage upload -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        r = c.post("/v1/images/caricature", files={"photo": ("a.png", b"", "image/png")})
        check("caricature: empty upload -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        # truncated PNG: passes verify(), must fail load()
        full = buf.getvalue()
        r = c.post("/v1/images/caricature", files={"photo": ("a.png", full[:80], "image/png")})
        check("caricature: truncated png -> 400", r.status_code == 400, f"{r.status_code} {r.text[:80]}")

        # --- body cap ---------------------------------------------------------
        health = c.get("/health").json()
        max_body = (health.get("limits") or {}).get("max_body_bytes", 24 * 1024 * 1024)
        over = json.dumps({"model": "claude-sonnet-5",
                           "messages": [{"role": "user", "content": "y" * (max_body + 4096)}]}).encode()
        r = post_raw(c, "/v1/chat/completions", over)
        check("oversized body -> 413", r.status_code == 413, f"{r.status_code} {r.text[:80]}")

        # --- blobs ------------------------------------------------------------
        r = c.get("/files/deadbeef.png")
        check("unknown blob -> 404", r.status_code == 404, f"{r.status_code}")

        # --- inbound auth -----------------------------------------------------
        if args.gateway_key:
            anon = httpx.Client(base_url=args.base, trust_env=False, timeout=30)
            r = anon.get("/v1/models")
            check("auth: missing token -> 401", r.status_code == 401, f"{r.status_code}")
            r = anon.get("/v1/models", headers={"authorization": "Bearer wrong-token"})
            check("auth: wrong token -> 401", r.status_code == 401, f"{r.status_code}")
            r = anon.get("/health")
            check("health stays public", r.status_code == 200, f"{r.status_code}")
            anon.close()
            r = c.get("/v1/models")
            check("auth: valid token -> 200", r.status_code == 200, f"{r.status_code}")

    print()
    if failures:
        print(f"FAILED: {len(failures)}: {', '.join(failures)}")
        return 1
    print("ALL EDGE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())