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


def _ensure_turnstile(page, log=None, tries=3):
    """确保页面上的 joyTurnstile 助手就绪。
    控制台/登录页的这个脚本是同源懒加载，经出口有时压根加载不出来；
    这种情况下 getToken() 按源码直接返回空串（widgetIds 为空），
    绝不能把它当成「CF 拒绝」——先刷新重试，真加载不出来要单独报。"""
    for attempt in range(1, tries + 1):
        for _ in range(12):                      # 每个尝试最多等 18s
            try:
                if page.evaluate("() => !!(window.joyTurnstile && typeof joyTurnstile.getToken"
                                 " === 'function' && typeof joyTurnstile.render === 'function')"):
                    return True
            except Exception:
                pass
            page.wait_for_timeout(1500)
        # 还没就绪：手动补一份带 cache-buster 的同源脚本（页面自己的文件，内容不改）
        try:
            r = page.evaluate("""async () => {
                if (window.joyTurnstile) return 'exists';
                await new Promise(res => {
                  const s = document.createElement('script');
                  s.src = '/includes/turnstile.js?v=5&_=' + Date.now();
                  s.onload = res; s.onerror = res;
                  document.head.appendChild(s);
                });
                for (let i = 0; i < 20; i++) {
                  if (window.joyTurnstile) return 'injected';
                  await new Promise(r => setTimeout(r, 300));
                }
                return 'missing'; }""")
            if log:
                log(f"[浏览器] 补注入 turnstile.js → {r}")
            if r == "injected":
                return True
        except Exception as e:
            if log:
                log(f"[浏览器] 补注入失败：{type(e).__name__}: {str(e)[:70]}")
        if attempt < tries:
            if log:
                log(f"[浏览器] 页面 Turnstile 助手没加载（脚本经出口加载失败）→ 第 {attempt} 次刷新重试")
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
            except Exception:
                pass
            page.wait_for_timeout(3500)
            _dismiss_cookie(page, log)
    if log:
        log("[浏览器] 页面 Turnstile 助手始终没加载（出口网络问题，不是 CF 拒绝）")
    return False


def _claim_slot_rendered(page):
    """弹窗里的领取槽位是否有真控件（隐藏 input 带 cf-chl-widget id 即已渲染）"""
    try:
        return page.evaluate("() => !!document.querySelector('#welcomeClaimTurnstile input[id^=cf-chl-widget]')")
    except Exception:
        return False


def _render_claim_slot(page, log=None):
    """槽位没渲染就显式调用他们自己的 render（正是页面打开弹窗时该做的事）"""
    try:
        r = page.evaluate("() => { try { const p = joyTurnstile.render('welcomeClaimTurnstile',"
                          " 'welcomeClaim', 'claim'); return (p && p.then) ? 'calling' : 'ok'; }"
                          " catch (e) { return 'ERR ' + (e && e.message); } }")
        page.wait_for_timeout(2500)
        if log:
            log(f"[浏览器] 显式渲染领取控件：{r}（槽位已渲染={_claim_slot_rendered(page)}）")
        return True
    except Exception as e:
        if log:
            log(f"[浏览器] 显式渲染失败：{type(e).__name__}: {str(e)[:80]}")
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
    """点「Claim now」领 $5（只在 status=eligible 时卡片才渲染）
    失败原因严格区分：脚本没加载 / 控件没渲染 / CF 没给 token / 平台拒绝"""
    _dismiss_cookie(page, log)
    helper_ok = _ensure_turnstile(page, log)
    st, wc = _fetch(page, "/api/user/welcome-credit")
    if not helper_ok:
        log("[浏览器] 页面脚本链不可用（不是 CF 拒绝）→ 直接走「自渲染控件 + 直接调领取接口」")
        return _claim_direct_api(page, wc, log=log)
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
    if not _claim_slot_rendered(page):
        _render_claim_slot(page, log)
    tok = _solve_turnstile(page, "welcomeClaim", log)
    if not tok:
        log("[浏览器] 页面链路没拿到 token → 改用「自渲染控件 + 直接调领取接口」")
        ok_d, detail_d = _claim_direct_api(page, wc, log=log)
        if not ok_d:
            return False, detail_d
        return True, detail_d
    if not _safe_click(page, "#welcomeTermsAgree", log, what="同意并领取"):
        return False, "点不到「agree and claim」按钮"
    for _ in range(16):
        page.wait_for_timeout(1500)
        try:
            err = (page.text_content("#welcomeTermsErr") or "").strip()
        except Exception:
            err = ""
        if err:
            low = err.lower()
            tip = ""
            if "verify your device" in low or "proxy" in low or "chrome" in low:
                tip = ("（平台按「设备/浏览器」维度校验赠金资格，条款明确禁止多账号或换 IP/换浏览器重复领取；"
                       "同一台机器重复领奖会被拒。要额度请充值，或在真人浏览器里为本账号领一次）")
            return False, f"领取被拒：{err[:160]}{tip}"
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
    kw = browser_kwargs(proxy)
    pa = kw.get("proxy")
    log(f"[浏览器] 启动 Camoufox（{('代理 ' + pa['server'] + '，时区随出口') if pa else '直连'}，显示 {info}）…")
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


