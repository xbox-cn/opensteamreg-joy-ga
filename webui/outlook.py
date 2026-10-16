# -*- coding: utf-8 -*-
"""微软邮箱（Outlook / MS Graph）取件客户端 —— 协议对齐 xbox-cn/Steam_Reg_Manger 的 app/outlook_mail.py。

账号行（导入邮箱池时每行一个）：
    email----password----client_id----refresh_token[----辅助邮箱[----辅助密码]]

要点（对方仓库实测结论）：
· OAuth2：POST /common/oauth2/v2.0/token，grant_type=refresh_token + client_id +
  scope="offline_access https://graph.microsoft.com/.default"；expires_at = now + expires_in - 300（提前 5 分钟）
· Graph 401 时强刷一次再重试；刷新回来的 refresh_token 可能与旧值不同（轮换），要存回池子
· 读信：GET /me/messages?$top&$select&$orderby=receivedDateTime desc（有 $search 时不能带 orderby/filter）
· 正文优先 body.content；HTML 转纯文本再交给注册引擎做正则匹配
"""
import html as _html
import json
import re
import time
import urllib.parse

import requests

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "offline_access https://graph.microsoft.com/.default"
LIST_SELECT = "id,subject,from,receivedDateTime,bodyPreview,isRead"
DETAIL_SELECT = ("id,subject,from,receivedDateTime,body,bodyPreview,internetMessageId,isRead")
RE_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
RE_BR = re.compile(r"<(br|/p|/div|/tr|/li|/h[1-6])[^>]*>", re.I)
RE_TAG = re.compile(r"<[^>]+>")


class OutlookError(Exception):
    pass


class AuthError(OutlookError):
    pass


class ApiError(OutlookError):
    def __init__(self, code, msg):
        super().__init__(f"{code}: {msg}")
        self.code = code


class NetworkError(OutlookError):
    pass


def html_to_text(s):
    """HTML → 纯文本（保留换行，去脚本/样式、反转义实体）"""
    if not s:
        return ""
    t = RE_SCRIPT.sub(" ", s)
    t = RE_BR.sub("\n", t)
    t = RE_TAG.sub(" ", t)
    t = _html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def parse_line(raw):
    """解析一行邮箱：email----password----client_id----refresh_token[----辅助邮箱[----辅助密码]]"""
    s = (raw or "").strip()
    if not s:
        raise ValueError("空行")
    parts = [p.strip() for p in s.split("----")]
    if len(parts) < 4:
        raise ValueError("段数不足 4（需要 email----password----client_id----refresh_token）")
    email, password, client_id, refresh_token = parts[0], parts[1], parts[2], parts[3]
    if not email or "@" not in email:
        raise ValueError("第 1 段不是邮箱")
    if not client_id:
        raise ValueError("第 3 段 client_id 为空")
    if not refresh_token:
        raise ValueError("第 4 段 refresh_token 为空")
    rec_email = parts[4] if len(parts) >= 5 else ""
    rec_pw = "----".join(parts[5:]) if len(parts) >= 6 else ""
    return {"email": email, "password": password, "client_id": client_id,
            "refresh_token": refresh_token, "recovery_email": rec_email,
            "recovery_password": rec_pw, "raw": s, "fields": len(parts)}


