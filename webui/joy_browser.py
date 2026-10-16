# -*- coding: utf-8 -*-
"""JoyProxy 控制台浏览器自动化（Camoufox + Xvfb）：
登录（joyproxy 的登录必须过 Turnstile，纯 requests 会被 captcha_failed 拦）→ 领 $5 赠金 →
余额买住宅 1GB → 取 API 提取链接。

数据请求都放在页面里 fetch（同源 + localStorage 里的 Bearer JWT），和控制台前端行为一致。
返回 {ok, stage, detail, steps[], jwt, extract_url, balance}
stage: import / display / launch / login / claim / buy / token / done
"""
import json
import threading

from . import joyproxy as J
from .nc_browser import display_ready, proxy_to_camoufox, log_ok, installed

BASE = "https://www.joyproxy.com"
OVERVIEW = BASE + "/admin-overview.html"

PAGE_FETCH = """async (arg) => {
  const t = localStorage.getItem('joypyroxy_token') || '';
  const h = {Authorization: 'Bearer ' + t};
  if (arg.body !== null && arg.body !== undefined) h['Content-Type'] = 'application/json';
  const r = await fetch(arg.path, {method: arg.method || 'GET', headers: h,
    body: (arg.body === null || arg.body === undefined) ? undefined : JSON.stringify(arg.body),
    cache: 'no-store'});
  return {status: r.status, text: await r.text()};
}"""


def _fetch(page, path, method="GET", body=None):
    res = page.evaluate(PAGE_FETCH, {"path": path, "method": method, "body": body})
    try:
        d = json.loads(res["text"])
    except Exception:
        d = {"raw": (res["text"] or "")[:200]}
    return res["status"], d


def _balance(page):
    st, pf = _fetch(page, "/api/user/profile")
    try:
        return float(pf.get("balance") or 0)
    except Exception:
        return None


def _safe_click(page, selector, log=None, timeout=12000, what=""):
    """点元素：常规点击被浮层挡住/不稳时，退化为 DOM 点击（实测控制台有浮层会挡「Claim now」）"""
    try:
        page.click(selector, timeout=timeout)
        return True
    except Exception as e:
        if log:
            log(f"[浏览器] 常规点击 {what or selector} 失败（{type(e).__name__}），改用 DOM 点击")
        try:
            okj = page.evaluate("(sel) => { const el = document.querySelector(sel);"
                                " if (!el) return false; el.click(); return true; }", selector)
            page.wait_for_timeout(600)
            return bool(okj)
        except Exception as e2:
            if log:
                log(f"[浏览器] DOM 点击也失败：{type(e2).__name__}: {str(e2)[:80]}")
            return False


def _dismiss_cookie(page, log=None):
    """关掉 cookie 同意横幅：它会盖住页面底部的 Turnstile 挑战框（实测点不到）"""
    for txt in ("Essential Only", "Accept All", "接受全部", "全部接受", "同意", "接受"):
        try:
            el = page.get_by_role("button", name=txt)
            if el.count():
                el.first.click(timeout=3000)
                page.wait_for_timeout(1200)
                if log:
                    log(f"[浏览器] 已关掉 cookie 横幅（{txt}）")
                return True
        except Exception:
            continue
    return False


def _ts_token(page, kind):
    """读 joyproxy 页面上的 Turnstile token。
    注册页/登录页各有专用取值函数（pickRegisterToken / pickLoginToken），领取走 getToken('welcomeClaim')"""
    try:
        return page.evaluate(
            """(k) => {
                 const jt = window.joyTurnstile;
                 if (!jt) return '';
                 if (k === 'register' && typeof jt.pickRegisterToken === 'function')
                   return jt.pickRegisterToken() || '';
                 if (k === 'login' && typeof jt.pickLoginToken === 'function')
                   return jt.pickLoginToken() || '';
                 if (typeof jt.getToken === 'function') return jt.getToken(k) || '';
                 return '';
               }""", kind) or ""
    except Exception:
        return ""


