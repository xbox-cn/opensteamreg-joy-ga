# -*- coding: utf-8 -*-
"""GA Windows + SunBrowser：领取判定流程（login → claim → buy → extract）。

机器级去重判定：GA runner 每次都是全新机器。若 claim 过 → 去重键=物理机；
仍报「该设备指纹已领过」→ 去重键穿透到 Turnstile/账号级。

env: JOY_EMAIL / JOY_PASSWORD / CDP（默认 http://127.0.0.1:9222）
输出: 每步日志 + JOY_RESULT={json}
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright
from webui import joy_browser as JB

EMAIL = os.environ.get("JOY_EMAIL", "")
PASSWORD = os.environ.get("JOY_PASSWORD", "")
CDP = os.environ.get("CDP", "http://127.0.0.1:9222")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    out = {"ok": False, "email": EMAIL, "balance": None, "claim": "", "extract_url": ""}
    with sync_playwright() as p:
        br = p.chromium.connect_over_cdp(CDP)
        ctx = br.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log(f"connected: {br.version}")

        try:
            page.goto("https://api.ipify.org", timeout=30000)
            out["exit_ip"] = (page.inner_text("body") or "").strip()
            log(f"exit_ip={out['exit_ip']}")
        except Exception as e:
            out["exit_ip"] = ""
            log(f"ip_check_err={type(e).__name__}")

        page.goto(JB.BASE + "/login.html", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2500)
        JB._dismiss_cookie(page, log)
        ok, detail, jwt = JB.login_via_api(page, EMAIL, PASSWORD, log=log)
        log(f"login_api={ok} {detail[:120]}")
        if not ok:
            page.fill("#email", EMAIL)
            page.fill("#password", PASSWORD)
            page.wait_for_timeout(500)
            tok = JB._solve_turnstile(page, "login", log)
            if not tok:
                out["claim"] = "登录 Turnstile 没过"
                print("JOY_RESULT=" + json.dumps(out, ensure_ascii=False))
                return 2
            page.click("#loginBtn")
            for _ in range(40):
                page.wait_for_timeout(1500)
                if "login" not in (page.url or ""):
                    break
        jwt = page.evaluate("() => localStorage.getItem('joypyroxy_token') || ''") or ""
        out["jwt"] = bool(jwt)
        log(f"jwt={'有' if jwt else '无'} url={page.url[:60]}")
        if not jwt:
            out["claim"] = "没拿到登录态"
            print("JOY_RESULT=" + json.dumps(out, ensure_ascii=False))
            return 2

        page.goto(JB.OVERVIEW, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        JB._dismiss_cookie(page, log)
        page.wait_for_timeout(800)

        ok_c, detail_c = JB._claim(page, log)
        bal = JB._balance(page)
        out["claim"] = detail_c
        out["balance"] = bal
        log(f"CLAIM ok={ok_c} detail={detail_c} balance={bal}")
        out["ok"] = bool(ok_c)

        if bal and bal > 0:
            ok_b, detail_b = JB._buy(page, 1, "residential", "", log)
            log(f"BUY ok={ok_b} {detail_b}")
            out["balance_after"] = JB._balance(page)

        st, d = JB._fetch(page, "/api/token/get")
        tk = str((d or {}).get("token") or "")
        if tk:
            out["extract_url"] = JB.J.extract_url(tk, network_type="residential")
            log(f"extract={out['extract_url'][:110]}")

    print("JOY_RESULT=" + json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out["ok"] else 4


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"JOY_RESULT fatal={type(e).__name__}: {e}", flush=True)
        sys.exit(1)
