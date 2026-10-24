#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_manager.py -- 按端口管理浏览器实例，按 URL 创建 / 切换标签页。

模型很简单：

    Browser  = 一个调试端口（一个浏览器进程）
    Tab      = 该浏览器下的一个标签页
    Element  = 标签页上的元素（元素操作统一从 Browser / Tab 出发）

    from cdp_flow import Browser

    # 确保 9223 端口有浏览器（没有就启动），并连上
    b = Browser.launch(port=9223, library="elements.json")
    b.element("main_page", "login_button").click()

    # 另开一个浏览器（另一个端口），互不干扰
    b2 = Browser.launch(port=9224, library="elements.json")
    b2.element("main_page", "login_button").click()

    # 标签页：按 URL 新建，创建后自动成为当前页
    tab = b.new_tab("https://example.com/orders")
    b.element("order_page", "btn_query").click()

    # 切换回以前打开的标签页
    b.switch(tab_other)

所有元素操作都挂在 Browser 上（操作它的当前标签页），这样"哪个浏览器、哪个标签页"
一目了然；需要显式指定时也可以用 `tab.element(...)`。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request

from .element_operator import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    CdpSession,
    Element,
    ElementLibrary,
    ElementOperatorError,
    OperationTimeout,
    exec_action,
)

__all__ = ["Browser", "Tab", "ElementGroup", "TabNotFound", "find_browser"]

log = logging.getLogger("element_operator.browser")

# Chromium 系浏览器常见安装位置（Edge 作为兜底）
BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# launch() 默认把 profile 放这里：按端口区分，天然支持"一端口一浏览器"
PROFILE_ROOT = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "ElementCapture")


class TabNotFound(ElementOperatorError):
    """标签页不存在（已关闭 / 标题 url 不匹配）。"""


class ElementGroup:
    """元素库里的一个分组，绑定在某个标签页上。

    元素操作本来要反复写「分组 + 元素名」，绑一次就只写元素名：

        g = tab.group("bili")
        g.element("input_bili").input_text("xxx")
        g["input_bili"].click()
        g.input_bili.click()        # 元素名当属性（与内置方法重名时改用 .element）
        "input_bili" in g
        g.names()                   # 该分组下的元素名
        g.find("input")             # 该分组内按名字模糊搜索

    视图是轻量的：只记「哪个标签页 + 哪个分组」，元素仍然是每次操作时重新定位。
    """

    def __init__(self, tab: "Tab", group: str):
        self.tab = tab
        self.name = str(group)

    def __repr__(self) -> str:
        return f"<ElementGroup [{self.name}] {len(self.names())} 个>"

    # ---------------- 取元素 ----------------
    def element(self, name: str) -> Element:
        return self.tab.element(self.name, str(name))

    get = element

    def __getitem__(self, name: str) -> Element:
        return self.element(name)

    def __getattr__(self, name: str) -> Element:
        # 只有正常属性找不到时才触发：g.input_bili 等价 g.element("input_bili")
        if name.startswith("_"):
            raise AttributeError(name)
        return self.element(name)

    def find(self, name: str) -> list:
        """该分组内按名字模糊搜索，返回 Element 列表。"""
        key = str(name)
        return [self.element(n) for n in self.names() if key in n]

    # ---------------- 分组信息 ----------------
    def names(self) -> list:
        """该分组下的元素名（元素库里没有这个分组时返回空列表）。"""
        return self.tab.browser.library.names(self.name)

    def __contains__(self, name) -> bool:
        return str(name) in self.names()

    def __iter__(self):
        return iter(self.names())

    def __len__(self) -> int:
        return len(self.names())


def find_browser() -> str:
    """找本机可用的 Chromium 系浏览器（先常见路径，再查注册表 App Paths）。"""
    for path in BROWSER_CANDIDATES:
        if path and os.path.exists(path):
            return path
    try:
        import winreg
    except Exception:
        return ""
    for exe in ("chrome.exe", "msedge.exe"):
        key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(root, key) as handle:
                    value = winreg.QueryValue(handle, None)
                if value and os.path.exists(value):
                    return value
            except Exception:
                continue
    return ""