def _click_ts_checkbox(page):
    """交互式挑战：在 cloudflare 挑战框里点一下复选框（数据中心 IP 常见）"""
    for f in page.frames:
        if "challenges.cloudflare" in (f.url or ""):
            try:
                cb = f.locator("input[type=checkbox]")
                if cb.count():
                    cb.first.click(timeout=3000)
                    return True
            except Exception:
                pass
            try:
                b = f.locator("body").bounding_box()
                if not b:
                    continue
                x, y = b["x"] + 22, b["y"] + b["height"] / 2
                page.mouse.move(x - 60, y - 30)
                page.wait_for_timeout(180)
                page.mouse.move(x, y, steps=14)
                page.mouse.click(x, y)
                return True
            except Exception:
                pass
    return False


def _ts_failed(page):
    """挑战框出现 "Verification Failed / Troubleshoot" → 该出口被 CF 拒，早点换出口"""
    for f in page.frames:
        if "challenges.cloudflare" in (f.url or ""):
            try:
                t = (f.locator("body").inner_text(timeout=1500) or "").lower()
                if "verification failed" in t or "troubleshoot" in t or "验证失败" in t:
                    return True
            except Exception:
                pass
    return False


def _solve_turnstile(page, kind, log, seconds=75):
    """等 Turnstile 过：先等自动解，再点复选框，最后再等一轮。返回 token（可能为空）
    期间若挑战框明确报「Verification Failed」→ 立即返回（调用方换出口重试）"""
    waited = 0
    while waited < 18:                                  # 自动挑战一般几秒
        tok = _ts_token(page, kind)
        if tok:
            log(f"[浏览器] Turnstile 已通过（约 {waited}s）")
            return tok
        if _ts_failed(page):
            log("[浏览器] 挑战框明确报 Verification Failed → 这个出口被 CF 拒，换出口再来")
            return ""
        page.wait_for_timeout(2000)
        waited += 2
    log("[浏览器] 自动挑战没出结果，尝试点击 Turnstile 复选框…")
    if _click_ts_checkbox(page):
        while waited < seconds:
            tok = _ts_token(page, kind)
            if tok:
                log(f"[浏览器] 点击后 Turnstile 通过（约 {waited}s）")
                return tok
            if _ts_failed(page):
                log("[浏览器] 挑战框报 Verification Failed → 换出口")
                return ""
            page.wait_for_timeout(2000)
            waited += 2
    log("[浏览器] Turnstile 仍未通过（可能是数据中心 IP 被 Cloudflare 拦）")
    return ""