def browser_kwargs(proxy=None, claim_proxy="", **extra):
    """统一浏览器参数：走代理时开 geoip（时区/语言/坐标跟出口一致——不一致会被 CF 判可疑卡在 Verifying），
    并封 WebRTC（避免真实 IP 泄漏）。claim_proxy 优先用于领奖那一段（平台要求非代理连接才能过验证）。"""
    px = claim_proxy or proxy
    kw = dict(headless=False, humanize=True, window=(1400, 920), block_webrtc=True,
              enable_cache=False)   # 关缓存：实测经出口可能把脚本截断缓存住，连刷新都救不回
    pa = proxy_to_camoufox(px)
    if pa:
        kw["proxy"] = pa
        kw["geoip"] = True                       # 时区/语言/坐标随出口（实测：科威特出口 → Asia/Kuwait / ar-KW）
    else:
        kw["locale"] = "en-US"
    kw.update(extra)
    return kw


def signup(email, password, proxy=None, log=print, activate=None, claim=True, buy_gb=1,
           network_type="residential", wait_activate=240, attempt=1, store=None, claim_proxy=""):
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
    kw = browser_kwargs(proxy)
    pa = kw.get("proxy")
    log(f"[浏览器] 启动 Camoufox（{('代理 ' + pa['server'] + '，时区随出口') if pa else '直连（数据中心 IP 大概率过不了盾）'}，显示 {info}）…")
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
        _ensure_turnstile(page, log)
        page.wait_for_timeout(1000)
        if "region-blocked" in (page.url or ""):
            out.update({"stage": "region", "detail": "该出口地区被 JoyProxy 限制注册（换一个出口再试）"})
            step("注册", False, "地区被限制（换出口）")
            return out
        if not _region_ok(page, log):
            out.update({"stage": "region", "detail": "该出口地区被限制（换一个出口再试）"})
            step("注册", False, "地区被限制（换出口）")
            return out
        # ① 注册：先试「自渲染 token + 直接调注册接口」（页面那条脚本链在本环境经常加载不出来）
        api_done = False
        ok_api, detail_api, code_api = register_via_api(page, email, password, log=log)
        if ok_api:
            step("注册", True, detail_api)
            api_done = True
            if code_api == "exists":
                out["stage"] = "register-exists"
        else:
            if code_api == "region":
                out.update({"stage": "region", "detail": detail_api})
                step("注册", False, detail_api)
                return out
            log(f"[浏览器] 接口注册没成功（{detail_api}）→ 退回表单方式")

        if not api_done:
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
            _dismiss_cookie(page, log)
            ok_l, detail_l, jwt_l = login_via_api(page, email, password, log=log)
            if ok_l:
                out["jwt"] = jwt_l or ""
                step("登录", True, detail_l)
            else:
                log(f"[浏览器] 接口登录没成功（{detail_l}）→ 退回表单方式")
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
            if not ok_c and claim_proxy and claim_proxy != proxy:
                log(f"[浏览器] 用「领奖专用出口」重开一次页面再领（{claim_proxy.split('@')[-1]}）…")
                page.goto(OVERVIEW, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(3000)
                _dismiss_cookie(page, log)
                ok_c, detail = _claim(page, log)
            bal = _balance(page)
            if not ok_c and (not bal or bal <= 0) and out.get("jwt"):
                # 平台要求真 Chrome + 不走代理：改用 Chromium 直连再领一次
                log(f"[浏览器] 领取未成功（{detail[:80]}）→ 改用 Chrome 直连兜底…")
                try:
                    fn = store.get("claim_chromium") if isinstance(store, dict) else None
                except Exception:
                    fn = None
                _cfn = fn or (lambda jwt, log=None: claim_via_chromium(jwt, log=log, proxy=claim_proxy))
                ok2, detail2, bal2 = _cfn(out["jwt"], log=log)
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

def claim_via_chromium(jwt, log=print, store=None, proxy=""):
    """领赠金兜底（外层）：Chromium 启动必须在新线程里做——
    同一线程里先起过 Camoufox 再起 Chromium 会报 "Playwright Sync API inside the asyncio loop"。"""
    out = {}

    def run():
        try:
            out["r"] = _claim_via_chromium_inner(jwt, log, proxy=proxy)
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


def _claim_via_chromium_inner(jwt, log=print, proxy=""):
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
            args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            if proxy:
                args.append("--proxy-server=" + proxy.split("@")[-1].replace("http://", "").replace("https://", ""))
            br = p.chromium.launch(headless=False, args=args)
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

CLAIM_DIRECT_JS = """async (arg) => {
  // 完全绕开页面那条坏掉的脚本链：自己把 CF 的 turnstile 全局跑起来（fetch+eval，CORS=* 可读）
  let el = document.getElementById('zClaimSlot');
  if (!el) {
    el = document.createElement('div');
    el.id = 'zClaimSlot';
    el.style.cssText = 'min-height:70px;display:flex;align-items:center;justify-content:center';
    const host = document.getElementById('welcomeClaimTurnstile') || document.body;
    host.appendChild(el);
  }
  if (typeof window.turnstile === 'undefined') {
    // CF 的脚本要求能找到「它自己的 script 标签」（否则报 Could not find Turnstile valid script tag）：
    // 先把标签插成最后一个 script，再 eval 源码——标签存在 + 代码执行，两头都满足
    const TS_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    let tag = document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]');
    if (!tag) {
      tag = document.createElement('script');
      tag.src = TS_SRC;
      tag.async = true;
      document.head.appendChild(tag);
      await new Promise(r => setTimeout(r, 1200));
    } else {
      document.head.appendChild(tag);      // 挪到最后，保证"最后一个 script"就是它
      await new Promise(r => setTimeout(r, 300));
    }
    if (typeof window.turnstile === 'undefined') {
      try {
        const t = await (await fetch(TS_SRC, {cache: 'no-store'})).text();
        (0, eval)(t);
      } catch (e) { return {err: 'api.js: ' + (e && e.message)}; }
    }
    for (let i = 0; i < 40 && typeof window.turnstile === 'undefined'; i++) {
      await new Promise(r => setTimeout(r, 250));
    }
  }
  if (typeof window.turnstile === 'undefined') return {err: 'turnstile 全局仍不可用'};
  try {
    if (window.__zClaimWidget != null) { try { turnstile.remove(window.__zClaimWidget); } catch (e) {} }
    window.__zClaimWidget = turnstile.render(el, {sitekey: arg.sitekey, theme: 'light', size: 'flexible'});
  } catch (e) { return {err: 'render: ' + (e && e.message)}; }
  return {widget: window.__zClaimWidget, type: typeof window.turnstile};
}"""

CLAIM_POLL_JS = """() => { try {
  if (window.__zClaimWidget == null || typeof window.turnstile === 'undefined') return '';
  return window.turnstile.getResponse(window.__zClaimWidget) || '';
} catch (e) { return ''; } }"""


def _claim_direct_api(page, wc, log=None):
    """不依赖页面脚本链的领取：自己渲染 Turnstile → 拿 token → 直接调他们的领取接口。
    参数与他们前端完全一致（sitekey/theme/size 与 render() 相同；device_token 取自 welcome-credit）。"""
    import time as _t
    try:
        relay_fpjs_routes(page, log)
    except Exception:
        pass
    sitekey = ""
    try:
        st, d = _fetch(page, "/api/public/auth-config")
        sitekey = str((d or {}).get("turnstile_site_key") or "")
    except Exception:
        pass
    if not sitekey:
        return False, "拿不到 turnstile sitekey"
    r = page.evaluate(CLAIM_DIRECT_JS, {"sitekey": sitekey})
    if isinstance(r, dict) and r.get("err"):
        return False, f"自渲染控件失败：{r['err']}"
    if log:
        log(f"[浏览器] 自渲染领取控件（widget={r.get('widget')}，turnstile={r.get('type')}）")
    tok = ""
    waited = 0
    while waited < 120:
        page.wait_for_timeout(2500)
        waited += 2.5
        tok = page.evaluate(CLAIM_POLL_JS) or ""
        if tok:
            break
        if waited in (15, 45, 75):                    # 卡住就点一次挑战复选框
            try:
                box = page.locator("#zClaimSlot iframe").first.bounding_box()
                if box:
                    x, y = box["x"] + 30, box["y"] + box["height"] / 2
                    page.mouse.move(x - 70, y - 40)
                    page.wait_for_timeout(200)
                    page.mouse.move(x, y, steps=16)
                    page.mouse.click(x, y)
                    if log:
                        log("[浏览器] 点了一次自渲染控件的复选框")
            except Exception:
                pass
        if not tok and waited % 20 == 0 and log:
            log(f"[浏览器] 等自渲染控件出 token… {int(waited)}s")
    if not tok:
        return False, "自渲染控件 120s 未产出 token（CF 未放行）"
    if log:
        log(f"[浏览器] ✔ 自渲染控件拿到 token（{len(tok)} 字节）→ 直接提交领取")
    dtok = str((wc or {}).get("device_token") or "")
    if dtok:
        try:
            page.evaluate("(t) => { document.cookie = 'joy_wc_did=' + t + '; path=/; max-age=86400'; }", dtok)
        except Exception:
            pass
    body = {"turnstile_token": tok, "fp_event_id": "", "visitor_id": "", "fp_attempted": False,
            "claim_nonce": "", "device_token": dtok}
    st2, d2 = _fetch(page, "/api/user/welcome-credit/claim", "POST", body)
    if log:
        log(f"[浏览器] 领取第一步返回：HTTP {st2} {json.dumps(d2, ensure_ascii=False)[:220]}")
    d2 = d2 or {}
    if d2.get("need_fingerprint"):
        # 第二步：设备指纹（他们前端就是 import FingerprintJS v4 → start({region, linkedId})）
        pub = str(d2.get("fingerprint_public_key") or "")
        region = str(d2.get("fingerprint_region") or (wc or {}).get("fingerprint_region") or "us")
        nonce = str(d2.get("claim_nonce") or "")
        if not pub:
            return False, "平台要求设备指纹但没给公钥（fingerprint_public_key 为空）"
        if log:
            log(f"[浏览器] 走第二步：设备指纹（region={region}，public_key={pub[:10]}…）")
        src_b64 = ""
        try:
            import base64 as _b64
            import requests as _rq
            rr = _rq.get("https://fpjscdn.net/v4/" + pub, timeout=30,
                         headers={"User-Agent": UA if "UA" in globals() else "Mozilla/5.0",
                                  "Origin": BASE})
            if rr.status_code == 200 and len(rr.content) > 1000:
                src_b64 = _b64.b64encode(rr.content).decode()
                if log:
                    log(f"[浏览器] 服务器直连取到指纹模块（{len(rr.content) // 1024} KB），备用注入")
            elif log:
                log(f"[浏览器] 服务器直连取指纹模块失败：HTTP {rr.status_code}")
        except Exception as e:
            if log:
                log(f"[浏览器] 服务器直连取指纹模块异常：{type(e).__name__}: {str(e)[:80]}")
        fp = page.evaluate(FP_JS, {"pub": pub, "region": region, "nonce": nonce, "src_b64": src_b64})
        if log:
            log(f"[浏览器] 指纹结果：{json.dumps(fp, ensure_ascii=False)[:200]}")
        if isinstance(fp, dict) and fp.get("err"):
            return False, f"设备指纹采集失败：{fp['err']}"
        body2 = {"turnstile_token": "", "fp_event_id": str(fp.get("event_id") or ""),
                 "visitor_id": str(fp.get("visitor_id") or ""), "fp_attempted": True,
                 "claim_nonce": nonce,
                 "device_token": str(d2.get("device_token") or dtok)}
        st3, d3 = _fetch(page, "/api/user/welcome-credit/claim", "POST", body2)
        if log:
            log(f"[浏览器] 领取第二步返回：HTTP {st3} {json.dumps(d3, ensure_ascii=False)[:260]}")
        d3 = d3 or {}
        ok = bool(d3.get("success"))
        msg = d3.get("message") or d3.get("error") or ("领取成功" if ok else "领取失败")
        return ok, str(msg)[:220]
    ok = bool(d2.get("success"))
    msg = d2.get("message") or d2.get("error") or ("领取成功" if ok else "领取失败")
    return ok, str(msg)[:220]

FP_JS = """async (arg) => {
  const url = 'https://fpjscdn.net/v4/' + encodeURIComponent(arg.pub);
  const opts = {region: arg.region, linkedId: arg.nonce};
  // ① 与官网前端一模一样的动态 import
  try {
    const mod = await import(url);
    const F = (mod && mod.start) ? mod : (mod && mod.default);
    if (F && typeof F.start === 'function') {
      const r = await F.start(opts);
      return {event_id: (r && (r.event_id || r.requestId || r.request_id)) || '',
              visitor_id: (r && (r.visitor_id || r.visitorId)) || '', via: 'import'};
    }
  } catch (e) { /* 落到兜底 */ }
  // ①.5 出口代理会把 CDN 响应改坏（实测 SRI 都对不上）→ 用服务器直连取回来的原文，走 Blob 模块
  if (arg.src_b64) {
    try {
      const bin = atob(arg.src_b64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const text = new TextDecoder('utf-8').decode(bytes);
      const blobUrl = URL.createObjectURL(new Blob([text], {type: 'text/javascript'}));
      const mod = await import(blobUrl);
      const F = (mod && mod.start) ? mod : (mod && mod.default);
      if (F && typeof F.start === 'function') {
        const r = await F.start(opts);
        return {event_id: (r && (r.event_id || r.requestId || r.request_id)) || '',
                visitor_id: (r && (r.visitor_id || r.visitorId)) || '', via: 'blob'};
      }
      return {err: 'blob 模块没有 start()'};
    } catch (e) { return {err: 'blob: ' + String(e && (e.message || e)).slice(0, 120)}; }
  }
  // ② 内联 module 脚本兜底（页面没有 CSP，内联可执行）
  try {
    window.__zFp = null;
    const s = document.createElement('script');
    s.type = 'module';
    s.textContent = "import('" + url + "').then(m => { const F = (m && m.start) ? m : (m && m.default);"
      + " return F.start(" + JSON.stringify(opts) + "); })"
      + ".then(r => { window.__zFp = {event_id: (r && (r.event_id || r.requestId || r.request_id)) || '',"
      + " visitor_id: (r && (r.visitor_id || r.visitorId)) || '', via: 'inline'}; })"
      + ".catch(e => { window.__zFp = {err: String(e && e.message || e)}; });";
    document.head.appendChild(s);
    for (let i = 0; i < 60; i++) {
      if (window.__zFp) return window.__zFp;
      await new Promise(r => setTimeout(r, 500));
    }
    return {err: '指纹模块 30s 没返回'};
  } catch (e) { return {err: String(e && e.message || e)}; }
}"""

# ============ 自渲染 Turnstile（不依赖页面那条时好时坏的脚本链）============
SELF_TS_JS = """async (arg) => {
  let el = document.getElementById(arg.slot);
  if (!el) {
    el = document.createElement('div');
    el.id = arg.slot;
    el.style.cssText = 'min-height:70px;display:flex;align-items:center;justify-content:center;margin:1rem 0';
    (document.querySelector('main') || document.body).appendChild(el);
  }
  if (typeof window.turnstile === 'undefined') {
    const TS_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    let tag = document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]');
    if (!tag) {
      tag = document.createElement('script');
      tag.src = TS_SRC; tag.async = true;
      document.head.appendChild(tag);
      await new Promise(r => setTimeout(r, 1200));
    } else {
      document.head.appendChild(tag);
      await new Promise(r => setTimeout(r, 300));
    }
    if (typeof window.turnstile === 'undefined' && arg.api_src_b64) {
      try {
        const bin = atob(arg.api_src_b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        (0, eval)(new TextDecoder('utf-8').decode(bytes));
      } catch (e) { return {err: 'api.js eval: ' + (e && e.message)}; }
    }
    for (let i = 0; i < 40 && typeof window.turnstile === 'undefined'; i++) {
      await new Promise(r => setTimeout(r, 250));
    }
  }
  if (typeof window.turnstile === 'undefined') return {err: 'turnstile 全局不可用'};
  try {
    if (window.__zWid != null) { try { turnstile.remove(window.__zWid); } catch (e) {} }
    window.__zWid = turnstile.render(el, {sitekey: arg.sitekey, theme: 'light', size: 'flexible'});
  } catch (e) { return {err: 'render: ' + (e && e.message)}; }
  return {widget: window.__zWid};
}"""

SELF_TS_POLL_JS = """() => { try {
  if (window.__zWid == null || typeof window.turnstile === 'undefined') return '';
  return window.turnstile.getResponse(window.__zWid) || '';
} catch (e) { return ''; } }"""


def _fetch_text_direct(url, timeout=30):
    """服务器直连取文件原文（出口代理会把部分 CDN 响应改坏，正文由服务器直连取）"""
    import requests as _rq
    try:
        r = _rq.get(url, timeout=timeout, headers={"User-Agent":
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"})
        if r.status_code == 200 and len(r.content) > 500:
            import base64 as _b64
            return _b64.b64encode(r.content).decode()
    except Exception:
        pass
    return ""


def _sitekey(page, log=None):
    try:
        st, d = _fetch(page, "/api/public/auth-config")
        return str((d or {}).get("turnstile_site_key") or "")
    except Exception:
        return ""


def selfrender_token(page, sitekey, log=None, slot="zTsSlot", wait=100):
    """自渲染 Turnstile 并拿 token（CF 的 api.js 拿不到就由服务器直连取原文注入）"""
    api_b64 = ""
    if not page.evaluate("() => typeof window.turnstile !== 'undefined'"):
        api_b64 = _fetch_text_direct("https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit")
    r = page.evaluate(SELF_TS_JS, {"slot": slot, "sitekey": sitekey, "api_src_b64": api_b64})
    if isinstance(r, dict) and r.get("err"):
        if log:
            log(f"[浏览器] 自渲染 Turnstile 失败：{r['err']}")
        return ""
    if log:
        log(f"[浏览器] 自渲染 Turnstile（{slot}，widget={r.get('widget')}）")
    waited = 0
    while waited < wait:
        page.wait_for_timeout(2500)
        waited += 2.5
        tok = page.evaluate(SELF_TS_POLL_JS) or ""
        if tok:
            if log:
                log(f"[浏览器] ✔ Turnstile token 到手（{len(tok)} 字节，{int(waited)}s）")
            return tok
        if waited in (15, 40, 65):
            try:
                box = page.locator("#" + slot + " iframe").first.bounding_box()
                if box:
                    x, y = box["x"] + 30, box["y"] + box["height"] / 2
                    page.mouse.move(x - 70, y - 40)
                    page.wait_for_timeout(200)
                    page.mouse.move(x, y, steps=16)
                    page.mouse.click(x, y)
            except Exception:
                pass
    if log:
        log("[浏览器] 自渲染 Turnstile 到点没出 token")
    return ""


def register_via_api(page, email, password, log=None):
    """直接调他们的注册接口（与页面表单同一 payload），自己带 Turnstile token。
    返回 (ok, detail, code)"""
    sitekey = _sitekey(page, log)
    if not sitekey:
        return False, "拿不到 sitekey", "sitekey"
    for attempt in (1, 2):
        tok = selfrender_token(page, sitekey, log=log, slot="zRegSlot")
        if not tok:
            return False, "自渲染 Turnstile 拿不到 token", "turnstile"
        body = {"email": email, "password": password}
        if tok and tok != "disabled":
            body["turnstile_token"] = tok
        st, d = _fetch(page, "/api/auth/register", "POST", body)
        d = d or {}
        if log:
            log(f"[浏览器] 注册接口（第 {attempt} 次）：HTTP {st} {json.dumps(d, ensure_ascii=False)[:180]}")
        _msg = str(d.get("message") or "")
        # 他们的成功响应是 {message: "Registration successful!...", user:{...}}——没有 success 字段
        if st == 200 and (d.get("success") or d.get("user") or "successful" in _msg.lower()):
            return True, _msg or "注册成功", ""
        code = str(d.get("error") or "")
        if code in ("captcha_failed", "turnstile_failed"):
            continue                                  # 换一个新 token 再来一次
        if code == "region_not_supported":
            return False, "地区不支持（换出口）", "region"
        _low = (code + " " + str(d.get("message") or "")).lower()
        if any(k in _low for k in ("exist", "already registered", "registered", "已注册")):
            return True, "邮箱已注册（跳过注册，直接激活/登录）", "exists"
        return False, str(d.get("message") or code or f"HTTP {st}")[:160], code or "api"
    return False, "注册被拒（Turnstile 连续失败）", "turnstile"


def login_via_api(page, email, password, log=None):
    """直接调登录接口，拿 JWT 写进 localStorage（与页面登录同一 payload）。
    返回 (ok, detail, jwt)"""
    sitekey = _sitekey(page, log)
    for attempt in (1, 2):
        tok = selfrender_token(page, sitekey, log=log, slot="zLoginSlot") if sitekey else ""
        if not tok:
            return False, "自渲染 Turnstile 拿不到 token", ""
        body = {"username": email, "password": password}
        if tok and tok != "disabled":
            body["turnstile_token"] = tok
        st, d = _fetch(page, "/api/auth/login", "POST", body)
        d = d or {}
        if log:
            log(f"[浏览器] 登录接口（第 {attempt} 次）：HTTP {st} {json.dumps(d, ensure_ascii=False)[:160]}")
        jwt = str(d.get("token") or "")
        if st == 200 and jwt:
            try:
                page.evaluate("(t) => localStorage.setItem('joypyroxy_token', t)", jwt)
            except Exception:
                pass
            return True, "已登录控制台（接口直登）", jwt
        code = str(d.get("error") or "")
        if code in ("captcha_failed", "turnstile_failed"):
            continue
        return False, str(d.get("message") or code or f"HTTP {st}")[:160], ""
    return False, "登录被拒（Turnstile 连续失败）", ""

def relay_fpjs_routes(page, log=None):
    """把 FingerprintJS 相关域名的请求改成服务器直连转发（playwright route intercept）。
    出口代理会把这些 CDN 响应改坏/拦掉，导致指纹事件拿不到 event_id。"""
    import requests as _rq

    def handler(route):
        req = route.request
        try:
            h = {k: v for k, v in (req.headers or {}).items()
                 if k.lower() not in ("host", "content-length", "accept-encoding", "connection")}
            kw = {"headers": h, "timeout": 30}
            if req.method.upper() == "POST" and req.post_data_buffer:
                kw["data"] = req.post_data_buffer
            r = _rq.request(req.method, req.url, **kw)
            route.fulfill(status=r.status_code, headers=dict(r.headers), body=r.content)
        except Exception as e:
            if log:
                log(f"[浏览器] 指纹转发失败（{req.url.split('/')[2]}）：{type(e).__name__}: {str(e)[:60]}")
            try:
                route.abort()
            except Exception:
                pass

    n = 0
    for pat in ("**fpjscdn.net/**", "**api.fpjs.io/**", "**api.fpjs.com/**"):
        try:
            page.route(pat, handler)
            n += 1
        except Exception:
            pass
    if log and n:
        log(f"[浏览器] 指纹域名已改为服务器直连转发（{n} 条规则）")
    return n