def _http(host: str, port: int, path: str, method: str = "GET",
          timeout: float = 10.0):
    """访问 CDP 的 HTTP 端点（/json、/json/new、/json/close）。"""
    url = f"http://{host}:{port}{path}"
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw) if raw.strip() else None
    except Exception:
        return raw


def _port_alive(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        _http(host, port, "/json/version", timeout=timeout)
        return True
    except Exception:
        return False


def _url_matches(href: str, hint: str) -> bool:
    """宽松比较两个 URL（忽略大小写、#hash、尾部斜杠，允许重定向/追加参数）。

    大小写不敏感是为了 Windows 下的 file:// 路径（F: / f: 会被 Chrome 改成大写），
    对 http(s) 路径也基本不会误判。
    """
    a = str(href or "").split("#")[0].rstrip("/").lower()
    b = str(hint or "").split("#")[0].rstrip("/").lower()
    if not b:
        return True
    return a == b or a.startswith(b) or b.startswith(a)


# --------------------------------------------------------------------------- #
# 标签页
# --------------------------------------------------------------------------- #
class Tab:
    """一个标签页。元素操作：`tab.element(group, name)`。"""

    def __init__(self, browser: "Browser", target: dict):
        self.browser = browser
        self.target = dict(target or {})
        self.id = str(self.target.get("id") or "")
        self._session: CdpSession | None = None

    # ---------------- 基本信息 ----------------
    @property
    def url(self) -> str:
        return str(self.target.get("url") or "")

    @property
    def title(self) -> str:
        return str(self.target.get("title") or "")

    def __repr__(self) -> str:
        return f"<Tab {self.id[:8]} {self.title[:24]!r} {self.url[:48]!r}>"

    def refresh(self) -> "Tab":
        """从 /json 重新同步 url / title。"""
        self.browser._refresh_target(self)
        return self

    def is_alive(self) -> bool:
        return any(p.get("id") == self.id for p in self.browser.pages())

    # ---------------- 会话 ----------------
    @property
    def session(self) -> CdpSession:
        """该标签页的 CDP 连接（懒建立；页面关闭后会自动重连失败并报错）。"""
        if self._session is None or self._session.chrome is None:
            pages = self.browser.pages()
            match = next((p for p in pages if p.get("id") == self.id), None)
            if match is None:
                raise TabNotFound(f"标签页已关闭：{self.title or self.id}")
            session = CdpSession(self.browser.host, self.browser.port)
            session.connect(match)
            self._session = session
            self.target = dict(match)
        return self._session

    def _exec(self, definition: dict, action: str, **params) -> dict:
        """供 Element 调用：在该标签页里定位并执行动作。"""
        return exec_action(self.session, definition, action, **params)

    def close_session(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
        self._session = None

    # ---------------- 导航 ----------------
    def goto(self, url: str, timeout: float = 30.0, wait_load: bool = True) -> "Tab":
        """导航到指定 URL（默认等页面加载完成）。"""
        url = str(url)
        self.session.call("Page.navigate", url=url)
        self.browser._refresh_target(self)
        if wait_load:
            self.wait_ready(timeout, url_hint=url)
        return self

    def reload(self, timeout: float = 30.0, wait_load: bool = True) -> "Tab":
        self.session.call("Page.reload")
        if wait_load:
            self.wait_ready(timeout, url_hint=self.url)
        return self

    def back(self, timeout: float = 30.0) -> "Tab":
        try:
            self.session.evaluate("history.back()")
        except Exception:
            pass
        self.wait_ready(timeout)
        return self

    def wait_ready(self, timeout: float = 30.0, url_hint: str | None = None,
                   interval: float = 0.2) -> bool:
        """等 document.readyState == 'complete'（可限定 URL）。

        导航过程中旧文档也会回应 evaluate，所以传了 url_hint 时会先核对 location.href，
        避免"刚发出导航就返回成功"。
        """
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                href = self.session.evaluate("location.href")
                state = self.session.evaluate("document.readyState")
                if state == "complete" and (url_hint is None or _url_matches(href, url_hint)):
                    self.browser._refresh_target(self)
                    return True
            except Exception:
                pass                                  # 导航中上下文会被销毁，正常
            if time.time() >= deadline:
                raise OperationTimeout(
                    f"标签页加载超时（{timeout}s）：{url_hint or self.url or self.title}")
            time.sleep(interval)

    # ---------------- 生命周期 ----------------
    def activate(self, front: bool = True) -> "Tab":
        """切换为当前标签页；front=True 时同时把浏览器窗口提到前台。"""
        if front:
            try:
                self.session.call("Page.bringToFront")
            except Exception:
                pass
        self.browser._current = self
        self.browser._refresh_target(self)
        return self

    def close(self) -> None:
        self.browser.close_tab(self)

    # ---------------- 元素 ----------------
    def group(self, name: str) -> "ElementGroup":
        """绑定一个元素分组：之后只写元素名。"""
        return ElementGroup(self, name)

    def element(self, group: str, name: str | None = None) -> Element:
        """本标签页上的元素（参数同 ElementOperator.element）。"""
        if name is None:
            text = str(group)
            if "/" not in text:
                found = self.browser.library.find(text)
                if len(found) == 1:
                    return Element(self, found[0])
                if len(found) > 1:
                    raise ElementOperatorError(
                        f"元素名 {text!r} 在多个分组中存在，请用 element('分组', '名称')")
                raise ElementOperatorError(f"元素库里没有名为 {text!r} 的元素")
            group, _, name = text.partition("/")
        return Element(self, self.browser.library.require(str(group).strip(), str(name).strip()))

    def find(self, name: str) -> list:
        return [Element(self, item) for item in self.browser.library.find(name)]


# --------------------------------------------------------------------------- #
# 浏览器
# --------------------------------------------------------------------------- #
class Browser:
    """一个调试端口 = 一个浏览器实例。

    三种写法，按需要选：

        b = Browser.launch(port=9223, library="elements.json")    # 类方法：创建 + 启动 + 连接
        b = Browser(port=9223, library="elements.json").start()   # 实例方法：先建对象再启动
        b = Browser(port=9223).connect()                          # 只连接已经在跑的浏览器

    `start()` 是幂等的：重复调用不会重复启动进程。
    """

    def __init__(self, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST,
                 library: str | None = None):
        self.host = str(host or DEFAULT_HOST)
        self.port = int(port or DEFAULT_PORT)
        self.library = ElementLibrary(library) if library else ElementLibrary()
        self._tabs: dict[str, Tab] = {}
        self._current: Tab | None = None
        self._process: subprocess.Popen | None = None
        self._profile = ""

    def __repr__(self) -> str:
        cur = self._current
        return f"<Browser {self.host}:{self.port} tabs={len(self._tabs)} current={cur!r}>"

    # ---------------- 启动 / 连接 ----------------
    @classmethod
    def launch(cls, port: int = DEFAULT_PORT, url: str | None = None,
               library: str | None = None, host: str = DEFAULT_HOST,
               profile: str | None = None, browser_path: str | None = None,
               headless: bool = False, extra_args=(),
               window_size: tuple | None = None,
               timeout: float = 30.0) -> "Browser":
        """创建并按需启动 / 连接；等价于 `Browser(...).start(...)`。"""
        return cls(port=port, host=host, library=library).start(
            url=url, profile=profile, browser_path=browser_path,
            headless=headless, extra_args=extra_args,
            window_size=window_size, timeout=timeout)

    def start(self, url: str | None = None, profile: str | None = None,
              browser_path: str | None = None, headless: bool = False,
              extra_args=(), window_size: tuple | None = None,
              timeout: float = 30.0) -> "Browser":
        """确保这个端口有可用浏览器并连接；已经连上就直接返回（幂等）。

        - 端口上已有浏览器 → 复用（`url` 不为空且没有匹配的标签页时新开一个）
        - 端口上没有 → 启动新进程，再连接
        """
        # 已经启动 / 连接过：只补齐连接和目标标签页，不重复起进程
        if self._tabs or self._process is not None:
            if not self._tabs:
                self.connect()
            if url:
                tab = self.get_tab(url=url)
                if tab is not None:
                    self.switch(tab)
                else:
                    self.new_tab(url, activate=True, wait_load=True, timeout=timeout)
            return self

        if _port_alive(self.host, self.port):
            log.info("复用已有浏览器 %s:%s", self.host, self.port)
            self.connect()
            if url:
                self.new_tab(url, activate=True, wait_load=True, timeout=timeout)
            return self

        self._start_process(profile=profile, browser_path=browser_path,
                            headless=headless, extra_args=extra_args,
                            url=url, window_size=window_size)
        self._wait_port(timeout)
        # 优先按 url 找到启动时打开的那个标签页；还没加载完就退回第一个
        try:
            self.connect(page=url or None)
        except ElementOperatorError:
            self.connect()
        if url and self.current is not None:
            try:
                self.current.wait_ready(timeout, url_hint=url)
            except OperationTimeout:
                log.warning("启动后等待页面加载超时：%s", url)
        return self

    def _start_process(self, profile=None, browser_path=None, headless=False,
                       extra_args=(), url=None, window_size=None) -> None:
        path = browser_path or find_browser()
        if not path or not os.path.exists(path):
            raise ElementOperatorError(
                "找不到 Chrome / Edge，请用 browser_path= 指定可执行文件路径")

        profile = profile or os.path.join(PROFILE_ROOT, f"profile-{self.port}")
        os.makedirs(profile, exist_ok=True)

        args = [
            path,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",          # Chrome 111+ 必需，否则 WS 握手 403
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
        ]
        if headless:
            args.append("--headless=new")
        if window_size:
            args.append(f"--window-size={int(window_size[0])},{int(window_size[1])}")
        args.extend(str(a) for a in extra_args)
        args.append(url or "about:blank")

        log.info("启动浏览器：%s（端口 %s）", path, self.port)
        self._process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        self._profile = profile

    def _wait_port(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _port_alive(self.host, self.port):
                return
            if self._process is not None and self._process.poll() is not None:
                raise ElementOperatorError(
                    f"浏览器进程已退出（端口 {self.port} 未就绪）。"
                    f"该用户数据目录可能被另一个 Chrome 占用：{self._profile}")
            time.sleep(0.25)
        raise OperationTimeout(f"等待调试端口 {self.port} 超时")

    def connect(self, page=None) -> "Browser":
        """连接该端口上的页面；page 可为 None / 下标 / url 或 title 关键字。"""
        pages = self.pages()
        if not pages:
            raise ElementOperatorError(
                f"{self.host}:{self.port} 下没有可调试页面"
                f"（浏览器是否用 --remote-debugging-port={self.port} 启动？）")
        target = CdpSession._pick_page(pages, page)
        tab = self._tab_for(target)
        self._current = tab
        return self

    def _tab_for(self, target: dict) -> Tab:
        tid = str(target.get("id") or "")
        tab = self._tabs.get(tid)
        if tab is None:
            tab = Tab(self, target)
            self._tabs[tid] = tab
        else:
            tab.target = dict(target)
        return tab

    def _refresh_target(self, tab: Tab) -> None:
        try:
            pages = self.pages()
        except Exception:
            return
        for p in pages:
            if p.get("id") == tab.id:
                tab.target = dict(p)
                return

    # ---------------- 页面 / 标签页 ----------------
    def pages(self) -> list:
        """原始 target 列表（`/json` 里 type == page 的部分）。"""
        return CdpSession.list_pages(self.host, self.port)

    def tabs(self) -> list:
        """当前所有标签页（Tab 对象，保持缓存复用）。"""
        try:
            pages = self.pages()
        except Exception:
            pages = []
        alive = set()
        out: list[Tab] = []
        for p in pages:
            tid = str(p.get("id") or "")
            if not tid:
                continue
            alive.add(tid)
            out.append(self._tab_for(p))
        for tid in list(self._tabs):
            if tid not in alive:
                self._tabs.pop(tid, None)
        if self._current is not None and self._current.id not in alive:
            self._current = None
        return out

    @property
    def current(self) -> Tab | None:
        """当前标签页（未设置或已关闭时自动取第一个）。"""
        if self._current is None or not any(t.id == self._current.id for t in self.tabs()):
            tabs = self.tabs()
            self._current = tabs[0] if tabs else None
        return self._current

    @property
    def title(self) -> str:
        tab = self.current
        return tab.title if tab else ""

    @property
    def url(self) -> str:
        tab = self.current
        return tab.url if tab else ""

    def get_tab(self, title: str | None = None, url: str | None = None,
                index: int | None = None) -> Tab | None:
        """查找已有标签页（标题 / url 子串匹配，不区分大小写）。"""
        tabs = self.tabs()
        if index is not None:
            return tabs[index] if 0 <= int(index) < len(tabs) else None
        t_key = str(title or "").lower()
        u_key = str(url or "").lower()
        for tab in tabs:
            if t_key and t_key in tab.title.lower():
                return tab
            if u_key and u_key in tab.url.lower():
                return tab
        return None

    def switch(self, tab) -> Tab:
        """切换当前标签页。tab 可以是 Tab 对象 / 下标 / 标题或 url 关键字。"""
        if isinstance(tab, Tab):
            target = tab
            if not target.is_alive():
                raise TabNotFound(f"标签页已关闭：{target.title or target.id}")
        elif isinstance(tab, int):
            target = self.get_tab(index=tab)
        else:
            target = self.get_tab(title=tab) or self.get_tab(url=tab)
        if target is None:
            titles = "；".join(f"{t.title or t.url}"[:40] for t in self.tabs())
            raise TabNotFound(f"没有匹配 {tab!r} 的标签页。现有：{titles}")
        return target.activate()

    activate = switch

    def new_tab(self, url: str = "about:blank", activate: bool = True,
                wait_load: bool = True, timeout: float = 30.0) -> Tab:
        """按 URL 新建标签页（Chrome 会把它放到前台）。"""
        url = str(url or "about:blank")
        target = self._http_new_tab(url)
        tab = self._tab_for(target)
        if wait_load and url not in ("about:blank", "chrome://newtab/"):
            try:
                tab.wait_ready(timeout, url_hint=url)
            except OperationTimeout:
                log.warning("新标签页加载超时：%s", url)
        if activate:
            tab.activate(front=False)             # /json/new 已经在前台，不必再抢焦点
        return tab

    def _http_new_tab(self, url: str) -> dict:
        """用 HTTP 端点建标签页（Chrome 111+ 要求 PUT；旧版用 GET）。"""
        path = "/json/new?" + urllib.parse.quote(url, safe="")
        last_error = None
        for method in ("PUT", "GET"):
            try:
                data = _http(self.host, self.port, path, method=method)
                if isinstance(data, dict) and data.get("id"):
                    return data
                last_error = f"返回异常：{str(data)[:120]}"
            except Exception as exc:
                last_error = str(exc)
        raise ElementOperatorError(f"新建标签页失败（{url}）：{last_error}")

    def close_tab(self, tab) -> None:
        """关闭标签页（Tab 对象 / 下标 / 关键字）。"""
        if not isinstance(tab, Tab):
            found = self.get_tab(index=tab) if isinstance(tab, int) else (
                self.get_tab(title=tab) or self.get_tab(url=tab))
            if found is None:
                return
            tab = found
        try:
            _http(self.host, self.port, f"/json/close/{tab.id}", timeout=5)
        except Exception:
            pass
        tab.close_session()
        self._tabs.pop(tab.id, None)
        if self._current is tab:
            self._current = None

    # ---------------- 元素（当前标签页） ----------------
    def group(self, name: str) -> "ElementGroup":
        """当前标签页上的分组视图（等价 `browser.current.group(name)`）。"""
        tab = self.current
        if tab is None:
            raise ElementOperatorError(f"{self.host}:{self.port} 下没有可用标签页")
        return ElementGroup(tab, name)

    def element(self, group: str, name: str | None = None) -> Element:
        """当前标签页上的元素；跨标签页时先 `switch()` / `new_tab()`。"""
        if not self.library.items:
            raise ElementOperatorError(
                "还没有加载元素库：Browser(port=..., library='elements.json')")
        tab = self.current
        if tab is None:
            raise ElementOperatorError(f"{self.host}:{self.port} 下没有可用标签页")
        return tab.element(group, name)

    def find(self, name: str) -> list:
        tab = self.current
        if tab is None:
            return []
        return tab.find(name)

    # ---------------- 生命周期 ----------------
    def is_alive(self) -> bool:
        return _port_alive(self.host, self.port)

    def close(self, kill: bool = False) -> None:
        """断开所有标签页连接；kill=True 时同时结束本次启动的浏览器进程。"""
        for tab in list(self._tabs.values()):
            tab.close_session()
        self._tabs.clear()
        self._current = None
        if kill and self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def __enter__(self) -> "Browser":
        if not self._tabs:
            self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
