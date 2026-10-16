# -*- coding: utf-8 -*-
"""GA 单次 JoyProxy 全流程：注册 → 邮箱激活 → 登录 → 领 $5 → 买 1GB → 取提取链接。

在 GitHub Actions 的 Windows runner 上跑（有真实桌面，Turnstile 才可能过）。
**全流程走 Camoufox**（注册 → 激活 → 登录 → 领 $5 → 买 1GB → 取提取链接），
与本地第一次成功领取的路径完全一致；只有在 Camoufox 领取被判失败时才用真 Edge
兜底重试一次（runner 上自带 Edge，比默认兜底需要的 playwright chromium 更省事）。

输入（环境变量）：
    JOY_MAILBOX   email----password----client_id----refresh_token[----辅助邮箱[----辅助密码]]
    JOY_PROXY     可选出口 http://user:pass@host:port（留空=直连 runner 出口）
    JOY_PASSWORD  可选，JoyProxy 账号密码；不给就现生成
    JOY_BUY_GB    买多少 GB，默认 1
    JOY_NETWORK   套餐类型，默认 residential
输出：
    每行日志实时打印；结束打印一行 `GA_RESULT={json}`（workflow 抓这行）
"""
import json
import os
import platform
import sys
import threading
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def log(msg):
    print(str(msg), flush=True)


EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
]


def find_edge():
    for p in EDGE_CANDIDATES:
        if Path(p).is_file():
            return p
    return ""


# ---------------------------------------------------------------- 激活邮件
def build_activate(row):
    """返回 signup 需要的 activate(timeout) -> (链接|None, note)"""
    from webui import postal

    def activate(timeout):
        link, cli = postal.wait_joy_activate_link(row, timeout=int(timeout), log=log, proxy="")
        note = "已取到激活链接" if link else "没等到激活邮件"
        new_rt = None
        try:
            if cli:
                info = cli.account_info() or {}
                new_rt = info.get("refresh_token")
        except Exception as e:
            log(f"[邮箱] 读账户信息失败（{type(e).__name__}: {str(e)[:70]}）")
        if new_rt:
            row["refresh_token"] = new_rt          # token 轮换：本轮沿用新值
            log("[邮箱] refresh_token 已轮换，本轮沿用新值")
        return link, note

    return activate


# ------------------------------------------------------- 领赠金：真 Edge 兜底
def edge_claim(jwt, log=print, proxy=""):
    """signup 的 store['claim_chromium'] 钩子：签名 (jwt, log=) -> (ok, detail, balance)。
    同一线程里先跑过 Camoufox 再起 patchright 会报 asyncio loop 错，所以换线程起。
    proxy：给 Edge 挂同一出口（GA runner 直连是机房 IP，欢迎页盾解不出）。"""
    out = {}

    def run():
        try:
            out["r"] = _edge_claim_inner(jwt, log, proxy)
        except Exception as e:
            out["e"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=480)
    if "e" in out:
        raise out["e"]
    if "r" not in out:
        return False, "Edge 兜底超时（480s）", None
    return out["r"]