def _login_if_needed(page, email, password, log):
    """控制台未登录会跳到 login.html；这里填表登录（含 Turnstile 自动过）"""
    page.goto(OVERVIEW, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(2500)
    _dismiss_cookie(page, log)
    if "login" not in (page.url or ""):
        log("[浏览器] 已有会话，跳过登录")
        return True, "已有会话"
    log("[浏览器] 在登录页，填表登录（Turnstile 由真浏览器过）…")
    page.fill("#email", email)
    page.fill("#password", password)
    page.wait_for_timeout(800)
    tok = _solve_turnstile(page, "login", log)
    if not tok:
        return False, "登录页 Turnstile 没过（数据中心 IP 常被拦；建议换个出口或稍后再试）"
    page.click("#loginBtn")
    for _ in range(40):                                    # 最多 ~60s
        page.wait_for_timeout(1500)
        if "login" not in (page.url or ""):
            page.wait_for_timeout(1500)
            return True, "登录成功"
        try:
            alerts = page.evaluate("() => [...document.querySelectorAll('[class*=alert],[class*=error],"
                                   "[id*=Error]')].map(e => e.textContent.trim()).filter(Boolean).slice(0, 2)")
        except Exception:
            alerts = []
        if alerts and any("验证" in a or "verif" in a.lower() for a in alerts):
            log(f"[浏览器] 页面提示：{alerts[0][:80]}，重试一次 Turnstile…")
            tok2 = _solve_turnstile(page, "login", log, seconds=45)
            if tok2:
                page.click("#loginBtn")
    alerts = []
    try:
        alerts = page.evaluate("() => [...document.querySelectorAll('[class*=alert],[class*=error],"
                               "[id*=Error]')].map(e => e.textContent.trim()).filter(Boolean).slice(0, 2)")
    except Exception:
        pass
    return False, f"登录未跳转（{('；'.join(alerts))[:100] or '可能卡在人机校验'})"


def _claim(page, log):
    """点「Claim now」领 $5（只在 status=eligible 时卡片才渲染）"""
    _dismiss_cookie(page, log)
    st, wc = _fetch(page, "/api/user/welcome-credit")
    if st != 200:
        return False, f"读赠金状态失败：HTTP {st}"
    reason = J.wc_reason(wc)
    if reason:
        return False, reason
    try:
        page.wait_for_selector("#ovWelcomeClaimBtn", timeout=8000)
    except Exception:
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
    try:
        page.wait_for_selector("#ovWelcomeClaimBtn", timeout=15000)
    except Exception:
        return False, "页面上没有「Claim now」按钮（可能刚被领过或条件不满足）"
    log("[浏览器] 点击「Claim now」，等条款弹窗…")
    if not _safe_click(page, "#ovWelcomeClaimBtn", log, what="Claim now"):
        return False, "页面上点不到「Claim now」（可能被浮层挡住）"
    page.wait_for_selector("#welcomeTermsModal", state="visible", timeout=15000)
    page.wait_for_timeout(1500)
    log("[浏览器] 条款弹窗已出：Turnstile 开始解（180s 内可人工在窗口里点复选框，脚本会自动接住 token）…")
    tok = _solve_turnstile(page, "welcomeClaim", log, seconds=180)
    if not tok:
        # 站方包装（joyTurnstile.getToken）不吐时的两条兜底：隐藏 input / CF 官方 API
        for js in (
            "() => { const e = document.querySelector('#welcomeTermsModal input[name=cf-turnstile-response],"
            "#welcomeTermsModal textarea[name=cf-turnstile-response]'); return e && e.value ? e.value : ''; }",
            "() => { try { return (window.turnstile && window.turnstile.getResponse"
            " && window.turnstile.getResponse()) || ''; } catch (e) { return ''; } }",
        ):
            try:
                tok = page.evaluate(js) or ""
            except Exception:
                tok = ""
            if tok:
                log("[浏览器] 从隐藏 input / turnstile API 拿到了 token")
                break
    if not tok:
        # 空 token 点 agree 只会换来 "Please complete the security check."，不烧这次尝试
        log("[浏览器] 仍没拿到 Turnstile token——不提交，直接返回（可重跑再试）")
        return False, "Turnstile 没过（挑战未解/没人点复选框），未提交领取"
    if not _safe_click(page, "#welcomeTermsAgree", log, what="同意并领取"):
        return False, "点不到「agree and claim」按钮"
    for _ in range(16):
        page.wait_for_timeout(1500)
        try:
            err = (page.text_content("#welcomeTermsErr") or "").strip()
        except Exception:
            err = ""
        if err:
            return False, f"领取被拒：{err[:160]}"
        bal = _balance(page)
        if bal and bal > 0:
            return True, f"赠金到账，余额 ${bal:.2f}"
        st2, wc2 = _fetch(page, "/api/user/welcome-credit")
        s = str((wc2 or {}).get("status") or "")
        if s and s != "eligible" and s != "available":
            return False, J.wc_reason(wc2) or f"状态 {s}"
    return False, "领取结果未知（超时）"


def _buy(page, gb, network_type, product_id, log):
    """余额支付买短效套餐（默认住宅 1GB）"""
    bal = _balance(page)
    st, pr = _fetch(page, "/api/pricing?_=%d" % int(__import__("time").time() * 1000))
    plan = J._price_of(pr, network_type)
    if not plan:
        return False, f"价目表里没有 {J.NET_LABEL.get(network_type, network_type)} 套餐"
    pid, name, price = str(plan.get("id")), str(plan.get("name")), float(plan.get("price") or 0)
    if product_id:
        pid = str(product_id)
    if bal is None:
        return False, "读余额失败"
    if bal + 1e-9 < price:
        return False, f"余额不足：{name} 需 ${price:.2f}，当前 ${bal:.2f}（$5 赠金没到账就买不了）"
    body = {"product_id": pid, "product_name": name, "type": "short-term", "quantity": 1,
            "payment_method": "balance", "network_type": network_type or "residential",
            "total_price": price}
    log(f"[浏览器] 下单 {name}({pid}) ${price:.2f}，余额支付…")
    st2, d = _fetch(page, "/api/orders/create", "POST", body)
    if st2 >= 400 or not (d or {}).get("success"):
        msg = (d or {}).get("error") or (d or {}).get("message") or f"HTTP {st2}"
        return False, f"下单失败：{msg}"[:200]
    oid = (d.get("order") or {}).get("id")
    bal2 = _balance(page)
    return True, f"订单 {oid} 已支付 ${price:.2f}" + (f"，余额 ${bal2:.2f}" if bal2 is not None else "")


def provision(email, password, gb=1, network_type="residential", product_id="",
              do_claim=True, do_buy=True, proxy=None, log=print, attempt=1):
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:300]})
        log(("✔ " if ok else "✘ ") + name + ("：" + str(detail)[:200] if detail else ""))
        return ok

    out = {"ok": False, "stage": "init", "detail": "", "steps": steps,
           "jwt": "", "extract_url": "", "balance": None}
    if not installed():
        out.update({"stage": "import", "detail": "未安装 camoufox（点「安装浏览器依赖」）"})
        return out
    ok_disp, info = display_ready(force=(attempt > 1))
    if not ok_disp:
        out.update({"stage": "display", "detail": info})
        return out
    try:
        from camoufox.sync_api import Camoufox
    except Exception as e:
        out.update({"stage": "import", "detail": f"{type(e).__name__}: {e}"})
        return out
    kw = dict(headless=False, humanize=True, locale="zh-CN", window=(1400, 920))
    pa = proxy_to_camoufox(proxy)
    if pa:
        kw["proxy"] = pa
        kw["geoip"] = True          # 同 signup：挂代理必须同步出口时区，否则 CF 盟解不出 token
    log(f"[浏览器] 启动 Camoufox（{('代理 ' + pa['server']) if pa else '直连'}，显示 {info}）…")
    br = None
    try:
        ctxm = Camoufox(**kw)
        br = ctxm.__enter__()
        ctx = getattr(br, "contexts", None)
        ctx = ctx[0] if ctx else br
        page = ctx.new_page() if hasattr(ctx, "new_page") else br.pages[0]

        ok, detail = _login_if_needed(page, email, password, log)
        if not step("登录 JoyProxy", ok, detail):
            out.update({"stage": "login", "detail": detail})
            return out
        jwt = page.evaluate("() => localStorage.getItem('joypyroxy_token') || ''")
        out["jwt"] = jwt or ""
        bal = _balance(page)
        out["balance"] = bal
        step("读账户", bal is not None, f"余额 ${bal:.2f}" if bal is not None else "读余额失败")

        if do_claim:
            ok, detail = _claim(page, log)
            step("$5 赠金", ok, detail)
        else:
            step("$5 赠金", True, "本次跳过（未开启自动领取）")
        out["balance"] = _balance(page)

        if do_buy:
            ok, detail = _buy(page, gb, network_type, product_id, log)
            step("购买套餐", ok, detail)
            out["balance"] = _balance(page)
        else:
            step("购买套餐", True, "本次跳过（未开启自动购买）")

        st, d = _fetch(page, "/api/token/get")
        tok = str((d or {}).get("token") or "")
        if st != 200 or not tok:
            step("获取提取链接", False, f"取 token 失败：HTTP {st}")
            out.update({"stage": "token", "detail": "取提取 token 失败"})
            return out
        out["extract_url"] = J.extract_url(tok, network_type=network_type)
        step("获取提取链接", True, out["extract_url"][:110] + "…")
        out["ok"] = all(s["ok"] for s in steps)
        out["stage"] = "done"
        out["detail"] = "完成" if out["ok"] else "部分步骤未完成（见上）"
        return out
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        out.update({"stage": "error", "detail": msg})
        step("浏览器流程", False, msg)
        if "Failed to launch" in msg and attempt == 1:      # 陈旧 X 显示 → 清一次重试
            try:
                br = None
            except Exception:
                pass
            try:
                return provision(email, password, gb=gb, network_type=network_type,
                                 product_id=product_id, do_claim=do_claim, do_buy=do_buy,
                                 proxy=proxy, log=log, attempt=2)
            except Exception:
                pass
        return out
    finally:
        if br is not None:
            try:
                br.close()
            except Exception:
                pass

