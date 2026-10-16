# -*- coding: utf-8 -*-
"""JoyProxy（joyproxy.com）账号模式客户端：
登录 → 领 $5 赠金 → 买住宅 1GB（余额支付）→ 取 API 提取链接（api.joyproxy.com/v2/extract?token=…）

只用官方站内接口（都从控制台页面逆向确认）：
  POST /api/auth/login                       {username, password[, turnstile_token]} → {token}
  GET  /api/user/profile                     Bearer → {balance, signup_bonus_usd, ...}
  GET  /api/user/welcome-credit              Bearer → 赠金状态（含 fingerprint/turnstile 要求与 device_token）
  POST /api/user/welcome-credit/claim        Bearer → {turnstile_token, device_token, ...}
  GET  /api/pricing                          → {pricing: {short_term_proxy_ip: [{id,name,price}…]}}  住宅/移动/商业/长效价目
  POST /api/orders/create                    Bearer → {product_id, product_name, type, quantity,
                                                        payment_method:"balance", network_type, total_price[, custom_traffic_gb]}
  GET  /api/token/get                        Bearer → {token}   提取用 token（拼 extract 链接）
提取：https://api.joyproxy.com/v2/extract?token=…&count=…&duration=…&protocol=…&format=…&output=…
"""
import json
import time

import requests

BASE = "https://www.joyproxy.com"
EXTRACT_BASE = "https://api.joyproxy.com/v2"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 住宅/移动/商业 三类短效套餐的价目表字段（/api/pricing）
NET_PRICE_KEY = {"residential": "short_term_proxy_ip",
                 "cellular": "short_term_proxy_ip_cellular",
                 "business": "short_term_proxy_ip_business"}
NET_LABEL = {"residential": "住宅", "cellular": "移动", "business": "商业"}

# 赠金状态 → 中文原因
WC_REASON = {
    "denied_duplicate_device": "该设备指纹已领过赠金（同设备重复）",
    "denied_duplicate_account": "该账号已领过赠金",
    "denied_fingerprint": "设备指纹未通过校验",
    "already_claimed": "赠金已领取过",
    "expired": "领取窗口已过期",
    "ineligible": "该账号不符合领取条件",
    "rate_limited": "尝试过于频繁，稍后再试",
    "verify_failed": "安全校验未通过（Turnstile）",
    "denied": "领取被拒（平台判定不符合条件）",
}