def _edge_claim_inner(jwt, log=print, proxy=""):
    import urllib.parse as up
    from patchright.sync_api import sync_playwright
    from webui import joy_browser as JB

    exe = find_edge()
    if not exe:
        return False, "runner 上没有找到 msedge.exe", None
    ctx_kw = {"locale": "en-US", "viewport": {"width": 1400, "height": 920}}
    if proxy:
        u = up.urlparse(proxy if "://" in proxy else "http://" + proxy)
        p = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 8080}"}
        if u.username:
            p["username"] = up.unquote(u.username)
            p["password"] = up.unquote(u.password or "")
        ctx_kw["proxy"] = p
        # 时区跟出口对齐（对齐原理同 Camoufox 的 geoip=True）
        try:
            import requests as _rq
            s = _rq.Session(); s.trust_env = False; s.verify = False
            s.proxies = {"http": proxy, "https": proxy}
            info = s.get("http://ip-api.com/json?fields=timezone,countryCode", timeout=25).json()
            if info.get("timezone"):
                ctx_kw["timezone_id"] = info["timezone"]
                log(f"[Edge] 出口 {u.hostname} → {info.get('countryCode')} {info['timezone']}")
        except Exception as e:
            log(f"[Edge] 查出口时区失败（{type(e).__name__}），用默认时区")
    log(f"[Edge] 启动真 Edge（普通窗口、{'代理 ' + (proxy.split('@')[-1] if proxy else '直连')}）：{exe}")
    driver = sync_playwright().start()
    browser = None
    try:
        browser = driver.chromium.launch(headless=False, executable_path=exe)
        ctx = browser.new_context(**ctx_kw)
        page = ctx.new_page()
        page.goto(JB.BASE + "/login.html", wait_until="domcontentloaded", timeout=90000)
        page.evaluate("(t) => localStorage.setItem('joypyroxy_token', t)", jwt)
        page.goto(JB.OVERVIEW, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3500)
        JB._dismiss_cookie(page, log)
        if "login" in (page.url or ""):
            return False, "注入登录态后仍被要求登录（JWT 失效）", None
        log("[Edge] 已进入控制台，开始领赠金…")
        ok, detail = JB._claim(page, log)
        bal = JB._balance(page)
        log(f"[Edge] 领取结果 ok={ok} {detail} balance={bal}")
        return ok, detail, bal
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        driver.stop()


def pick_exit_from_extract(url, log=print):
    """从提取链接取一个出口。count 改成 1：每次现提取 = 新 sessid = 新出口 IP。
    同时通过该出口查一次公网 IP，方便从日志判断出口质量。取不到返回空串。"""
    import re
    import requests

    url = str(url or "").strip()
    if not url:
        return ""
    url = re.sub(r"duration=[^&]*", "duration=30m", url)
    url = re.sub(r"count=[^&]*", "count=1", url)
    if "count=" not in url:
        url += "&count=1"
    session = requests.Session()
    session.trust_env = False
    session.verify = False
    try:
        resp = session.get(url, timeout=60)
        lines = [x.strip() for x in re.split(r"[\r\n]+", resp.text or "") if x.strip()]
    except Exception as exc:
        log(f"取出口失败：{type(exc).__name__}: {str(exc)[:100]}")
        return ""
    if not lines:
        log("取出口失败：提取链接返回空")
        return ""
    proxy = lines[0]
    if not proxy.startswith("http"):
        proxy = "http://" + proxy
    sessid = ""
    m = re.search(r"sessid-([A-Za-z0-9]+)", proxy)
    if m:
        sessid = m.group(1)
    egress = ""
    try:
        probe = requests.Session()
        probe.trust_env = False
        probe.verify = False
        probe.proxies = {"http": proxy, "https": proxy}
        r = probe.get("http://ip-api.com/json?fields=query", timeout=30)
        try:
            egress = str((r.json() or {}).get("query") or "").strip()[:40]
        except Exception:
            egress = ""
    except Exception:
        egress = ""
    import re as _re
    if not _re.fullmatch(r"[0-9.]+", egress or ""):
        # 网关回了 402 之类（Payment Required=流量耗尽）——继续跑只会全程失败
        log(f"出口不可用（网关响应：{egress or '无响应'}）——大概率流量已耗尽")
        return ""
    log(f"出口已选：{proxy.split('@')[-1]} sessid={sessid or '?'} 出口IP={egress}")
    return proxy


