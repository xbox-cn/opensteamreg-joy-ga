# -*- coding: utf-8 -*-
"""探针：Camoufox + 指定出口打开 JoyProxy 注册页，看浏览器真实出口 IP 与盾的解出情况。

用于回答两个问题：
  1. Camoufox 的 proxy 参数到底有没有生效（打印浏览器内看到的公网 IP）；
  2. 注册页 Turnstile 在"多等一会儿 + 点复选框"的情况下能不能解出 token（对照线上真实流程）。
"""
import os
import re
import sys
import urllib.parse as up

import requests

REGISTER = "https://www.joyproxy.com/register.html"

CHECKBOX_CLICK_JS = """() => {
  const f = document.querySelector('iframe[src*="challenges.cloudflare"]');
  if (!f) return false;
  const r = f.getBoundingClientRect();
  return {x: r.left + 24, y: r.top + r.height / 2};
}"""

TOKEN_HIDDEN = ("() => { const e = document.querySelector('input[name=cf-turnstile-response],"
                "textarea[name=cf-turnstile-response]'); return e && e.value ? e.value : ''; }")
TOKEN_API = ("() => { try { return (window.joyTurnstile && window.joyTurnstile.pickRegisterToken"
             " && window.joyTurnstile.pickRegisterToken()) || ''; } catch (e) { return ''; } }")


def log(msg):
    print(msg, flush=True)


def pick_proxy():
    proxy = (os.environ.get("JOY_PROXY") or "").strip()
    if proxy:
        return proxy
    url = (os.environ.get("JOY_EXTRACT_URL") or "").strip()
    if not url:
        return ""
    url = re.sub(r"duration=[^&]*", "duration=30m", url)
    s = requests.Session()
    s.trust_env = False
    s.verify = False
    try:
        r = s.get(url, timeout=60)
        lines = [x.strip() for x in re.split(r"[\r\n]+", r.text or "") if x.strip()]
    except Exception as e:
        log(f"extract failed: {type(e).__name__}: {str(e)[:100]}")
        return ""
    if not lines:
        return ""
    px = lines[0]
    return px if px.startswith("http") else "http://" + px


def to_camoufox(proxy):
    if not proxy:
        return None
    u = up.urlparse(proxy if "://" in proxy else "http://" + proxy)
    if not u.hostname:
        return None
    d = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 8080}"}
    if u.username:
        d["username"] = up.unquote(u.username)
        d["password"] = up.unquote(u.password or "")
    return d


def main():
    proxy = pick_proxy()
    log(f"proxy host: {(proxy.split('@')[-1] if proxy else '直连')}")
    kw = dict(headless=False, humanize=True, locale="en-US", window=(1400, 920))
    pa = to_camoufox(proxy)
    if pa:
        kw["proxy"] = pa
        kw["geoip"] = True          # 按出口 IP 同步时区/语言，否则 CF 盾判环境不一致
        log(f"camoufox proxy dict: server={pa['server']} user_len={len(pa.get('username', ''))} pass_len={len(pa.get('password', ''))}")
    else:
        log("camoufox proxy dict: 无（直连）")

    from camoufox.sync_api import Camoufox

    br = None
    try:
        br = Camoufox(**kw).__enter__()
        ctx = getattr(br, "contexts", None)
        ctx = ctx[0] if ctx else br
        page = ctx.new_page() if hasattr(ctx, "new_page") else br.pages[0]

        page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)
        log("browser egress: " + (page.inner_text("body") or "")[:70])

        page.goto(REGISTER, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3500)
        console = []
        try:
            page.on("console", lambda m: console.append(f"{m.type}:{m.text[:90]}"))
        except Exception:
            pass
        widget_js = ("() => ({widget: !!document.querySelector('[class*=cf-turnstile],div[id*=cf-turnstile],.cf-turnstile'),"
                     "iframe: !!document.querySelector('iframe[src*=\"challenges.cloudflare\"],iframe[title*=\"Cloudflare\"]'),"
                     "turnstileApi: !!window.turnstile, joyTs: !!window.joyTurnstile,"
                     "cfScripts: document.querySelectorAll('script[src*=\"challenges.cloudflare\"],script[src*=\"turnstile\"]').length})")
        for i in range(12):
            hidden = page.evaluate(TOKEN_HIDDEN) or ""
            api = page.evaluate(TOKEN_API) or ""
            st = page.evaluate(widget_js) or {}
            log(f"t={i*10:3d}s token_hidden={bool(hidden)} token_api={bool(api)} "
                f"widget={st.get('widget')} iframe={st.get('iframe')} "
                f"turnstileApi={st.get('turnstileApi')} joyTs={st.get('joyTs')} cfScripts={st.get('cfScripts')}")
            if hidden or api:
                log("turnstile solved")
                break
            if i in (2, 6):
                try:
                    page.screenshot(path=f"probe_reg_{i*10}s.png")
                except Exception:
                    pass
            if i == 4:
                box = page.evaluate(CHECKBOX_CLICK_JS)
                if box:
                    page.mouse.move(box["x"] - 60, box["y"] - 30)
                    page.wait_for_timeout(200)
                    page.mouse.click(box["x"], box["y"])
                    log(f"clicked checkbox at ({box['x']:.0f},{box['y']:.0f})")
                else:
                    log("challenge iframe not found for click")
            page.wait_for_timeout(10000)
        try:
            page.screenshot(path="probe_reg_final.png")
        except Exception:
            pass
    except Exception as e:
        log(f"probe error: {type(e).__name__}: {str(e)[:160]}")
    finally:
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