REGISTER = BASE + "/register.html"


def _region_ok(page, log):
    """注册前的地区检查（官网会在不支持的地区直接跳 /auth-region-blocked.html）"""
    st, d = _fetch(page, "/api/public/auth-region-check")
    blocked = bool((d or {}).get("blocked_signin"))
    if blocked:
        log(f"[浏览器] 这个出口的地区被限制注册/登录（{json.dumps(d, ensure_ascii=False)[:120]}）")
    return not blocked


def signup(email, password, proxy=None, log=print, activate=None, claim=True, buy_gb=1,
           network_type="residential", wait_activate=240, attempt=1, store=None):
    """全自动注册 JoyProxy：
    ①注册（CF 盾 #1）→ ②邮箱激活（回调取链接）→ ③登录（CF 盾 #2）→ ④领 $5（CF 盾 #3）
    → ⑤余额买住宅 1GB → ⑥取提取链接。
    activate: callable(timeout) -> (链接|None, note)  由 worker 用微软邮箱池取激活链接。
    返回 {ok, stage, detail, steps[], email, password, jwt, extract_url, balance}"""
    steps = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:300]})
        log(("✔ " if ok else "✘ ") + name + ("：" + str(detail)[:200] if detail else ""))
        return ok

    out = {"ok": False, "stage": "init", "detail": "", "steps": steps, "email": email,
           "password": password, "jwt": "", "extract_url": "", "balance": None}
    if not installed():
        out.update({"stage": "import", "detail": "未安装 camoufox（点「安装浏览器依赖」）"})
        return out
    ok_disp, info = display_ready(force=(attempt > 1))
    if not ok_disp:
        out.update({"stage": "display", "detail": info})
        return out
    try:
        from camoufox.sync_api import Camoufox
    except Exception as e:
        out.update({"stage": "import", "detail": f"{type(e).__name__}: {e}"})
        return out
    kw = dict(headless=False, humanize=True, locale="en-US", window=(1400, 920))
    pa = proxy_to_camoufox(proxy)
    if pa:
        kw["proxy"] = pa
        kw["geoip"] = True          # 按出口 IP 同步时区/地区；不同步时 CF 盾判环境不一致（GA 实测：12 个出口全灭，geoip 探针 10s 出 token）
    log(f"[浏览器] 启动 Camoufox（{('代理 ' + pa['server']) if pa else '直连（数据中心 IP 大概率过不了盾）'}，显示 {info}）…")
    br = None
    try:
        ctxm = Camoufox(**kw)
        br = ctxm.__enter__()
        ctx = getattr(br, "contexts", None)
        ctx = ctx[0] if ctx else br
        page = ctx.new_page() if hasattr(ctx, "new_page") else br.pages[0]

        # ① 注册
        page.goto(REGISTER, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(3000)
        _dismiss_cookie(page, log)
        for _ in range(10):                          # 等 joyTurnstile 初始化（没有它读不到 token）
            if page.evaluate("() => !!window.joyTurnstile"):
                break
            page.wait_for_timeout(1500)
        page.wait_for_timeout(1000)
        if "region-blocked" in (page.url or ""):
            out.update({"stage": "region", "detail": "该出口地区被 JoyProxy 限制注册（换一个出口再试）"})
            step("注册", False, "地区被限制（换出口）")
            return out
        if not _region_ok(page, log):
            out.update({"stage": "region", "detail": "该出口地区被限制（换一个出口再试）"})
            step("注册", False, "地区被限制（换出口）")
            return out
        page.fill("#email", email)
        page.fill("#password", password)
        page.wait_for_timeout(600)
        tok = _solve_turnstile(page, "register", log)
        if not tok:
            # 注册页的 token 走 pickRegisterToken；兜底从隐藏 input 读
            tok = page.evaluate("() => { const e = document.querySelector('input[name=cf-turnstile-response],"
                                "textarea[name=cf-turnstile-response]'); return e && e.value ? e.value : ''; }")
        if not tok:
            out.update({"stage": "turnstile", "detail": "注册页 Turnstile 没过（换个出口或稍后再试）"})
            step("注册", False, out["detail"])
            return out
        page.click("#registerBtn")
        body = ""
        for _ in range(30):
            page.wait_for_timeout(1500)
            if "region-blocked" in (page.url or ""):
                out.update({"stage": "region", "detail": "提交后地区被拦（换出口）"})
                step("注册", False, "地区被限制（换出口）")
                return out
            if "login" in (page.url or ""):
                break
            try:
                body = (page.inner_text("#alertBox") or "").strip()
            except Exception:
                body = ""
            if body and ("exist" in body.lower() or "已注册" in body or "registered" in body.lower()):
                log(f"[浏览器] 该邮箱已注册过（{body[:80]}）→ 跳过注册，直接走激活/登录")
                step("注册", True, "邮箱已注册（跳过注册，直接激活/登录）")
                break
        if "login" not in (page.url or "") and not (body and ("exist" in body.lower() or "registered" in body.lower())):
            low = (body or "").lower()
            if "turnstile" in low or "verif" in low:
                step("注册", False, f"注册被拒（{body[:100]}）")
                out.update({"stage": "turnstile", "detail": body[:160]})
                return out
            out.update({"stage": "register", "detail": f"注册未成功跳转：{body[:160] or page.url}"})
            step("注册", False, out["detail"])
            return out
        step("注册", True, "注册成功，等激活邮件")

        # ② 邮箱激活
        if callable(activate):
            link, anote = activate(wait_activate)
            if not link:
                step("邮箱激活", False, f"没取到激活链接：{anote or '超时'}")
                out.update({"stage": "activate", "detail": "没等到激活邮件"})
                return out
            try:
                page.goto(link, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                if "admin" in (page.url or ""):          # 激活后会直接落到控制台
                    try:
                        page.wait_for_selector(".admin-sidebar, #ovBalanceWelcomeExpiry, #ovWelcomeCol",
                                               timeout=8000)
                    except Exception:
                        pass
                txt = (page.inner_text("body") or "")[:160]
                low = txt.lower()
                out["jwt"] = page.evaluate("() => localStorage.getItem('joypyroxy_token') || ''") or ""
                ok_act = bool(out["jwt"]) or "admin" in (page.url or "") \
                    or any(k in low for k in ("activat", "verified", "success", "已激活", "成功"))
                step("邮箱激活", ok_act, f"{page.url[:70]} ｜ {txt[:80]}")
                if out["jwt"]:
                    log("[浏览器] 激活后已自动登录控制台（免第二次盾）")
                elif not ok_act:
                    log("[浏览器] 激活页没看到明确成功字样，继续尝试登录")
            except Exception as e:
                step("邮箱激活", False, f"打开激活链接失败：{type(e).__name__}: {str(e)[:100]}")
        else:
            step("邮箱激活", True, "跳过（没提供取信回调）")

        # ③ 登录（CF 盾 #2）——激活后已自动登录就跳过
        if not out.get("jwt"):
            page.goto(BASE + "/login.html", wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2200)
            _dismiss_cookie(page, log)
            for _ in range(10):
                if page.evaluate("() => !!window.joyTurnstile"):
                    break
                page.wait_for_timeout(1500)
            page.fill("#email", email)
            page.fill("#password", password)
            page.wait_for_timeout(500)
            tok2 = _solve_turnstile(page, "login", log)
            if not tok2:
                out.update({"stage": "turnstile2", "detail": "登录页 Turnstile 没过"})
                step("登录", False, out["detail"])
                return out
            page.click("#loginBtn")
            for _ in range(40):
                page.wait_for_timeout(1500)
                if "login" not in (page.url or ""):
                    break
            if "login" in (page.url or ""):
                try:
                    err = (page.inner_text("#alertBox") or "").strip()
                except Exception:
                    err = ""
                step("登录", False, f"登录没跳转（{err[:100] or '可能账号未激活'}）")
                out.update({"stage": "login", "detail": err[:160] or "登录没跳转"})
                return out
            out["jwt"] = page.evaluate("() => localStorage.getItem('joypyroxy_token') || ''") or ""
            step("登录", True, "已登录控制台")
        else:
            step("登录", True, "激活后已自动登录（免第二次盾）")

        # ④ 领 $5 赠金（CF 盾 #3）——确保当前在控制台页
        if "admin" not in (page.url or ""):
            page.goto(OVERVIEW, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
        _dismiss_cookie(page, log)
        page.wait_for_timeout(800)
        if claim:
            ok_c, detail = _claim(page, log)
            bal = _balance(page)
            if not ok_c and (not bal or bal <= 0) and out.get("jwt"):
                # 平台要求真 Chrome + 不走代理：改用 Chromium 直连再领一次
                log(f"[浏览器] 领取未成功（{detail[:80]}）→ 改用 Chrome 直连兜底…")
                try:
                    fn = store.get("claim_chromium") if isinstance(store, dict) else None
                except Exception:
                    fn = None
                ok2, detail2, bal2 = (fn or claim_via_chromium)(out["jwt"], log=log)
                if ok2:
                    ok_c, detail, bal = ok2, detail2 + "（Chrome 直连）", bal2
                else:
                    detail = f"{detail}；Chrome 直连也不行：{detail2}"
            step("$5 赠金", ok_c, detail)
            out["balance"] = bal
            if not ok_c or not bal or bal <= 0:
                out["credit_failed"] = True       # 赠金没到账：不中断流程，继续取提取链接（账号先入库备用）
                log("[浏览器] 赠金没到账（平台设备校验），继续：购买会因余额不足而跳过，但提取链接照样取回来")
        else:
            step("$5 赠金", True, "跳过（未开启）")

        # ⑤ 余额买住宅 1GB
        if buy_gb:
            ok_b, detail = _buy(page, buy_gb, network_type, "", log)
            step("购买套餐", ok_b, detail)
            out["balance"] = _balance(page)
            if not ok_b:
                out["buy_failed"] = True
                log("[浏览器] 购买没成功（多为余额不足），继续取提取链接")
        else:
            step("购买套餐", True, "跳过（未开启）")

        # ⑥ 提取链接
        st, d = _fetch(page, "/api/token/get")
        tk = str((d or {}).get("token") or "")
        if st != 200 or not tk:
            step("获取提取链接", False, f"HTTP {st}")
            out.update({"stage": "token", "detail": "取提取 token 失败"})
            return out
        out["extract_url"] = J.extract_url(tk, network_type=network_type)
        step("获取提取链接", True, out["extract_url"][:110] + "…")
        out["ok"] = all(x["ok"] for x in steps)
        out["stage"] = "done"
        out["detail"] = "完成" if out["ok"] else "部分步骤未完成（见上）"
        return out
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:160]}"
        out.update({"stage": "error", "detail": msg})
        step("浏览器流程", False, msg)
        if "Failed to launch" in msg and attempt == 1:
            try:
                br = None
            except Exception:
                pass
            try:
                return signup(email, password, proxy=proxy, log=log, activate=activate, claim=claim,
                              buy_gb=buy_gb, network_type=network_type, wait_activate=wait_activate,
                              attempt=2, store=store)
            except Exception:
                pass
        return out
    finally:
        if br is not None:
            try:
                br.close()
            except Exception:
                pass

def claim_via_chromium(jwt, log=print, store=None):
    """领赠金兜底（外层）：Chromium 启动必须在新线程里做——
    同一线程里先起过 Camoufox 再起 Chromium 会报 "Playwright Sync API inside the asyncio loop"。"""
    out = {}

    def run():
        try:
            out["r"] = _claim_via_chromium_inner(jwt, log)
        except Exception as e:
            out["e"] = e
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=420)
    if "e" in out:
        raise out["e"]
    if "r" not in out:
        return False, "Chrome 兜底超时（420s）", None
    return out["r"]


