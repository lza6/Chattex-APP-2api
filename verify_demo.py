"""Browser-level verification of the demo page (Playwright, real Chromium).

Asserts the page actually works end to end - not just that it returns 200:
health badge, persona list, non-stream chat, image generation, caricature upload,
and zero unexpected console errors.

    python verify_demo.py
"""

from __future__ import annotations

import io
import os
import sys

from PIL import Image
from playwright.sync_api import sync_playwright

BASE = os.environ.get("CHATTEX_BASE", "http://127.0.0.1:18611")
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")

failures: list[str] = []
console_errors: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else '  <- ' + detail}")
    if not cond:
        failures.append(name)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    os.makedirs(SHOT_DIR, exist_ok=True)

    probe = os.path.join(SHOT_DIR, "probe_face.png")
    Image.new("RGB", (320, 320), (120, 90, 70)).save(probe, format="PNG")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        page.goto(BASE + "/", wait_until="networkidle")
        page.wait_for_timeout(1200)

        badge = page.inner_text("#modeBadge")
        check("mode badge resolved", "mock" in badge or "real" in badge, badge)
        check("mock banner visible", page.is_visible("#banner.on"), "banner hidden")
        check("health panel filled", "并发上限" in page.inner_text("#health"), page.inner_text("#health")[:80])

        persona_opts = page.eval_on_selector_all("#persona option", "els => els.length")
        check("persona picker populated", persona_opts >= 9, f"{persona_opts} options")

        style_opts = page.eval_on_selector_all("#imageStyle option", "els => els.length")
        check("style picker populated", style_opts == 8, f"{style_opts} options")

        # --- chat (streaming) ---------------------------------------------------
        page.fill("#chatPrompt", "playwright probe: reply with one short sentence")
        page.select_option("#stream", "0")
        page.click("#sendChat")
        page.wait_for_function(
            "() => [...document.querySelectorAll('#chatLog .msg.bot .txt')].some(e => e.textContent.length > 0)",
            timeout=30000,
        )
        bot_text = page.inner_text("#chatLog .msg.bot .txt")
        check("chat reply rendered", len(bot_text) > 5 and "请求失败" not in bot_text, bot_text[:120])
        check("chat reply came from gateway", "[MOCK]" in bot_text or len(bot_text) > 20, bot_text[:80])

        # --- chat (streaming) ---------------------------------------------------
        page.select_option("#stream", "1")
        page.click("#sendChat")
        page.wait_for_function(
            "() => document.querySelectorAll('#chatLog .msg.bot .txt').length >= 2 "
            "&& document.querySelectorAll('#chatLog .msg.bot .txt')[1].textContent.length > 5",
            timeout=30000,
        )
        stream_text = page.eval_on_selector_all(
            "#chatLog .msg.bot .txt", "els => els[els.length-1].textContent"
        )
        check("streaming reply rendered",
              len(stream_text) > 5 and "请求失败" not in stream_text, stream_text[:120])
        page.screenshot(path=os.path.join(SHOT_DIR, "demo_chat.png"))

        # --- image generation ---------------------------------------------------
        page.click('.tab[data-pane="image"]')
        page.select_option("#imageRatio", "16:9")
        page.click("#sendImage")
        page.wait_for_function(
            "() => document.querySelector('#imageOut img') !== null",
            timeout=60000,
        )
        page.wait_for_timeout(500)
        img_ok = page.eval_on_selector(
            "#imageOut img", "el => el.complete && el.naturalWidth > 0"
        )
        check("generated image decoded", bool(img_ok), "image element present but not decoded")
        check("image source labelled", "mock" in page.inner_text("#imageOut figcaption").lower()
              or "real" in page.inner_text("#imageOut figcaption").lower(),
              page.inner_text("#imageOut figcaption"))
        page.screenshot(path=os.path.join(SHOT_DIR, "demo_image.png"))

        # --- caricature ---------------------------------------------------------
        page.click('.tab[data-pane="caricature"]')
        page.set_input_files("#photo", probe)
        page.click("#sendCar")
        page.wait_for_function(
            "() => document.querySelector('#carOut img') !== null",
            timeout=60000,
        )
        page.wait_for_timeout(500)
        car_ok = page.eval_on_selector("#carOut img", "el => el.complete && el.naturalWidth > 0")
        check("caricature image decoded", bool(car_ok), "caricature element not decoded")
        page.screenshot(path=os.path.join(SHOT_DIR, "demo_caricature.png"))

        unexpected = [e for e in console_errors if "favicon" not in e.lower()]
        check("no console errors", not unexpected, "; ".join(unexpected[:3]))

        browser.close()

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())