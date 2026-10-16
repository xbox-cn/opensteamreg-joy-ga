# -*- coding: utf-8 -*-
"""NoneCap 账号「全自动建号」：反检测浏览器过 Turnstile（做法照 xbox-cn/nonecap 的 keysys/reg_one.py）。

关键点（实测结论，写在那个仓库里）：
- 注册表单是 Next.js Server Action，必须带 Turnstile token → 只能靠真实浏览器过
- 用 Camoufox（反检测 Firefox）+ humanize，**必须有显示器**（headless 过不了）；Linux 上用 Xvfb
- 过完 Turnstile 提交后，后面的邮箱验证 / 登录 / 取 key 全走协议（见 postal.py / nonecap.py）

依赖（面板「安装浏览器依赖」按钮或手工执行）：
    pip install -U "camoufox[geoip]" && python -m camoufox fetch
    apt-get install -y xvfb        # Linux 无显示器时
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import time

BASE = "https://dashboard.nonecap.com"


def installed():
    try:
        return importlib.util.find_spec("camoufox") is not None
    except Exception:
        return False


_display_cache = None
_log_sink = print


def log_ok(msg):
    try:
        _log_sink(msg)
    except Exception:
        pass


def _xvfb_alive(num):
    """看 /tmp/.X<n>-lock 里的 PID 是否还活着（判断 Xvfb 是真在跑还是留了陈旧的锁/套接字）"""
    try:
        with open(f"/tmp/.X{num}-lock") as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    try:
        import os as _os
        _os.kill(pid, 0)
        return True
    except Exception:
        return False


def _clean_stale_display(num):
    """清掉死掉的 Xvfb 留下的锁与套接字（部署重启会把上个 Xvfb 一起杀掉）"""
    for p in (f"/tmp/.X{num}-lock", f"/tmp/.X11-unix/X{num}"):
        try:
            os.remove(p)
        except Exception:
            pass


def display_ready(force=False, allow_spawn=True):
    """Linux 且没有 DISPLAY → 起 Xvfb（Turnstile 需要真实窗口）。返回 (ok, 说明)。
    结果按进程缓存：同一个进程里只探测/启动一次（首屏接口会用，不能每次盲等）。"""
    global _display_cache
    if _display_cache is not None and not force:
        return _display_cache
    if sys.platform in ("win32", "darwin"):
        _display_cache = (True, "本机桌面")
        return _display_cache
    disp = os.environ.get("SFR_NC_DISPLAY") or os.environ.get("DISPLAY") or ":99"
    num = disp.lstrip(":")
    if os.environ.get("DISPLAY") and _xvfb_alive(num):
        _display_cache = (True, disp)            # 现有显示还活着，直接用
        return _display_cache
    if not shutil.which("Xvfb"):
        _display_cache = (False, "缺少 Xvfb：apt-get install -y xvfb")
        return _display_cache
    if not allow_spawn:                          # Web 端只负责报告，不抢显示号（真正用的是 worker）
        _display_cache = (True, "worker 建号时自动准备")
        return _display_cache
    # 逐个试 :99 :98 :97 :96 —— 两个服务都开了 PrivateTmp，X 的抽象套接字是全局的，
    # 对方占用的显示号本地看不到锁文件，所以只能"试一个、失败换下一个"。
    base = int(num)
    try:
        log = open("/tmp/xvfb.log", "ab", buffering=0)
    except Exception:
        log = subprocess.DEVNULL
    for n in range(base, base - 8, -1):
        d = str(n)
        if _xvfb_alive(d):                       # 本进程私有 tmp 里锁还在且活着 → 直接复用
            os.environ["DISPLAY"] = ":" + d
            _display_cache = (True, ":" + d)
            return _display_cache
        if os.path.exists(f"/tmp/.X{d}-lock"):
            _clean_stale_display(d)              # 陈旧锁（上次被重启杀掉留下的）
        try:
            proc = subprocess.Popen(["Xvfb", ":" + d, "-screen", "0", "1400x920x24"],
                                    stdout=log, stderr=log, cwd="/")
        except Exception as e:
            _display_cache = (False, f"Xvfb 启动失败：{type(e).__name__}: {e}")
            return _display_cache
        ok = False
        for _ in range(25):                      # 最多等 2.5s 出套接字
            if os.path.exists(f"/tmp/.X11-unix/X{d}"):
                ok = True
                break
            if proc.poll() is not None:          # 这个号被占用/起不来 → 换下一个
                break
            time.sleep(0.1)
        if ok:
            os.environ["DISPLAY"] = ":" + d
            log_ok(f"[浏览器] 虚拟显示已就绪：:{d}")
            _display_cache = (True, ":" + d)
            return _display_cache
        try:
            proc.kill()
        except Exception:
            pass
    _display_cache = (False, "Xvfb 起不来（/tmp/xvfb.log 有原因）")
    return _display_cache


def proxy_to_camoufox(proxy):
    """http://user:pass@host:port → Camoufox 需要的 {"server","username","password"}"""
    if not proxy:
        return None
    import urllib.parse
    u = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
    if not u.hostname:
        return None
    d = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 8080}"}
    if u.username:
        d["username"] = u.username
        d["password"] = u.password or ""
    return d