# ------------------------------------------------------- 协议登录（登录盾的替代路径）
def protocol_login(email, password, proxy, log=print):
    """登录页交互盾过不去时：在注册页（同域）解出 turnstile token，
    直接调 /api/auth/login 拿 JWT。拿不到返回空串。"""
    import time as _t
    from camoufox.sync_api import Camoufox
    from webui import joyproxy as J
    from webui.nc_browser import proxy_to_camoufox

    kw = dict(headless=False, humanize=True, locale="en-US", window=(1400, 920))
    pa = proxy_to_camoufox(proxy)
    if pa:
        kw["proxy"] = pa
        kw["geoip"] = True
    token_js = ("() => {const e=document.querySelector('input[name=cf-turnstile-response],"
                "textarea[name=cf-turnstile-response]');return e&&e.value?e.value:''}")
    tok = ""
    br = None
    try:
        br = Camoufox(**kw).__enter__()
        ctx = br.contexts[0] if getattr(br, "contexts", None) else br
        page = ctx.new_page() if hasattr(ctx, "new_page") else br.pages[0]
        page.goto("https://www.joyproxy.com/register.html",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        deadline = _t.time() + 75
        while _t.time() < deadline:
            tok = page.evaluate(token_js) or ""
            if tok:
                break
            page.wait_for_timeout(2000)
        log(f"[协议登录] 注册页 token：{'已拿到' if tok else '没拿到（该出口盾没过）'}")
    except Exception as e:
        log(f"[协议登录] 取 token 失败：{type(e).__name__}: {str(e)[:110]}")
    finally:
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
    if not tok:
        return ""

    try:
        status, body = J._req(
            "POST", J.BASE + "/api/auth/login",
            payload={"username": email, "password": password, "turnstile_token": tok},
            proxy=proxy or None, timeout=40,
        )
    except Exception as e:
        log(f"[协议登录] 请求失败：{type(e).__name__}: {str(e)[:110]}")
        return ""
    jwt = str((body or {}).get("token") or "")
    if not jwt:
        log(f"[协议登录] 被拒：HTTP {status} {json.dumps(body, ensure_ascii=False)[:140]}")
        return ""
    log("[协议登录] 登录成功，拿到 JWT")
    return jwt


# ---------------------------------------------------------------------- 入口
def main():
    raw = (os.environ.get("JOY_MAILBOX") or "").strip()
    if not raw:
        log("✘ 没给 JOY_MAILBOX（email----password----client_id----refresh_token）")
        print("GA_RESULT=" + json.dumps({"ok": False, "stage": "config", "detail": "JOY_MAILBOX 缺失"}))
        return 1

    from webui import outlook, joy_browser

    try:
        parsed = outlook.parse_line(raw)
    except Exception as e:
        log(f"✘ 邮箱行解析失败：{e}")
        print("GA_RESULT=" + json.dumps({"ok": False, "stage": "config", "detail": str(e)}))
        return 1

    row = {
        "id": None,
        "email": parsed["email"],
        "password": parsed.get("password") or "",
        "client_id": parsed["client_id"],
        "refresh_token": parsed["refresh_token"],
        "recovery_email": parsed.get("recovery_email") or "",
        "recovery_password": parsed.get("recovery_password") or "",
        "raw": parsed.get("raw") or raw,
    }
    log(f"运行环境 {platform.platform()}；邮箱 {row['email']}")

    password = (os.environ.get("JOY_PASSWORD") or "").strip()
    if not password:
        # 已注册账号的真密码优先（JOY_PASSWORDS：每行 email----password）。
        # 注册被跳过的轮次不会改密码，别的来源（HMAC/随机）都会造成"两套密码"。
        table = (os.environ.get("JOY_PASSWORDS") or "").strip()
        for line in table.splitlines():
            parts = line.strip().split("----")
            if len(parts) >= 2 and parts[0].lower() == row["email"].lower():
                password = parts[1]
                log("使用 JOY_PASSWORDS 里该账号的既定密码")
                break
    if not password:
        # 确定性密码：同一邮箱在任何一轮都生成同一个值（HMAC-邮箱-密钥），
        # 适用于"本轮新注册"的邮箱——下轮重跑同邮箱也一致。
        secret = (os.environ.get("JOY_PW_SECRET") or "").strip()
        if secret:
            import hashlib
            import hmac as _hmac
            digest = _hmac.new(secret.encode(), row["email"].lower().encode(),
                               hashlib.sha256).hexdigest()[:10]
            password = "Jp!" + digest
            log("账号密码按邮箱确定性生成（同邮箱各轮一致）")
        else:
            import secrets
            password = "Jp!" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")[:10]
            log("⚠ 没设 JOY_PW_SECRET：本轮随机生成密码，重跑同邮箱会登不上（建议在仓库 secrets 里加上）")

    gb = int(os.environ.get("JOY_BUY_GB") or 1)
    net = (os.environ.get("JOY_NETWORK") or "residential").strip()
    extract_url = (os.environ.get("JOY_EXTRACT_URL") or "").strip()

    # 出口轮换：固定出口优先（JOY_PROXY），否则每轮从提取链接现取一个。
    # 换出口重试的原因：提取池里一部分出口的 CF 信任分不够，注册盾解不出 token；
    # 实测同一脚本换一个出口 10 秒就能出 token，所以按出口重试而不是放弃。
    # 每次尝试都放进一次性线程：Playwright 同步 API 在同一线程里重复起
    # 浏览器会报 "Sync API inside the asyncio loop"（worker 同款处理）。
    def _in_thread(fn, *a, **kw):
        out = {}

        def run():
            try:
                out["r"] = fn(*a, **kw)
            except Exception as e:
                out["e"] = e

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join()
        if "e" in out:
            raise out["e"]
        return out.get("r")

    tries = max(1, min(int(os.environ.get("JOY_TRIES") or 4), 8))
    fixed_proxy = (os.environ.get("JOY_PROXY") or "").strip()
    retryable_stages = {"turnstile", "turnstile2", "region", "login", "error"}
    res = None
    used = []
    try:
        for attempt in range(1, tries + 1):
            if fixed_proxy:
                proxy = fixed_proxy
            else:
                proxy = pick_exit_from_extract(extract_url, log=log)   # count=1：每次新 sessid
            used.append(proxy)
            log(f"—— 第 {attempt}/{tries} 次尝试（出口 {proxy.split('@')[-1] if proxy else '直连'}）")
            res = _in_thread(
                joy_browser.signup,
                row["email"], password,
                proxy=proxy or None,
                log=log, activate=build_activate(row),
                claim=True, buy_gb=gb, network_type=net, wait_activate=240,
                store={"claim_chromium": (lambda p: lambda jwt, log=print: edge_claim(jwt, log=log, proxy=p))(proxy)},
            )
            if res.get("ok"):
                break
            stage = str(res.get("stage") or "")
            if stage == "turnstile2" and not res.get("jwt"):
                # 登录页交互盾过不去 → 协议登录兜底：注册页 token 调 /api/auth/login，
                # 拿到 JWT 后直接进 Edge 领取（全新 runner = 全新设备指纹，正是平台信任的方向）
                log("[兜底] 登录盾未过 → 改走协议登录")
                jwt = _in_thread(protocol_login, row["email"], password, proxy or "", log)
                if jwt:
                    ok_c, detail_c, bal_c, extract_c = edge_claim(jwt, log=log, proxy=proxy)
                    res = {
                        "ok": bool(ok_c and (bal_c or 0) > 0),
                        "stage": "done" if ok_c else "claim",
                        "detail": detail_c,
                        "email": row["email"],
                        "jwt": jwt,
                        "balance": bal_c,
                        "extract_url": extract_c or "",
                    }
                    log(f"[兜底] 协议登录+Edge 领取结果：ok={res['ok']} {detail_c}")
                    break
                log("[兜底] 协议登录也没成，继续换出口")
            if stage not in retryable_stages:
                log(f"失败阶段 {stage} 与出口无关，不再换出口")
                break
            if attempt >= tries:
                break
            log(f"该出口没通过（{stage}），换出口重试…")
    except Exception:
        traceback.print_exc()
        print("GA_RESULT=" + json.dumps({"ok": False, "stage": "exception",
                                         "detail": traceback.format_exc()[-400:]}))
        return 1

    safe = {
        "ok": bool(res.get("ok")),
        "stage": res.get("stage"),
        "detail": res.get("detail"),
        "email": res.get("email"),
        "password": password,
        "balance": res.get("balance"),
        "extract_url": res.get("extract_url"),
        "jwt_present": bool(res.get("jwt")),
        "credit_failed": bool(res.get("credit_failed")),
        "buy_failed": bool(res.get("buy_failed")),
        "exits_tried": len(used),
        "steps": res.get("steps"),
    }
    print("GA_RESULT=" + json.dumps(safe, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
