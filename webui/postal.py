# -*- coding: utf-8 -*-
"""邮局（WGTemporaryEmail 协议）客户端：建箱 / 收信 / 取验证链接。

被三处复用：Web 端（渠道测试、NoneCap 注册）、worker（NoneCap 全自动注册任务）。
协议与 steam_full_flow.py 的邮箱部分一致：
    POST {base}/permanent-addresses  长效（X-API-Key）   POST {base}/addresses 短效
    GET  {base}/{token}/emails?per_page=10               GET {base}/{token}/emails/{id}
"""
import json
import re
import time
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")


def create_mailbox(ch, username=None):
    """按渠道配置建一个信箱。username 传了就优先用它（对齐引擎：前缀可选 + 随机位数）。"""
    import random
    import string
    base = (ch.get("base") or "").rstrip("/")
    if not base:
        raise ValueError("邮箱渠道没填接口地址")
    headers = {"X-API-Key": ch["api_key"]} if ch.get("api_key") else {}
    if not username:
        ds = [d.strip() for d in (ch.get("domains") or "").split(",") if d.strip()]
        domain = random.choice(ds) if ds else (ch.get("domain") or None)
        n = max(4, min(int(ch.get("rand_len") or 10), 48))
        pre = (ch.get("prefix") or "") if ch.get("prefix_enabled") else ""
        username = pre + "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))
    else:
        ds = [d.strip() for d in (ch.get("domains") or "").split(",") if d.strip()]
        domain = random.choice(ds) if ds else (ch.get("domain") or None)
    if (ch.get("mode") or "temp") == "permanent":
        r = requests.post(f"{base}/permanent-addresses",
                          json={"username": username, "domain": domain},
                          headers=headers, timeout=30, verify=False)
    else:
        r = requests.post(f"{base}/addresses", json={"domain": domain},
                          headers=headers, timeout=30, verify=False)
    if r.status_code == 409:
        raise ValueError(f"邮箱 {username} 已存在（换个用户名）")
    r.raise_for_status()
    j = r.json() or {}
    if not j.get("email") or not j.get("token"):
        raise ValueError(f"邮局返回不完整：{str(j)[:150]}")
    return {"email": j["email"], "token": j["token"], "id": j.get("id"),
            "base": base, "expires_at": j.get("expires_at")}


def emails(base, token):
    r = requests.get(f"{base.rstrip('/')}/{token}/emails?per_page=10",
                     timeout=30, verify=False)
    return (r.json() or {}).get("emails") or [] if r.status_code == 200 else []


def detail(base, token, mid):
    r = requests.get(f"{base.rstrip('/')}/{token}/emails/{mid}", timeout=30, verify=False)
    return r.json() if r.status_code == 200 else None


RE_NC_VERIFY = re.compile(r"https?://dashboard\.nonecap\.com/verify\?token=[A-Za-z0-9._\-]+")


def wait_verify_link_outlook(row, timeout=180, log=None, proxy=""):
    """微软邮箱池：用 MS Graph 轮询 NoneCap 验证邮件。返回 (链接, OutlookMail 客户端)"""
    from . import outlook
    cli = outlook.from_row(row, proxy=proxy or "")
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            for m in cli.list_messages(top=10):
                fa = (m.get("from_address") or "").lower()
                if "nonecap" not in fa and "nonecap" not in (m.get("subject") or "").lower():
                    continue
                d = cli.get_message(m["id"])
                hit = RE_NC_VERIFY.search(d.get("text") or "") or RE_NC_VERIFY.search(d.get("html") or "")
                if hit:
                    return hit.group(0), cli
        except Exception as e:
            if log:
                log(f"读微软邮箱出错（{type(e).__name__}: {str(e)[:80]}），继续等…")
        if log:
            log("等待 NoneCap 验证邮件（微软邮箱）…")
        time.sleep(5)
    return None, cli


RE_JOY_LINK = re.compile(r"https?://(?:www\.)?joyproxy\.com/[^\s\"'<>)]+", re.I)


def wait_joy_activate_link(row, timeout=240, log=None, proxy=""):
    """微软邮箱池：等 JoyProxy 的激活邮件并取激活链接。返回 (链接|None, OutlookMail 客户端)
    优先取带 activate/verify/token 的链接；找不到就退回邮件里第一条 joyproxy 链接。"""
    from . import outlook
    cli = outlook.from_row(row, proxy=proxy or "")
    deadline = time.time() + float(timeout)
    seen = 0
    while time.time() < deadline:
        try:
            for m in cli.list_messages(top=10):
                fa = (m.get("from_address") or "").lower()
                subj = (m.get("subject") or "").lower()
                if "joyproxy" not in fa and "joyproxy" not in subj:
                    continue
                d = cli.get_message(m["id"])
                body = (d.get("text") or "") + "\n" + (d.get("html") or "")
                links = RE_JOY_LINK.findall(body)
                if links:
                    best = None
                    for u in links:
                        lu = u.lower()
                        if any(k in lu for k in ("activate", "activat", "verify", "confirm", "token")):
                            best = u
                            break
                    link = (best or links[0]).rstrip(").,;\"'")
                    if log:
                        log(f"收到 JoyProxy 激活邮件（{m.get('subject','')[:40]}）→ {link[:90]}")
                    return link, cli
        except Exception as e:
            if log:
                log(f"读微软邮箱出错（{type(e).__name__}: {str(e)[:80]}），继续等…")
        seen += 1
        if log and seen % 6 == 0:
            log("等待 JoyProxy 激活邮件（微软邮箱）…")
        time.sleep(5)
    return None, cli


def wait_verify_link(base, token, timeout=180, log=None):
    """轮询信箱取 NoneCap 验证链接（https://dashboard.nonecap.com/verify?token=…）
    顺便把「收到几封、什么主题」写进日志——信没来 vs 我们没匹配上，一眼能分辨。"""
    pat = re.compile(r"https?://dashboard\.nonecap\.com/verify\?token=[A-Za-z0-9._\-]+")
    deadline = time.time() + float(timeout)
    seen_n = -1
    while time.time() < deadline:
        try:
            msgs = emails(base, token)
        except Exception as e:
            if log:
                log(f"读信箱出错（{type(e).__name__}），继续等…")
            msgs = []
        if log and len(msgs) != seen_n:
            seen_n = len(msgs)
            if msgs:
                sub = str((msgs[0] or {}).get("subject") or "")[:50]
                log(f"信箱收到 {seen_n} 封邮件（最新：{sub or '无主题'}）")
            else:
                log("信箱暂时没有新邮件…")
        for m in msgs:
            d = detail(base, token, m.get("id") or m.get("email_id"))
            if not d:
                continue
            blob = json.dumps(d, ensure_ascii=False).replace("\\/", "/").replace("&amp;", "&")
            hit = pat.search(blob)
            if hit:
                return hit.group(0)
        if log:
            log("等待 NoneCap 验证邮件…")
        time.sleep(4)
    return None


def click_link(link):
    """访问验证链接（纯协议）"""
    r = requests.get(link, timeout=30, verify=False, allow_redirects=True,
                     headers={"User-Agent": UA})
    return r.status_code, (r.text or "")[:200]


def host_of(url):
    return urllib.parse.urlparse(url or "").hostname or ""