def _turnstile_token(page):
    return page.evaluate(
        '() => { const e = document.querySelector(\'input[name="cf-turnstile-response"],'
        'textarea[name="cf-turnstile-response"]\'); return e && e.value ? e.value : ""; }')


def signup(email, password, proxy=None, log=print, wait_submit=8, attempt=1):
    """浏览器注册一个 NoneCap 账号。返回 {ok, stage, detail}
    stage: import / display / launch / turnstile / submit / done
    attempt：启动失败时内部重试一次（清理陈旧 X 显示）"""
    if not installed():
        return {"ok": False, "stage": "import",
                "detail": "未安装 camoufox：点「安装浏览器依赖」或 pip install -U \"camoufox[geoip]\" && python -m camoufox fetch"}
    global _log_sink
    _log_sink = log
    ok_disp, info = display_ready(force=(attempt > 1))
    if not ok_disp:
        return {"ok": False, "stage": "display", "detail": info}
    try:
        from camoufox.sync_api import Camoufox
    except Exception as e:
        return {"ok": False, "stage": "import", "detail": f"{type(e).__name__}: {e}"}
    kw = dict(headless=False, humanize=True, locale="en-US", window=(1400, 920))
    pa = proxy_to_camoufox(proxy)
    if pa:
        kw["proxy"] = pa
    log(f"[浏览器] 启动 Camoufox（{('代理 ' + pa['server']) if pa else '直连'}，显示 {info}）…")
    br = None
    try:
        ctxm = Camoufox(**kw)
        br = ctxm.__enter__()
        ctx = getattr(br, "contexts", None)
        ctx = ctx[0] if ctx else br
        page = ctx.new_page() if hasattr(ctx, "new_page") else br.pages[0]
        log("[浏览器] 打开 /signup …")
        page.goto(BASE + "/signup", wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2500)
        page.fill("#email", email)
        page.fill("#password", password)
        try:
            page.check("input[name=terms]", timeout=8000)
        except Exception as e:
            log(f"[浏览器] 勾选条款跳过（{str(e)[:50]}）")
        tok = ""
        for i in range(25):                       # 最多 ~50s，Managed Challenge 通常几秒自动过
            tok = _turnstile_token(page)
            if tok:
                log(f"[浏览器] Turnstile 通过（约 {i*2}s，token {len(tok)} 字节）")
                break
            page.wait_for_timeout(2000)
        if not tok:                               # 兜底：点一下复选框再来一轮
            log("[浏览器] 首次未过，尝试点击 Turnstile 复选框…")
            for f in page.frames:
                if "challenges.cloudflare" in (f.url or ""):
                    try:
                        b = f.locator("body").bounding_box()
                        if b:
                            x, y = b["x"] + 20, b["y"] + b["height"] / 2
                            page.mouse.move(x - 40, y - 20)
                            page.wait_for_timeout(150)
                            page.mouse.move(x, y, steps=12)
                            page.mouse.click(x, y)
                    except Exception:
                        pass
            page.wait_for_timeout(8000)
            tok = _turnstile_token(page)
        if not tok:
            return {"ok": False, "stage": "turnstile",
                    "detail": "Turnstile 没通过：换个出口 IP/代理再试（数据中心 IP 常被拦）"}
        page.click("button[type=submit]", timeout=15000)
        page.wait_for_timeout(int(wait_submit * 1000))
        body = page.evaluate("() => document.body.innerText.slice(0, 400)").replace("\n", " | ")
        log(f"[浏览器] 提交后页面：{body[:180]}")
        low = body.lower()
        if "not permitted" in low:
            return {"ok": False, "stage": "blocked",
                    "detail": "该出口 IP 被 NoneCap 禁止注册（Sign-ups from your network are not permitted）"
                              "→ 到「代理池」配置出口，或在批量建号里选「跟随代理模式 / 自定义代理」再试"}
        # 成功标志：页面提示查收验证邮件（Next.js 会把文案嵌进 HTML，所以只认短语、别认裸词）
        if "check your email" in low or "verification link" in low:
            return {"ok": True, "stage": "done", "detail": body[:200]}
        if ("already" in low and ("registered" in low or "exist" in low)) or "已被注册" in body:
            return {"ok": False, "stage": "submit", "detail": f"邮箱已被注册过：{body[:120]}"}
        return {"ok": True, "stage": "done", "detail": body[:200]}
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:180]}"
        if "Failed to launch" in str(e) and attempt == 1:
            log("[浏览器] 启动失败，清理显示后重试一次…")
            display_ready(force=True)
            return signup(email, password, proxy=proxy, log=log, wait_submit=wait_submit,
                          attempt=2)
        return {"ok": False, "stage": "launch", "detail": msg}
    finally:
        try:
            if br:
                br.close()
        except Exception:
            pass
        try:                                     # 清掉本次实例留下的事件循环，避免下次同线程启动报错
            import asyncio as _aio
            _aio.set_event_loop(None)
        except Exception:
            pass