def _claim_via_chromium_inner(jwt, log=print):
    """领赠金专用兜底：平台要求「真 Chrome/Edge + 不用代理/VPN」——
    用 Playwright 自带 Chromium 直连（服务器本机出口，不走代理），把已登录的 JWT 注入 localStorage 再领。
    返回 (ok, detail, balance)"""
    if not jwt:
        return False, "没有登录态，先完成注册/激活", None
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return False, f"playwright 不可用：{type(e).__name__}: {e}", None
    ok_disp, info = display_ready()
    if not ok_disp:
        return False, info, None
    br = None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=False, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
            ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 920})
            page = ctx.new_page()
            page.goto(BASE + "/login.html", wait_until="domcontentloaded", timeout=90000)
            page.evaluate("(t) => localStorage.setItem('joypyroxy_token', t)", jwt)
            page.goto(OVERVIEW, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3500)
            _dismiss_cookie(page, log)
            if "login" in (page.url or ""):
                return False, "注入登录态后仍被要求登录（JWT 可能已失效）", None
            log("[Chrome] 已进入控制台（直连），开始领赠金…")
            ok, detail = _claim(page, log)
            bal = _balance(page)
            return ok, detail, bal
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:160]}", None
    finally:
        if br is not None:
            try:
                br.close()
            except Exception:
                pass