class JoyError(Exception):
    """code: auth / turnstile / network / api / insufficient"""

    def __init__(self, code, msg, data=None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.data = data


def _req(method, url, token="", payload=None, proxy=None, timeout=30, headers=None):
    h = {"User-Agent": UA, "Accept": "application/json"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = "Bearer " + token
    if headers:
        h.update(headers)
    kw = {"timeout": timeout, "headers": h}
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    try:
        r = requests.request(method, url, data=(json.dumps(payload) if payload is not None else None), **kw)
    except Exception as e:
        raise JoyError("network", f"{type(e).__name__}: {str(e)[:120]}")
    try:
        d = r.json()
    except Exception:
        d = {"raw": (r.text or "")[:200]}
    return r.status_code, d


def _need(r, d, ok=(200,), what=""):
    if r in ok:
        return d
    msg = (d or {}).get("error") or (d or {}).get("message") or (d or {}).get("raw") or f"HTTP {r}"
    code = "api"
    if r in (401, 403):
        code = "auth" if "turnstile" not in str(msg).lower() else "turnstile"
    raise JoyError(code, f"{what}{msg}"[:200], d)


def login(email, password, proxy=None, timeout=30):
    """登录拿 JWT（控制台所有接口用它）。需要人机校验时抛 JoyError('turnstile')"""
    email = str(email or "").strip()
    if not email or not password:
        raise JoyError("auth", "邮箱或密码为空")
    r, d = _req("POST", BASE + "/api/auth/login",
                payload={"username": email, "password": password}, proxy=proxy, timeout=timeout)
    if r == 403 and ("turnstile" in json.dumps(d, ensure_ascii=False).lower()):
        raise JoyError("turnstile", "登录需要人机校验（Turnstile），请在面板用「浏览器开通」", d)
    if r >= 400 or not (d or {}).get("token"):
        msg = (d or {}).get("error") or (d or {}).get("message") or (d or {}).get("raw") or f"HTTP {r}"
        raise JoyError("auth", f"登录失败：{msg}"[:200], d)
    return {"token": d["token"], "user": d.get("user") or {}}


def profile(token, proxy=None):
    r, d = _req("GET", BASE + "/api/user/profile", token=token, proxy=proxy)
    return _need(r, d, what="读账户失败：")


def welcome_credit(token, proxy=None):
    r, d = _req("GET", BASE + "/api/user/welcome-credit", token=token, proxy=proxy)
    return _need(r, d, what="读赠金状态失败：")


def wc_reason(wc):
    st = str((wc or {}).get("status") or "")
    if not wc or not wc.get("enabled"):
        return "赠金活动当前不可用"
    if st in ("", "available", "claimable", "eligible", "ready"):
        return ""
    return WC_REASON.get(st, f"不可领取（{st}）")


def claim_welcome_credit(token, device_token="", turnstile_token="", proxy=None):
    body = {"turnstile_token": turnstile_token or "", "fp_event_id": "", "visitor_id": "",
            "fp_attempted": False, "claim_nonce": "", "device_token": device_token or ""}
    r, d = _req("POST", BASE + "/api/user/welcome-credit/claim", token=token, payload=body, proxy=proxy)
    if r >= 400 or not (d or {}).get("success"):
        code = (d or {}).get("error") or (d or {}).get("code") or f"HTTP {r}"
        if "turnstile" in str(code).lower():
            raise JoyError("turnstile", "领取需要人机校验（Turnstile）", d)
        raise JoyError("api", f"领取失败：{WC_REASON.get(str(code), code)}"[:200], d)
    return d


def pricing(proxy=None):
    r, d = _req("GET", BASE + "/api/pricing?_=%d" % int(time.time() * 1000), proxy=proxy)
    return _need(r, d, what="读价目表失败：")


def _price_of(pricing_data, network_type, product_id=""):
    key = NET_PRICE_KEY.get(network_type or "residential", "short_term_proxy_ip")
    plans = ((pricing_data or {}).get("pricing") or {}).get(key) or []
    if product_id:
        for p in plans:
            if str(p.get("id")) == str(product_id):
                return p
    for p in plans:                                   # 没指定就取最小 1GB 档
        if "1gb" in str(p.get("id", "")).lower() or str(p.get("name", "")).startswith("1 "):
            return p
    return plans[0] if plans else None


def buy_short_term(token, network_type="residential", gb=1, product_id="", proxy=None):
    """余额支付买短效套餐（默认住宅 1GB）。返回 {order_id, price, product, balance_before}"""
    pf = profile(token, proxy=proxy)
    bal = float(pf.get("balance") or 0)
    pr = _price_of(pricing(proxy=proxy), network_type)
    if not pr:
        raise JoyError("api", f"价目表里没有 {NET_LABEL.get(network_type, network_type)} 短效套餐")
    pid, name, price = str(pr.get("id")), str(pr.get("name")), float(pr.get("price") or 0)
    if product_id:
        pid = str(product_id)
    if bal + 1e-9 < price:
        raise JoyError("insufficient",
                       f"余额不足：需要 ${price:.2f}，当前 ${bal:.2f}（先领 $5 赠金）",
                       {"balance": bal, "price": price})
    body = {"product_id": pid, "product_name": name, "type": "short-term", "quantity": 1,
            "payment_method": "balance", "network_type": network_type or "residential",
            "total_price": price}
    if network_type in ("residential", "cellular", "business") and gb and gb > 1:
        body["custom_traffic_gb"] = int(gb)
        body["total_price"] = round(price / max(1, int(str(pr.get("name", "1")).split()[0] or 1)) * gb, 2)
    r, d = _req("POST", BASE + "/api/orders/create", token=token, payload=body, proxy=proxy, timeout=45)
    if r >= 400 or not (d or {}).get("success"):
        msg = (d or {}).get("error") or (d or {}).get("message") or f"HTTP {r}"
        code = "insufficient" if "insufficient" in str(msg).lower() or "balance" in str(msg).lower() else "api"
        raise JoyError(code, f"下单失败：{msg}"[:200], d)
    return {"order_id": (d.get("order") or {}).get("id"), "price": price, "product": f"{name}({pid})",
            "balance_before": bal}


def api_token(token, proxy=None):
    r, d = _req("GET", BASE + "/api/token/get", token=token, proxy=proxy)
    d = _need(r, d, what="取提取 token 失败：")
    t = str((d or {}).get("token") or "").strip()
    if not t:
        raise JoyError("api", "接口没返回提取 token")
    return t


def extract_url(tok, count=200, duration="1m", protocol="http", fmt="crlf", output="http",
                network_type="", country_geoname_id="", city_geoname_id=""):
    """拼 api.joyproxy.com/v2/extract 链接（network_type 留空 = 住宅默认，官方前端同规则）"""
    from urllib.parse import urlencode
    q = [("token", tok), ("count", str(count)), ("duration", duration),
         ("protocol", protocol), ("format", fmt), ("output", output)]
    if city_geoname_id:
        q.append(("city_geoname_id", str(city_geoname_id)))
    elif country_geoname_id:
        q.append(("country_geoname_id", str(country_geoname_id)))
    nt = str(network_type or "").strip().lower()
    if nt and nt != "residential":
        q.append(("network_type", nt))
    return EXTRACT_BASE + "/extract?" + urlencode(q)


def provision(email, password, gb=1, network_type="residential", product_id="",
              do_claim=True, do_buy=True, device_token="", claim_ok=None, proxy=None, log=print,
              token=""):
    """一键开通：登录（或复用已保存登录态）→ 领赠金 → 买套餐 → 取提取链接。
    token 给了就跳过登录（面板/worker 里存的 JWT，24h 有效；协议路径不需要人机校验）。
    claim_ok: 浏览器里已领成功时传 True（跳过 requests 领取尝试）。
    返回 {ok, steps:[{step, ok, detail}], token, extract_url, balance, welcome}"""
    out = {"ok": False, "steps": [], "token": "", "extract_url": "", "balance": None, "welcome": None}

    def step(name, ok, detail=""):
        out["steps"].append({"step": name, "ok": bool(ok), "detail": str(detail)[:300]})
        log(("✔ " if ok else "✘ ") + name + ("：" + str(detail)[:200] if detail else ""))
        return ok

    if token:
        try:
            pf = profile(token, proxy=proxy)
            step("登录 JoyProxy", True, f"复用已保存登录态（{email or pf.get('email')}）")
        except JoyError as e:
            step("登录 JoyProxy", False, f"已保存登录态失效：{e.msg}")
            return out
        out["token"] = token
    else:
        try:
            li = login(email, password, proxy=proxy)
        except JoyError as e:
            step("登录 JoyProxy", False, e.msg)
            return out
        token = li["token"]
        out["token"] = token
        step("登录 JoyProxy", True, f"账号 {email}")

    try:
        pf = profile(token, proxy=proxy)
        out["balance"] = float(pf.get("balance") or 0)
        step("读账户", True, f"余额 ${out['balance']:.2f}")
    except JoyError as e:
        step("读账户", False, e.msg)
        return out

    # —— 赠金 ——
    if do_claim:
        try:
            wc = welcome_credit(token, proxy=proxy)
            out["welcome"] = wc
            reason = wc_reason(wc)
            if claim_ok:
                step("$5 赠金", True, "浏览器端已领取")
            elif reason:
                step("$5 赠金", False, reason)
            elif wc.get("turnstile_required"):
                step("$5 赠金", False, "需要人机校验（Turnstile）→ 用「浏览器领取」")
            else:
                dt = device_token or wc.get("device_token") or ""
                claim_welcome_credit(token, device_token=dt, proxy=proxy)
                pf2 = profile(token, proxy=proxy)
                out["balance"] = float(pf2.get("balance") or 0)
                step("$5 赠金", True, f"已领取，余额 ${out['balance']:.2f}")
        except JoyError as e:
            step("$5 赠金", False, e.msg)
    else:
        step("$5 赠金", True, "本次跳过（未开启自动领取）")

    # —— 购买 1GB 住宅 ——
    if do_buy:
        try:
            b = buy_short_term(token, network_type=network_type, gb=gb,
                               product_id=product_id, proxy=proxy)
            out["balance"] = float((profile(token, proxy=proxy).get("balance")) or 0)
            step("购买套餐", True, f"{b['product']} ${b['price']:.2f} 已用余额支付，订单 {b['order_id']}，"
                                   f"余额 ${out['balance']:.2f}")
        except JoyError as e:
            step("购买套餐", False, e.msg)
    else:
        step("购买套餐", True, "本次跳过（未开启自动购买）")

    # —— 提取链接 ——
    try:
        tok = api_token(token, proxy=proxy)
        out["extract_url"] = extract_url(tok, network_type=network_type)
        step("获取提取链接", True, out["extract_url"][:110] + "…")
    except JoyError as e:
        step("获取提取链接", False, e.msg)
        return out

    out["ok"] = all(s["ok"] for s in out["steps"])
    bad = [s for s in out["steps"] if not s["ok"]]
    out["detail"] = "完成" if out["ok"] else "；".join(f"{s['step']}：{s['detail']}" for s in bad)[:220]
    return out


def refresh_extract_url(email, password, network_type="residential", proxy=None, log=print):
    """只刷新提取链接（不购买、不领赠金）：面板/worker 定期调用"""
    li = login(email, password, proxy=proxy)
    tok = api_token(li["token"], proxy=proxy)
    url = extract_url(tok, network_type=network_type)
    log(f"提取链接已刷新：{url[:100]}…")
    return {"token": tok, "extract_url": url}