class OutlookMail:
    """一个邮箱的取件客户端（线程内使用；token 到期自动刷新）"""

    def __init__(self, email, client_id, refresh_token, password="",
                 recovery_email="", recovery_password="", raw="", mb_id=None,
                 proxy="", timeout=30):
        self.email = email
        self.client_id = client_id
        self.password = password
        self.recovery_email = recovery_email
        self.recovery_password = recovery_password
        self.raw = raw or "----".join([email, password, client_id, refresh_token])
        self.mb_id = mb_id
        self.proxy = proxy or ""
        self.timeout = timeout
        self._refresh_token = refresh_token
        self._access = ""
        self._expires_at = 0.0

    # ---------------------------------------------------------------- token
    def _proxies(self):
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    def _get_token(self, force=False):
        if not force and self._access and time.time() < self._expires_at:
            return self._access
        try:
            r = requests.post(TOKEN_URL, data={
                "grant_type": "refresh_token", "client_id": self.client_id,
                "refresh_token": self._refresh_token, "scope": SCOPE,
            }, proxies=self._proxies(), timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            raise NetworkError(f"取 token 失败：{type(e).__name__}: {str(e)[:120]}")
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code != 200 or not j.get("access_token"):
            code = j.get("error") or f"http_{r.status_code}"
            desc = j.get("error_description") or (r.text or "")[:120]
            raise AuthError(f"{code}: {str(desc)[:140]}")
        self._access = j["access_token"]
        try:
            exp = int(j.get("expires_in") or 3600)
        except Exception:
            exp = 3600
        self._expires_at = time.time() + exp - 300          # 提前 5 分钟当过期
        if j.get("refresh_token"):                          # 微软可能轮换 refresh_token
            self._refresh_token = j["refresh_token"]
        return self._access

    def account_info(self):
        """当前（可能已轮换的）凭据——存回池子/账号行时用"""
        return {"email": self.email, "client_id": self.client_id,
                "refresh_token": self._refresh_token, "expires_at": self._expires_at,
                "password": self.password, "recovery_email": self.recovery_email,
                "recovery_password": self.recovery_password}

    # ---------------------------------------------------------------- Graph
    def _request(self, path, params=None):
        url = GRAPH + path
        for attempt in (1, 2):
            tok = self._get_token(force=(attempt == 2))
            try:
                r = requests.get(url, params=params, timeout=self.timeout,
                                 headers={"Authorization": f"Bearer {tok}",
                                          "Accept": "application/json"},
                                 proxies=self._proxies())
            except requests.exceptions.RequestException as e:
                raise NetworkError(f"{type(e).__name__}: {str(e)[:120]}")
            if r.status_code == 401 and attempt == 1:
                continue                                     # token 失效 → 强刷一次
            if r.status_code >= 400:
                try:
                    err = (r.json() or {}).get("error") or {}
                except Exception:
                    err = {}
                raise ApiError(err.get("code") or f"http_{r.status_code}",
                               str(err.get("message") or (r.text or ""))[:150])
            try:
                return r.json()
            except Exception:
                raise ApiError("bad_json", (r.text or "")[:150])
        raise AuthError("401（强刷后仍失败）")

    def list_messages(self, top=10, since_ts=None, unread=False, folder=None):
        """列最近邮件 → [{id, subject, from_address, from_name, received_at, received_ts, preview}]"""
        path = (f"/me/mailFolders/{urllib.parse.quote(folder)}/messages" if folder
                else "/me/messages")
        params = {"$select": LIST_SELECT, "$top": max(1, min(int(top), 1000)),
                  "$orderby": "receivedDateTime desc"}
        flt = []
        if unread:
            flt.append("isRead eq false")
        if since_ts:
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since_ts))
            flt.append(f"receivedDateTime ge {iso}")
        if flt:
            params["$filter"] = " and ".join(flt)
        j = self._request(path, params)
        out = []
        for m in (j or {}).get("value") or []:
            fa = ((m.get("from") or {}).get("emailAddress") or {})
            out.append({
                "id": m.get("id"),
                "subject": m.get("subject") or "",
                "from_address": (fa.get("address") or "").lower(),
                "from_name": fa.get("name") or "",
                "received_at": m.get("receivedDateTime") or "",
                "received_ts": _iso_ts(m.get("receivedDateTime")),
                "preview": m.get("bodyPreview") or "",
            })
        return out

    def get_message(self, mid):
        """取一封邮件详情（含正文文本）→ {subject, from_address, received_at, received_ts, text, html}"""
        j = self._request(f"/me/messages/{urllib.parse.quote(str(mid))}",
                          {"$select": DETAIL_SELECT}) or {}
        body = j.get("body") or {}
        content = body.get("content") or ""
        is_html = str(body.get("contentType") or "").lower() == "html"
        fa = ((j.get("from") or {}).get("emailAddress") or {})
        return {
            "id": j.get("id"), "subject": j.get("subject") or "",
            "from_address": (fa.get("address") or "").lower(),
            "from_name": fa.get("name") or "",
            "received_at": j.get("receivedDateTime") or "",
            "received_ts": _iso_ts(j.get("receivedDateTime")),
            "html": content if is_html else "",
            "text": html_to_text(content) if is_html else content,
            "blob": json.dumps({"subject": j.get("subject") or "",
                                "text": html_to_text(content) if is_html else content,
                                "html": content if is_html else ""}, ensure_ascii=False),
        }

    def check(self):
        """连通性/token 自检：永不抛异常 → {ok, email, error, error_code, token_expires_at}"""
        out = {"ok": False, "email": self.email, "error": "", "error_code": None,
               "token_expires_at": 0}
        try:
            self._get_token()
            self._request("/me/messages", {"$top": 1, "$select": "id"})
            out["ok"] = True
            out["token_expires_at"] = int(self._expires_at)
        except AuthError as e:
            out["error"] = str(e)[:160]
            out["error_code"] = "auth"
        except ApiError as e:
            out["error"] = str(e)[:160]
            out["error_code"] = e.code
        except NetworkError as e:
            out["error"] = str(e)[:160]
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"[:160]
        return out


def _iso_ts(s):
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except Exception:
        return 0


def from_row(row, proxy="", timeout=30):
    """DB 里的 mailboxes 行 → OutlookMail"""
    return OutlookMail(row["email"], row["client_id"], row["refresh_token"],
                       password=row.get("password") or "",
                       recovery_email=row.get("recovery_email") or "",
                       recovery_password=row.get("recovery_password") or "",
                       raw=row.get("raw") or "", mb_id=row.get("id"),
                       proxy=proxy, timeout=timeout)


def from_line(raw, proxy="", timeout=30):
    p = parse_line(raw)
    return OutlookMail(p["email"], p["client_id"], p["refresh_token"], p["password"],
                       p["recovery_email"], p["recovery_password"], p["raw"],
                       proxy=proxy, timeout=timeout)