def install(log=print):
    """安装浏览器依赖：pip 包 + 浏览器内核 + Linux 系统库/Xvfb（尽力而为，逐条报错不中断）"""
    steps = [
        ([sys.executable, "-m", "pip", "install", "-U", "camoufox[geoip]"], "pip 安装 camoufox"),
        ([sys.executable, "-m", "camoufox", "fetch"], "下载 Camoufox 浏览器内核"),
    ]
    if sys.platform not in ("win32", "darwin"):
        steps += [
            (["bash", "-lc", "command -v Xvfb >/dev/null || "
                             "(apt-get update -qq && apt-get install -y -qq xvfb)"],
             "安装 Xvfb（虚拟显示器）"),
            (["bash", "-lc", f"{sys.executable} -m playwright install-deps firefox || true"],
             "安装 Firefox 系统库（playwright install-deps）"),
        ]
    ok_all = True
    for cmd, title in steps:
        log(f"→ {title} …")
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=2400)
            tail = (p.stdout or "")[-400:].strip() + (" | " + (p.stderr or "")[-400:].strip() if p.stderr else "")
            log(f"  {title}：退出码 {p.returncode}{(' | ' + tail[-300:]) if tail else ''}")
            if p.returncode != 0:
                ok_all = False
        except Exception as e:
            ok_all = False
            log(f"  {title} 失败：{type(e).__name__}: {str(e)[:150]}")
    return ok_all, "浏览器依赖已就绪" if ok_all else "有步骤失败（见日志）"
