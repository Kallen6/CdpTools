#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
element_capture_qt.py -- Qt 元素捕获器（CDP 原生拾取 + 层级/标签两级勾选 + 匹配计数）

元素记录格式（就是元素库 JSON 里存的结构）：
    node_path          从 html 到目标元素的完整链路（每层记 tag/attributes/index/sibling_count/innerText）
    checked_nodes      勾选"参与定位"的层索引
    node_match_rules   每层的属性匹配规则 {层索引: {属性名: [匹配方式, 值, 是否启用]}}

界面：
    顶部     连接栏（启动 Chrome / 连接 / 页面 / 开始拾取 / 截图拾取）
    左栏     元素库资源管理器 —— 分组像文件夹（可展开/折叠），组下是元素名；
             可新建/删除分组、重命名/删除元素、刷新（都直接写回 json）
    右栏     选中元素的详情 —— 层级勾选情况（✓/✗ + 启用的标签规则）
    编辑窗口 拾取后自动打开（双击左栏元素也可打开）：
             层级勾选 → 标签规则 → 统计匹配数量（同时在网页上标记全部匹配：
             橙色框 + 左上角序号，滚动跟随）→ “标记全部匹配 / 清除标记”
             → 验证操作（点击元素 / 读取文本 / 写入文本）→ 填名选组保存

交互：
    1. 点"打开 ..."选中要维护的 json（或"新建 ..."一个）；下次启动自动恢复该文件。
       "总览 ..."是独立的按分组查看窗口（非模态，自动同步）。
    2. 填端口（默认 9223）→ 连接 → 下拉框选页面
    3. 点"开始拾取"→ 在浏览器里移动鼠标（Chrome 原生高亮+信息浮层）→ 单击目标元素
       （这条依赖真实鼠标输入，要求 Chrome 窗口可见、有焦点）
    4. Chrome 窗口在屏幕外 / 被挡住 / 标签页在后台时，用"截图拾取"：
       截取当前视口显示成图，在图上单击即可捕获（命中走 DOM.getNodeForLocation，
       与页面可见性无关）；目标不在截图里就先"向下滚动"再"刷新截图"
    5. 弹出的编辑窗口里：左栏勾选参与定位的层，右栏勾选/编辑每层的标签规则；
       点"统计匹配数量"看命中几个，网页上会同时标出全部匹配（橙框 + 左上角序号，
       滚动/缩放跟随）；再点"标记全部匹配"可切换清除；
       用"点击元素 / 读取文本 / 写入文本"在页面上实测定位是否命中目标
    6. 填元素名、选好分组 → 保存：追加进当前元素库（同分组同名则更新），
       旧元素不丢；同一文件用多个分组区分不同标签页的元素

依赖：PyQt5、websocket-client
"""
from __future__ import annotations

import base64
import collections
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

import websocket
# 断开类异常：页面被关、浏览器退出、target 被替换时底层 recv/send 抛这些。
# 必须在上层识别出来，不能让它们冒到 Qt 槽函数里——PyQt5 对槽里的未捕获异常
# 是直接 abort 进程，不是打印一下就完事。
from websocket import (
    WebSocketConnectionClosedException,
    WebSocketException,
    WebSocketTimeoutException,
)

from PyQt5.QtCore import QEventLoop, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
    QMessageBox, QPushButton, QScrollArea, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

# 应用目录：源码运行时就是脚本目录；打包成 exe 后必须换成 exe 所在目录——
# onefile 的 __file__ 指向临时解压目录（退出即删），配置/用户数据写进去会丢。
HERE = (os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))

# Chromium 系浏览器的常见安装位置（Edge 同样支持 CDP，作为兜底）
BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_browser() -> str:
    """找可用的 Chromium 系浏览器：先常见安装路径，再查注册表 App Paths。"""
    for path in BROWSER_CANDIDATES:
        if path and os.path.exists(path):
            return path
    try:
        import winreg
    except Exception:
        return ""
    key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, key) as handle:
                value = winreg.QueryValue(handle, None)
            if value and os.path.exists(value):
                return value
        except Exception:
            continue
    return ""


# 机器上已有的"调试用 profile"目录。优先复用它们，因为里面通常带着
# 登录态、代理/证书配置、已装的扩展——换一个全新目录等于开一个干净浏览器，
# 会出现登录态丢失、甚至配置不同导致上不了网的情况。
LEGACY_PROFILE_DIRS = (
    r"D:\chrome_debug_temp",
    r"D:\chrome_debug",
    r"D:\chrome_temp",
    r"C:\chrome_debug_temp",
)

def writable_dir() -> str:
    """放"上次参数/用户数据"的可写目录。

    源码运行时就是脚本目录（行为不变）；打包成 exe 后 exe 可能在 Program Files
    这类只读位置，所以数据统一放到 %LOCALAPPDATA%\\ElementCapture。
    """
    if not getattr(sys, "frozen", False):
        return HERE
    base = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "ElementCapture")
    try:
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        return HERE


DATA_DIR = writable_dir()
LAUNCH_CONFIG_PATH = os.path.join(DATA_DIR, "_launch_config.json")


def default_profile_dir(port: int) -> str:
    """默认用户数据目录：优先复用已存在的调试 profile，否则按端口在数据目录下新建。"""
    for path in LEGACY_PROFILE_DIRS:
        if os.path.isdir(path):
            return path
    return os.path.join(DATA_DIR, "_user_data", str(port))


def load_launch_config() -> dict:
    """读取上次用过的启动参数，省得每次重填。"""
    try:
        with open(LAUNCH_CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_launch_config(data: dict) -> None:
    """写入启动配置。合并式更新：启动设置对话框只提交浏览器相关字段，
    不能把 library_path（当前元素库）这类其它键抹掉。"""
    merged = load_launch_config()
    merged.update(data)
    try:
        with open(LAUNCH_CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


# 注：等调试端口就绪用的是主窗口里的 _port_timer（QTimer 轮询），
# 不在这里做阻塞等待——浏览器冷启动 + 首次建 profile 可能要十几秒，
# 同步等待会把 UI 冻住。

# --------------------------------------------------------------------------- #
# 拾取模式高亮配置（配色沿用 DevTools）
# --------------------------------------------------------------------------- #
HIGHLIGHT_CONFIG = {
    "contentColor": {"r": 111, "g": 168, "b": 220, "a": 0.66},
    "paddingColor": {"r": 147, "g": 196, "b": 125, "a": 0.55},
    "borderColor": {"r": 255, "g": 229, "b": 153, "a": 0.66},
    "marginColor": {"r": 246, "g": 178, "b": 107, "a": 0.66},
    "showInfo": True,
    "showRulers": False,
}

MATCH_TYPES = ("等于", "包含", "正则")

# 拾取轮询间隔（ms）。这里不追求高频——高亮浮层由 Chrome 自己绘制，
# 轮询只负责接住"点击"事件，300ms 的点击响应完全够用，
# 反而能减少对浏览器的干扰（重页面上鼠标追踪本身就不便宜）。
POLL_INTERVAL_MS = 300

# 单次轮询最多读取的 CDP 消息条数。绝对不能用 while True 读到底：
# 页面 DOM 抖动时事件流不断，UI 线程会被冻在这里，所以按条数封顶。
POLL_MAX_MESSAGES = 40

# poll_pick 的返回值约定：None = 本轮没事件，-1 = 用户按 ESC，PICK_CLOSED = 连接断了。
# 断线单独给一个值，是为了让 UI 能立刻退出"拾取中"状态，而不是一直空转定时器。
PICK_CLOSED = -2

# 断线统一提示语（CdpClosed 的 message）：UI 直接显示，不必各处拼字符串。
CLOSED_TIP = "浏览器连接已断开，请刷新页面列表后重新连接"

# 拾取相关事件（其余 CDP 事件一律丢弃，免得 DOM 变更事件把内存和轮询耗光）
PICK_EVENTS = ("Overlay.inspectNodeRequested", "Overlay.inspectModeCanceled")

# call() 等待响应期间收到的事件先缓存下来（上限够装几十次点击，多了丢最旧的）
EVENT_CACHE_MAX = 200

# 截图拾取的滚动步长（CSS 像素）
SHOT_SCROLL_STEP = 320

# CDP 命令超时（秒）。命令是同步等待响应的，等多久 UI 就冻多久，
# 所以这个值不能大：5 秒是"还能忍"和"系统标记未响应"的分界。
CDP_TIMEOUT = 5

# /json 请求超时（秒）。本地回环正常响应 < 50ms，0.8s 足够。
# 关键是别设大：Chrome 启动早期会出现"端口已监听但 HTTP 未就绪"的状态，
# 那时每次请求都会干等满超时——轮询定时器一叠加，界面就点不动了。
LIST_TIMEOUT = 0.8

# --------------------------------------------------------------------------- #
# 采集函数：Runtime.callFunctionOn 的 functionDeclaration，`this` = 被点中的元素
# 返回链路 + 每层的全部候选属性（供界面勾选），不做取舍
# --------------------------------------------------------------------------- #
CAPTURE_FN = r"""
function () {
    var el = this;

    var NOISE = /^data-v-[0-9a-f]+$|[0-9a-f]{8,}/i;
    var stable = function (v) {
        return !!v && v.split(/\s+/).every(function (c) { return c && !NOISE.test(c); });
    };
    // Shadow DOM 穿透：shadow 内节点 parentElement 为 null，需从 host 继续向上
    var parentOf = function (n) {
        if (n.parentElement) return n.parentElement;
        var p = n.parentNode;
        return (p && p.nodeType === 11 && p.host) ? p.host : null;
    };
    var kidsOf = function (n) {
        return Array.prototype.slice.call((n.parentNode && n.parentNode.children) || []);
    };
    // index 必须与匹配脚本里的 getIndex 完全一致（都用 parentElement.children），
    // 否则采集出的 index 值在实际定位时永远匹配不上
    var indexOf = function (n) {
        if (!n || !n.parentElement) return 0;
        var k = Array.prototype.slice.call(n.parentElement.children || []);
        var i = k.indexOf(n);
        return i < 0 ? 0 : i;
    };
    var attrsOf = function (n) {
        var o = {};
        for (var i = 0; i < n.attributes.length; i++) {
            var a = n.attributes[i];
            o[a.name] = (a.value || '').slice(0, 200);
        }
        return o;
    };
    // 稳定性打分：语义化文本 > 显式标识 > 结构信息；hash 类特征判 0（不可用）
    var score = function (k, v) {
        if (!v) return 0;
        if (k === 'aria-label' || k === 'placeholder') return 100;
        if (k === 'name' || k === 'title' || k === 'alt') return 90;
        if (k === 'innerText') return v.length <= 20 ? (/\d/.test(v) ? 55 : 80) : 40;
        if (k === 'id') return stable(v) ? 70 : 0;
        if (k === 'class') return stable(v) ? 60 : 0;
        if (k === 'role' || k === 'type') return 50;
        return 10;
    };

    try { el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (e) {}

    // root -> leaf，方向与定位端逐层收窄一致
    var chain = [];
    for (var n = el; n && n.nodeType === 1; n = parentOf(n)) chain.unshift(n);

    var ATTRS = ['aria-label', 'placeholder', 'name', 'title', 'alt', 'role', 'type', 'id', 'class'];

    var layers = chain.map(function (n, i) {
        var src = {}, k;
        for (var a = 0; a < ATTRS.length; a++) { k = ATTRS[a]; src[k] = n.getAttribute(k); }
        src.innerText = (n.innerText || '').trim();

        var best = 0;
        for (k in src) { if (src[k]) best = Math.max(best, score(k, src[k])); }

        var rules = [];
        for (k in src) {
            if (!src[k]) continue;                       // 空值不展示
            var s = score(k, src[k]);
            rules.push({ name: k, type: '等于', value: src[k], score: s,
                         recommended: s > 0 && s === best });
        }
        // 该层没有任何可用特征时，index 作为唯一可精确定位的兜底
        rules.push({ name: 'index', type: '等于', value: String(indexOf(n)), score: 0,
                     recommended: best === 0 });

        return {
            index: i,
            tag: n.tagName.toLowerCase(),
            text: (n.innerText || '').trim().slice(0, 40),
            id: n.getAttribute('id') || '',
            cls: (n.getAttribute('class') || '').slice(0, 60),
            rules: rules,
            best: best
        };
    });

    var xpathOf = function (e) {
        var segs = [];
        for (var m = e; m && m.nodeType === 1; m = parentOf(m)) {
            if (m.id && stable(m.id)) {
                return '//*[@id="' + m.id + '"]' + (segs.length ? '/' + segs.join('/') : '');
            }
            var i = 1;
            for (var s = m.previousElementSibling; s; s = s.previousElementSibling) {
                if (s.tagName === m.tagName) i++;
            }
            segs.unshift(m.tagName.toLowerCase() + '[' + i + ']');
        }
        return '/' + segs.join('/');
    };

    var r = el.getBoundingClientRect();
    return {
        tag_name: el.tagName.toLowerCase(),
        outer_html: (el.outerHTML || '').slice(0, 200000),
        xpath: xpathOf(el),
        attributes: attrsOf(el),
        node_path: chain.map(function (n) {
            return {
                tag: n.tagName.toLowerCase(),
                attributes: attrsOf(n),
                index: indexOf(n),
                sibling_count: kidsOf(n).length,
                innerText: (n.innerText || '').trim().slice(0, 300)
            };
        }),
        layers: layers,
        target_index: chain.length - 1,
        rect: { x: r.left, y: r.top, width: r.width, height: r.height },
        page_rect: {
            x: r.left + (window.scrollX || 0),
            y: r.top + (window.scrollY || 0),
            width: r.width, height: r.height
        }
    };
}
"""

# --------------------------------------------------------------------------- #
# 定位计数脚本：按 node_path 从根到叶逐层收窄，每层用启用的规则过滤
# payload = {"node_path": [...], "checked": [层索引...], "node_rules": {层索引: {属性: [方式, 值, 启用]}}}
# 返回匹配到的元素数量与前若干个样本（供列表展示与浏览器高亮）
# --------------------------------------------------------------------------- #
COUNT_FN_TMPL = r"""
(function () {
    try {
        var data = __PAYLOAD__;
        var nodePath = Array.isArray(data.node_path) ? data.node_path : [];
        var rawChecked = Array.isArray(data.checked) ? data.checked : [];
        var rulesByNode = data.node_rules || {};
        // '' / 'count' = 只统计；其余动作在定位到元素后就地执行（见下方分支）
        var action = (data.action || '').toString();
        // 清除标记不需要定位，放在最前面（定位失败提前 return 时也能正常清除）
        if (action === 'clear_marks') {
            try {
                var staleMarks = window.__elecap_marks__;
                if (staleMarks && staleMarks.destroy) staleMarks.destroy();
            } catch (e) {}
            return { ok: true, action: action, cleared: true };
        }

        function normalizeChecked(arr, n) {
            var seen = {}, out = [];
            for (var i = 0; i < arr.length; i++) {
                var v = arr[i];
                if (typeof v !== 'number' || v < 0 || v >= n || seen[v]) continue;
                seen[v] = true; out.push(v);
            }
            out.sort(function (a, b) { return a - b; });
            return out;
        }
        function matchValue(val, rule) {
            val = (val || '').toString();
            var t = (rule[0] || '等于').toString();
            var v = (rule[1] === undefined || rule[1] === null) ? '' : rule[1].toString();
            if (t === '等于') return val === v;
            if (t === '包含') return val.indexOf(v) >= 0;
            if (t === '正则') { try { return new RegExp(v).test(val); } catch (e) { return false; } }
            return false;
        }
        function activeRules(ruleMap) {
            var out = [];
            if (!ruleMap) return out;
            for (var k in ruleMap) {
                var r = ruleMap[k];
                if (!r || !r[2]) continue;               // [方式, 值, 是否启用]
                out.push({ name: k, type: r[0], value: r[1] });
            }
            return out;
        }
        function getIndex(el) {
            if (!el || !el.parentElement) return 0;
            var children = Array.prototype.slice.call(el.parentElement.children || []);
            var idx = children.indexOf(el);
            return idx < 0 ? 0 : idx;
        }
        function getAttrValue(el, name) {
            if (name === 'index') return getIndex(el).toString();
            if (name === 'innerText') return ((el.innerText || el.textContent || '') + '').trim();
            var val = '';
            try { val = el.getAttribute(name) || ''; } catch (e) {}
            return val;
        }
        function matchAttrs(el, ruleMap) {
            var rules = activeRules(ruleMap);
            for (var i = 0; i < rules.length; i++) {
                var r = rules[i];
                if (!matchValue(getAttrValue(el, r.name), [r.type, r.value])) return false;
            }
            return true;
        }
        // Shadow DOM 穿透，与采集端保持一致
        function traverseShadowRoots(root, tagName) {
            var out = [];
            var queryTag = (tagName || '').toLowerCase() || '*';
            function add(node) { if (node && node.nodeType === 1 && out.indexOf(node) < 0) out.push(node); }
            function collectWithin(r) {
                if (!r || !r.querySelectorAll) return;
                var nodes = [];
                try { nodes = Array.prototype.slice.call(r.querySelectorAll(queryTag)); } catch (e) { nodes = []; }
                for (var i = 0; i < nodes.length; i++) add(nodes[i]);
            }
            function collectAllShadow(r) {
                if (!r || !r.querySelectorAll) return;
                var hosts = [];
                try { hosts = Array.prototype.slice.call(r.querySelectorAll('*')); } catch (e) { hosts = []; }
                for (var i = 0; i < hosts.length; i++) {
                    var h = hosts[i];
                    try {
                        if (h && h.shadowRoot) { collectWithin(h.shadowRoot); collectAllShadow(h.shadowRoot); }
                    } catch (e) {}
                }
            }
            collectWithin(root);
            collectAllShadow(root);
            return out;
        }
        function collectCandidates(scope, tag) {
            var tagName = (tag || '').toLowerCase();
            if (!scope) return [];
            if (scope.nodeType === 9 || scope.nodeType === 11) return traverseShadowRoots(scope, tagName);
            if (scope.nodeType === 1) {
                var list = [];
                if (!tagName || (scope.tagName || '').toLowerCase() === tagName) list.push(scope);
                var within = traverseShadowRoots(scope, tagName);
                for (var i = 0; i < within.length; i++) if (list.indexOf(within[i]) < 0) list.push(within[i]);
                try {
                    if (scope.shadowRoot) {
                        var sub = traverseShadowRoots(scope.shadowRoot, tagName);
                        for (var j = 0; j < sub.length; j++) if (list.indexOf(sub[j]) < 0) list.push(sub[j]);
                    }
                } catch (e) {}
                return list;
            }
            return [];
        }
        function findMatches(scope, tag, ruleMap) {
            var cand = collectCandidates(scope, tag), out = [];
            for (var i = 0; i < cand.length; i++) {
                if (matchAttrs(cand[i], ruleMap)) out.push(cand[i]);
            }
            return out;
        }

        var steps = normalizeChecked(rawChecked, nodePath.length);
        if (!steps.length) return { ok: false, error: '没有勾选任何层' };

        var scopes = [document];
        var trace = [];
        for (var si = 0; si < steps.length; si++) {
            var idx = steps[si];
            var info = nodePath[idx] || {};
            var tag = (info.tag || '').toLowerCase();
            var ruleMap = rulesByNode[String(idx)] || {};
            var next = [];
            for (var sc = 0; sc < scopes.length; sc++) {
                var found = findMatches(scopes[sc], tag, ruleMap);
                for (var f = 0; f < found.length; f++) if (next.indexOf(found[f]) < 0) next.push(found[f]);
            }
            trace.push({ layer: idx, tag: tag, count: next.length });
            if (!next.length) {
                return { ok: true, count: 0, trace: trace, samples: [],
                         action: action, marked: 0 };
            }
            scopes = next;
        }

        var rectOf = function (e) {
            var r = e.getBoundingClientRect();
            return { x: r.left, y: r.top, width: r.width, height: r.height };
        };
        // 取前 50 个匹配位置（供需要坐标的场景使用）
        if (action === 'rects') {
            var rects = [];
            for (var ri = 0; ri < scopes.length && ri < 50; ri++) rects.push(rectOf(scopes[ri]));
            return { ok: true, count: scopes.length, action: action, rects: rects };
        }
        // 页面内自绘标记层：给全部匹配元素同时画框 + 左上角序号，滚动/缩放时实时跟随。
        // 不用 Overlay.highlightRect 的原因：一次只能画一个矩形（后画覆盖先画），
        // 且新版 Chrome 把它改成了 rect 参数，调用失败会被静默吞掉、界面上什么都看不到。
        if (action === 'marks') {
            try {
                var prevMarks = window.__elecap_marks__;
                if (prevMarks && prevMarks.destroy) prevMarks.destroy();
            } catch (e) {}
            // 兜底清理：万一上次的句柄丢了（如页面内跳转），按属性把残留覆盖层删掉
            var staleHosts = document.querySelectorAll('[data-elecap-marks]');
            for (var sh = 0; sh < staleHosts.length; sh++) {
                try { staleHosts[sh].parentNode.removeChild(staleHosts[sh]); } catch (e) {}
            }
            var limit = (typeof data.mark_limit === 'number' && data.mark_limit > 0)
                ? Math.min(data.mark_limit, 300) : 100;
            var host = document.createElement('div');
            host.setAttribute('data-elecap-marks', '1');
            host.style.cssText = 'position:fixed;left:0;top:0;right:0;bottom:0;'
                + 'pointer-events:none;z-index:2147483646;';
            (document.documentElement || document.body).appendChild(host);
            var boxes = [];
            for (var mk = 0; mk < scopes.length && mk < limit; mk++) {
                var box = document.createElement('div');
                box.style.cssText = 'position:fixed;box-sizing:border-box;pointer-events:none;'
                    + 'border:2px solid #ff5722;background:rgba(255,87,34,0.14);';
                var tag = document.createElement('div');
                tag.textContent = String(mk + 1);
                tag.style.cssText = 'position:absolute;left:-2px;min-width:18px;height:18px;'
                    + 'line-height:18px;padding:0 4px;background:#ff5722;color:#fff;'
                    + 'font:bold 12px/18px monospace;text-align:center;';
                box.appendChild(tag);
                host.appendChild(box);
                boxes.push({ box: box, tag: tag, el: scopes[mk] });
            }
            var painting = false;
            var paint = function () {
                painting = false;
                var vw = window.innerWidth || 0, vh = window.innerHeight || 0;
                for (var b = 0; b < boxes.length; b++) {
                    var it = boxes[b];
                    var el = it.el;
                    if (!el || !el.isConnected) { it.box.style.display = 'none'; continue; }
                    var r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.bottom < 0 || r.top > vh
                        || r.right < 0 || r.left > vw) {
                        it.box.style.display = 'none';      // 视口外不画，滚回来会自动出现
                        continue;
                    }
                    it.box.style.display = 'block';
                    it.box.style.left = r.left + 'px';
                    it.box.style.top = r.top + 'px';
                    it.box.style.width = Math.max(r.width, 2) + 'px';
                    it.box.style.height = Math.max(r.height, 2) + 'px';
                    // 元素贴视口顶部时，序号标签挪到框内，避免被裁掉
                    it.tag.style.top = (r.top >= 22 ? '-20px' : '0px');
                }
            };
            var schedule = function () {
                if (painting) return;
                painting = true;
                if (window.requestAnimationFrame) window.requestAnimationFrame(paint);
                else setTimeout(paint, 16);
            };
            var api = {
                repaint: schedule,
                focus: function (i) {
                    var it = boxes[i];
                    if (!it || !it.el) return false;
                    try { it.el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'smooth' }); }
                    catch (e) {
                        try { it.el.scrollIntoView(); } catch (e2) {}
                    }
                    try {
                        it.box.style.borderColor = '#1565c0';
                        it.box.style.background = 'rgba(21,101,192,0.25)';
                        it.tag.style.background = '#1565c0';
                        setTimeout(function () {
                            it.box.style.borderColor = '#ff5722';
                            it.box.style.background = 'rgba(255,87,34,0.14)';
                            it.tag.style.background = '#ff5722';
                        }, 1200);
                    } catch (e) {}
                    return true;
                },
                destroy: function () {
                    try {
                        window.removeEventListener('scroll', schedule, true);
                        window.removeEventListener('resize', schedule, true);
                    } catch (e) {}
                    try { if (host.parentNode) host.parentNode.removeChild(host); } catch (e) {}
                    if (window.__elecap_marks__ === api) window.__elecap_marks__ = null;
                }
            };
            window.__elecap_marks__ = api;
            window.addEventListener('scroll', schedule, true);
            window.addEventListener('resize', schedule, true);
            paint();
            return { ok: true, count: scopes.length, action: action, marked: boxes.length };
        }
        // 对首个匹配元素执行“点击 / 读取文本 / 写入文本”，用于验证定位是否为目标元素
        if (action === 'click' || action === 'text' || action === 'input') {
            var target = scopes[0];
            if (!target) return { ok: true, count: 0, action: action, found: false };
            try { target.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (e) {}
            var targetRect = rectOf(target);
            var targetText = ((target.innerText || target.textContent || '') + '').trim().slice(0, 4000);
            if (action === 'click') {
                try { target.click(); } catch (e) { return { ok: false, error: String(e) }; }
            } else if (action === 'input') {
                var iv = (data.input_text === undefined || data.input_text === null)
                    ? '' : String(data.input_text);
                try {
                    if (target.isContentEditable) { target.textContent = iv; }
                    else { target.value = iv; }
                    target.dispatchEvent(new Event('input', { bubbles: true }));
                    target.dispatchEvent(new Event('change', { bubbles: true }));
                } catch (e) { return { ok: false, error: String(e) }; }
            }
            var currentValue = '';
            try {
                currentValue = target.isContentEditable
                    ? ((target.textContent || '') + '').slice(0, 200)
                    : (('' + (target.value === undefined ? '' : target.value)) || '').slice(0, 200);
            } catch (e) {}
            return { ok: true, count: scopes.length, action: action, found: true,
                     tag: target.tagName.toLowerCase(), text: targetText,
                     value: currentValue, rect: targetRect };
        }

        var samples = [];
        for (var i = 0; i < scopes.length && i < 100; i++) {
            var e = scopes[i];
            var rr = e.getBoundingClientRect();
            samples.push({
                tag: e.tagName.toLowerCase(),
                text: ((e.innerText || '') + '').trim().replace(/\s+/g, ' ').slice(0, 60),
                x: rr.left, y: rr.top, width: rr.width, height: rr.height
            });
        }
        return { ok: true, count: scopes.length, trace: trace, samples: samples };
    } catch (e) {
        return { ok: false, error: String(e) };
    }
})()
"""


# --------------------------------------------------------------------------- #
# CDP 客户端（自实现命令收发，只依赖 websocket-client）
# --------------------------------------------------------------------------- #
class CdpClosed(RuntimeError):
    """CDP 会话已断开：页面被关、浏览器退出、target 被替换。

    拿它当"错误提示"用：UI 捕获后复位拾取状态并提示重连，不再往下抛。
    """


class CdpError(RuntimeError):
    """CDP 命令被协议层拒绝（响应里带 error）。

    典型例子：Overlay.enable 在 DOM.enable 之前调用，返回
    "DOM should be enabled first"。这类错误必须让用户看见，不能当成功。
    """


class CdpTimeout(RuntimeError):
    """命令在 CDP_TIMEOUT 内没等到响应（页面无响应 / 主线程被长任务占住）。"""


class CdpClient:
    """薄封装：连接目标页、发命令、拾取与采集。

    为什么不用 PyChromeDevTools 的命令收发（1.0.4）：
      1. 每条命令前先 pop_messages()，把已到达的事件（含 Overlay 的点击事件）直接丢掉；
      2. wait_result() 只认 result 不认 error，命令失败时匹配不到响应，会对着
         ws.recv() 死等到超时——每失败一次界面就冻结 CDP_TIMEOUT 秒；
      3. connect_targetID() 的异常回退引用了从没初始化过的 self.tabs。
    这里自己收发：id 匹配就返回，error 立刻抛出，其余事件收进 _events 交给 poll_pick。
    只在 Qt 主线程调用，不需要加锁。
    """

    def __init__(self) -> None:
        self.ws = None
        self.host = "127.0.0.1"
        self.port = 9223
        self.current: dict | None = None
        self._msg_id = 0
        self._events: collections.deque = collections.deque(maxlen=EVENT_CACHE_MAX)
        self._overlay_ready = False

    # ---------------- 连接 ----------------
    @staticmethod
    def list_pages(host: str, port: int) -> list:
        with urllib.request.urlopen(f"http://{host}:{port}/json",
                                    timeout=LIST_TIMEOUT) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        return [t for t in raw if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]

    def connect(self, host: str, port: int, tab: dict) -> None:
        self.close()
        self.host, self.port, self.current = host, port, tab
        self._msg_id = 0
        self._events.clear()
        url = f"ws://{host}:{port}/devtools/page/{tab['id']}"
        try:
            ws = websocket.create_connection(url, timeout=CDP_TIMEOUT,
                                             suppress_origin=True)
        except Exception as exc:
            raise RuntimeError(f"无法连接该页面：{exc}") from exc
        ws.settimeout(CDP_TIMEOUT)
        self.ws = ws
        try:
            self._enable_overlay()
        except Exception:
            self.close()                    # 只连上一半的会话不要留着
            raise

    def close(self) -> None:
        ws, self.ws = self.ws, None
        self._overlay_ready = False
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _enable_overlay(self) -> None:
        """启用拾取所需的域——顺序不能反。

        Chrome 的 Overlay 域依赖 DOM agent：不先 DOM.enable，Overlay.enable 会直接
        返回 error "DOM should be enabled first"；Overlay 没真正启用时，
        setInspectMode 就算回 OK 也不会推送 inspectNodeRequested，表现就是
        "点了开始拾取没反应、鼠标还是普通光标"。
        DOM.enable 之后页面 DOM 变更会推事件，call / poll_pick 一律丢弃，不占内存。
        """
        self.call("DOM.enable")
        self.call("Overlay.enable")
        self._overlay_ready = True
        self._drain_events()                # DOM.enable 会先灌一批初始节点事件

    # ---------------- 连接状态 ----------------
    def alive(self) -> bool:
        """连接是否还活着。

        页面关掉 / 浏览器退出后 ws 对象还在，只是已经断开；不能只看"有没有对象"。
        """
        ws = self.ws
        if ws is None:
            return False
        # 老版本 websocket-client 没有 connected 属性，缺省按"还活着"处理
        return bool(getattr(ws, "connected", True))

    def require_alive(self) -> None:
        """死了就抛 CdpClosed，把"连接已断开"变成一句能展示给人看的话。"""
        if not self.alive():
            self.close()
            raise CdpClosed(CLOSED_TIP)

    # ---------------- 命令 ----------------
    def call(self, cmd: str, **kwargs):
        """发一条 CDP 命令，返回响应里的 result。

        三种失败各自可辨：CdpClosed（连接断了）、CdpError（命令被拒）、
        CdpTimeout（超时）。错误必须抛出去——上一版把 error 响应当成功，
        结果 Overlay 根本没启用，界面却显示"请在浏览器中点击"。
        """
        ws = self.ws
        if ws is None:
            return None
        if not getattr(ws, "connected", True):
            self.close()
            raise CdpClosed(CLOSED_TIP)

        self._msg_id += 1
        mid = self._msg_id
        payload = json.dumps({"id": mid, "method": cmd, "params": kwargs},
                             ensure_ascii=False)
        try:
            ws.settimeout(CDP_TIMEOUT)      # poll_pick 会临时改成 0，这里恢复
            ws.send(payload)
        except (WebSocketException, OSError) as exc:
            self.close()
            raise CdpClosed(CLOSED_TIP) from exc

        deadline = time.time() + CDP_TIMEOUT
        while True:
            if time.time() >= deadline:
                raise CdpTimeout(
                    f"{cmd} 超时（{CDP_TIMEOUT}s）：页面可能正忙，稍后重试")
            try:
                raw = ws.recv()
            except WebSocketTimeoutException:
                continue                    # 循环开头判总超时，这里不再等
            except (WebSocketException, OSError) as exc:
                self.close()
                raise CdpClosed(CLOSED_TIP) from exc
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == mid:
                error = msg.get("error")
                if error:
                    text = error.get("message") if isinstance(error, dict) else str(error)
                    raise CdpError(f"{cmd} 失败：{text}")
                return msg.get("result", {})
            # 别的消息只是事件：拾取事件留着（用户可能在这期间点了页面），
            # 其余（DOM 变更、控制台等）一律丢，攒着只会吃内存
            if msg.get("method") in PICK_EVENTS:
                self._events.append(msg)

    def evaluate(self, expression: str, await_promise: bool = False):
        """执行 JS 并按值取回结果。"""
        res = self.call("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=await_promise)
        if not res or "exceptionDetails" in res:
            return None
        inner = res.get("result", {})
        if isinstance(inner, dict) and "value" in inner:
            return inner.get("value")
        return None

    def _drain_events(self, max_messages: int = 500) -> None:
        """丢掉积压在 ws 里的事件。

        DOM 域启用后，页面每次节点变更都会推事件；不拾取的时候没人读，
        攒多了会让下一条命令的等待被拖慢。只在进出拾取时按条数清一次，
        不写成 while True——页面 DOM 抖动时那会变成新的卡死点。
        """
        ws = self.ws
        if ws is None:
            return
        try:
            ws.settimeout(0)
            for _ in range(max_messages):
                try:
                    ws.recv()
                except Exception:
                    break
        finally:
            try:
                ws.settimeout(CDP_TIMEOUT)
            except Exception:
                pass

    # ---------------- 拾取 ----------------
    def activate(self) -> None:
        """把标签页提到前台。

        不做这一步的话：你刚点完 Qt 上的"开始拾取"，系统焦点还在 Qt 窗口上，
        Chrome 要等你在页面上再点一次，inspect 的鼠标追踪才真正生效。
        只 bringToFront，不再改窗口状态：把最大化的窗口还原会闪屏、触发整页重排，
        副作用比省下的那一次点击更大。
        """
        try:
            self.call("Page.bringToFront")
        except CdpClosed:
            raise
        except (CdpError, CdpTimeout):
            pass

    def start_pick(self) -> None:
        """进入拾取模式。失败抛 CdpClosed / CdpError / CdpTimeout，由 UI 显示原因。"""
        self.require_alive()
        if not self._overlay_ready:
            self._enable_overlay()          # 域没开时点了页面也不会有事件
        self.activate()                     # 先抢焦点再进拾取，省掉"额外点一次"
        self.call("Overlay.setInspectMode", mode="searchForNode",
                  highlightConfig=HIGHLIGHT_CONFIG)

    def stop_pick(self) -> None:
        """退出检查模式。连接已断时静默返回：没有会话可退，UI 那边会复位状态。"""
        try:
            self.call("Overlay.setInspectMode", mode="none",
                      highlightConfig=HIGHLIGHT_CONFIG)
        except (CdpClosed, CdpError, CdpTimeout):
            pass
        self._drain_events()                # 拾取期间的杂事件顺手清掉

    @staticmethod
    def _parse_pick_event(msg: dict):
        """把一条 CDP 事件翻译成拾取结果：backendNodeId / -1（ESC）/ None（无关事件）。"""
        method = msg.get("method")
        if method == "Overlay.inspectNodeRequested":
            node = (msg.get("params") or {}).get("backendNodeId")
            try:
                return int(node) if node else None
            except (TypeError, ValueError):
                return None
        if method == "Overlay.inspectModeCanceled":
            return -1
        return None

    def poll_pick(self, max_messages: int = POLL_MAX_MESSAGES):
        """非阻塞读一次拾取事件。

        返回 backendNodeId；无事件返回 None；ESC 返回 -1；连接断了返回 PICK_CLOSED。
        断线单独给个返回值，UI 才能立刻退出"拾取中"，而不是开着定时器空转。

        先消化 call() 期间攒下的事件：用户点得快时，点击事件是在某条命令往返
        途中到达的，那时它进的是 _events，不在 ws 缓冲里。
        """
        budget = max_messages
        while self._events and budget > 0:
            budget -= 1
            hit = self._parse_pick_event(self._events.popleft())
            if hit is not None:
                return hit

        ws = self.ws
        if ws is None or budget <= 0:
            return None
        found = None
        closed = False
        try:
            ws.settimeout(0)                    # 非阻塞：没数据时 recv 立刻抛超时
            for _ in range(budget):
                try:
                    raw = ws.recv()
                except WebSocketConnectionClosedException:
                    closed = True               # 页面/浏览器在这期间没了
                    break
                except WebSocketException:
                    break                       # 含 recv 超时（说明没数据）
                except Exception:
                    break
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                hit = self._parse_pick_event(msg)
                if hit is not None:
                    found = hit
                    break
                # 其余事件（DOM 变更等）直接丢弃，不攒
        except Exception:
            found = None
        finally:
            try:
                ws.settimeout(CDP_TIMEOUT)      # 不恢复的话后续命令会立刻超时
            except Exception:
                pass                            # 死 socket 上 settimeout 会抛，忽略
        if closed:
            self.close()
            return PICK_CLOSED
        return found

    # ---------------- 屏幕外/隐藏窗口拾取 ----------------
    # Overlay.setInspectMode 的命中只认真实鼠标输入（CDP 合成的鼠标事件会被忽略），
    # 页面 hidden（窗口在屏幕外、被挡住、标签页在后台）时更是什么都收不到。
    # 下面两个接口是纯几何/合成路径，不要求窗口可见，也不要求页面拿到焦点。
    def node_at(self, x: float, y: float):
        """按视口 CSS 坐标做命中测试，返回 backendNodeId（没命中返回 None）。

        注意 x/y 必须是整数：协议里 getNodeForLocation 的参数就是 integer，
        传小数会被直接拒掉（返回 error），表现成"点了却没反应"。
        """
        res = self.call("DOM.getNodeForLocation",
                        x=int(round(float(x))), y=int(round(float(y))))
        node = res.get("backendNodeId") if isinstance(res, dict) else None
        try:
            node = int(node)
        except (TypeError, ValueError):
            return None
        return node or None

    def screenshot(self):
        """截取当前视口，返回 (png字节, 视口CSS宽, 视口CSS高, 设备像素比)；失败返回 None。"""
        res = self.call("Page.captureScreenshot", format="png")
        data = res.get("data") if isinstance(res, dict) else None
        if not data:
            return None
        meta = self.evaluate(
            "[window.innerWidth, window.innerHeight, window.devicePixelRatio || 1]")
        if not isinstance(meta, list) or len(meta) < 3:
            meta = [1280, 720, 1]
        return base64.b64decode(data), float(meta[0]), float(meta[1]), float(meta[2]) or 1.0

    # ---------------- 采集 ----------------
    def capture(self, backend_node_id: int):
        resolved = self.call("DOM.resolveNode", backendNodeId=int(backend_node_id))
        object_id = ((resolved or {}).get("object") or {}).get("objectId")
        if not object_id:
            return None
        result = self.call("Runtime.callFunctionOn", objectId=object_id,
                           functionDeclaration=CAPTURE_FN, returnByValue=True,
                           awaitPromise=False)
        value = ((result or {}).get("result") or {}).get("value")
        return value if isinstance(value, dict) else None

    def count_matches(self, payload: dict):
        # str.replace 是纯字面替换、不做正则解析，所以 JSON 里的 $ \ 等字符都是安全的
        script = COUNT_FN_TMPL.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
        return self.evaluate(script)

    def run_action(self, payload: dict, action: str, text: str = ""):
        """对首个匹配元素执行动作：click / text / input / rects。

        定位算法与 count_matches 同源（同一个脚本里的 action 分支），
        所以验证操作作用的就是界面上统计出来的那个元素。
        """
        data = dict(payload)
        data["action"] = action
        if action == "input":
            data["input_text"] = text
        script = COUNT_FN_TMPL.replace("__PAYLOAD__", json.dumps(data, ensure_ascii=False))
        return self.evaluate(script)

    def paint_marks(self, payload: dict, limit: int = 100):
        """在页面里给全部匹配元素同时画标记框（左上角带序号），滚动/缩放时跟随。

        实现为页面内自绘覆盖层，不依赖 Overlay.highlightRect
        （那个一次只能画一个矩形，且新版 Chrome 改了参数形态，失败还是静默的）。
        """
        data = dict(payload)
        data["action"] = "marks"
        data["mark_limit"] = int(limit)
        script = COUNT_FN_TMPL.replace("__PAYLOAD__", json.dumps(data, ensure_ascii=False))
        return self.evaluate(script)

    def clear_marks(self) -> None:
        """移除页面上的标记框（无论当前规则是否还能定位到元素）。"""
        script = COUNT_FN_TMPL.replace(
            "__PAYLOAD__", json.dumps({"action": "clear_marks"}, ensure_ascii=False))
        try:
            self.evaluate(script)
        except Exception:
            pass

    def focus_mark(self, index: int) -> None:
        """把第 index 个标记滚动到视口中间并短暂强调（1 基序号 - 1 = index）。"""
        try:
            self.evaluate(
                "(window.__elecap_marks__ && window.__elecap_marks__.focus)"
                f" ? window.__elecap_marks__.focus({int(index)}) : false")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 启动浏览器的参数对话框
# --------------------------------------------------------------------------- #
class ChromeLaunchDialog(QDialog):
    """以远程调试模式启动 Chromium 系浏览器的参数对话框。"""

    def __init__(self, parent=None, port: int = 9223):
        super().__init__(parent)
        self.setWindowTitle("启动浏览器（远程调试模式）")
        self.setMinimumWidth(680)

        form = QFormLayout(self)
        saved = load_launch_config()

        row = QHBoxLayout()
        self.ed_path = QLineEdit(saved.get("browser_path") or find_browser())
        self.ed_path.setPlaceholderText("chrome.exe / msedge.exe 的完整路径")
        row.addWidget(self.ed_path, 1)
        btn = QPushButton("浏览")
        btn.clicked.connect(self._pick_path)
        row.addWidget(btn)
        form.addRow("浏览器路径", self._wrap(row))

        row2 = QHBoxLayout()
        self.ed_profile = QLineEdit(saved.get("profile_dir") or default_profile_dir(port))
        row2.addWidget(self.ed_profile, 1)
        btn2 = QPushButton("浏览")
        btn2.clicked.connect(self._pick_profile)
        row2.addWidget(btn2)
        form.addRow("用户数据目录", self._wrap(row2))

        self.ed_port = QLineEdit(str(saved.get("port") or port))
        form.addRow("调试端口", self.ed_port)

        self.ed_args = QLineEdit(saved.get("extra_args", ""))
        self.ed_args.setPlaceholderText("可选，例如 --window-size=1500,900")
        form.addRow("附加参数", self.ed_args)

        self.lbl_cmd = QLineEdit()
        self.lbl_cmd.setReadOnly(True)
        font = QFont("Consolas")
        font.setPointSize(8)
        self.lbl_cmd.setFont(font)
        form.addRow("将执行命令", self.lbl_cmd)

        hint = QLabel(
            "说明：\n"
            "  · 用户数据目录最关键。换成全新目录 = 开一个干净浏览器：登录态、代理/证书配置、\n"
            "    已装扩展全都没有，可能出现打不开网页的情况。建议沿用你平时调试用的那个目录。\n"
            "  · 该目录若已被另一个浏览器实例占用，新进程会把参数转交给已有实例然后自己退出，\n"
            "    调试端口不会打开——这种情况请先完全退出 Chrome。\n"
            "  · --remote-allow-origins=* 会自动带上，缺了它 WebSocket 握手会返回 403。")
        hint.setWordWrap(True)
        form.addRow(hint)

        for widget in (self.ed_path, self.ed_profile, self.ed_port, self.ed_args):
            widget.textChanged.connect(self._update_preview)
        self._update_preview()

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("启动")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self._on_ok)
        box.rejected.connect(self.reject)
        form.addRow(box)

    def _update_preview(self) -> None:
        """实时显示将要执行的命令行，方便和自己惯用的 bat 对照。"""
        self.lbl_cmd.setText(subprocess.list2cmdline(self.build_command()))
        self.lbl_cmd.setCursorPosition(0)

    def _on_ok(self) -> None:
        save_launch_config({
            "browser_path": self.ed_path.text().strip(),
            "profile_dir": self.ed_profile.text().strip(),
            "port": self.ed_port.text().strip(),
            "extra_args": self.ed_args.text().strip(),
        })
        self.accept()

    @staticmethod
    def _wrap(layout) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        layout.setContentsMargins(0, 0, 0, 0)
        return holder

    def _pick_path(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择浏览器可执行文件", "",
                                              "可执行文件 (*.exe)")
        if path:
            self.ed_path.setText(path)

    def _pick_profile(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择用户数据目录",
                                                self.ed_profile.text())
        if path:
            self.ed_profile.setText(path)

    def build_command(self) -> list:
        args = [
            self.ed_path.text().strip(),
            f"--remote-debugging-port={self.ed_port.text().strip()}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        profile = self.ed_profile.text().strip()
        if profile:
            args.append(f"--user-data-dir={profile}")
        args.extend(self.ed_args.text().split())
        return args


# --------------------------------------------------------------------------- #
# 元素库查看器（按分组展示已有元素）
# --------------------------------------------------------------------------- #
class LibraryViewerDialog(QDialog):
    """非模态查看窗口：按分组列出当前元素库里的已有元素，便于维护时对照。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.path = ""
        self.resize(900, 540)
        self.setWindowTitle("元素库")

        v = QVBoxLayout(self)
        self.lbl_file = QLabel("—")
        bold = QFont()
        bold.setBold(True)
        self.lbl_file.setFont(bold)
        self.lbl_file.setWordWrap(True)
        v.addWidget(self.lbl_file)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["分组 / 元素", "标签", "xpath", "页面"])
        self.tree.setColumnWidth(0, 240)
        self.tree.setColumnWidth(1, 60)
        self.tree.setColumnWidth(2, 320)
        self.tree.setColumnWidth(3, 220)
        v.addWidget(self.tree, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        btn_reload = QPushButton("刷新")
        btn_reload.setToolTip("重新从磁盘读取（文件可能被本工具之外的操作改过）")
        btn_reload.clicked.connect(self.reload_from_disk)
        row.addWidget(btn_reload)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        row.addWidget(btn_close)
        v.addLayout(row)

    def load(self, path: str, items: list) -> None:
        """填充数据：分组为父节点（带数量），元素为子节点。"""
        self.path = path
        groups: dict = {}
        for e in items:
            groups.setdefault(str(e.get("group_name") or "main_page"), []).append(e)

        self.tree.clear()
        for gname in sorted(groups):
            els = groups[gname]
            top = QTreeWidgetItem([f"{gname}（{len(els)} 个）", "", "", ""])
            bold = top.font(0)
            bold.setBold(True)
            top.setFont(0, bold)
            top.setToolTip(0, f"分组 {gname}：{len(els)} 个元素")
            self.tree.addTopLevelItem(top)
            for e in els:
                name = str(e.get("name") or "")
                xpath = str(e.get("xpath") or "")
                page = str(e.get("target_page") or "")
                child = QTreeWidgetItem([name, str(e.get("tag_name") or ""), xpath, page])
                child.setToolTip(0, name)
                child.setToolTip(2, xpath)
                child.setToolTip(3, page)
                top.addChild(child)
            top.setExpanded(True)

        self.lbl_file.setText(f"{path} —— 共 {len(items)} 条元素 / {len(groups)} 个分组")
        self.setWindowTitle(f"元素库 — {os.path.basename(path)}")

    def reload_from_disk(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return
        if not isinstance(data, list):
            QMessageBox.critical(self, "读取失败", "元素库顶层不是数组")
            return
        self.load(self.path, data)


# --------------------------------------------------------------------------- #
# 元素编辑窗口（拾取后自动打开；双击库中元素也可打开）
# --------------------------------------------------------------------------- #
def info_from_item(item: dict) -> dict:
    """把元素库里的一条记录还原成编辑器需要的 info（node_path + 每层候选规则 + 已存勾选）。

    · 候选规则来自 node_path 的 attributes（默认 等于 方式），再被 node_match_rules 覆盖；
    · 规则带 "on" 字段 → _fill 直接按它恢复启用状态（区别于新捕获时的 recommended 逻辑）。
    """
    node_path = item.get("node_path") or []
    saved_rules = item.get("node_match_rules") or {}
    layers = []
    for i, node in enumerate(node_path):
        rules = []
        by_name = {}
        for k, v in (node.get("attributes") or {}).items():
            rule = {"name": k, "type": "等于", "value": str(v), "on": False}
            rules.append(rule)
            by_name[k] = rule
        for k, spec in (saved_rules.get(str(i)) or {}).items():
            if not isinstance(spec, (list, tuple)) or len(spec) < 3:
                continue
            rule = by_name.get(k)
            if rule is None:
                rule = {"name": k, "type": spec[0], "value": spec[1], "on": False}
                rules.append(rule)
                by_name[k] = rule
            rule["type"], rule["value"], rule["on"] = spec[0], spec[1], bool(spec[2])
        if "index" not in by_name:
            rules.append({"name": "index", "type": "等于",
                          "value": str(node.get("index", 0)), "on": False})
        layers.append({"index": i, "tag": node.get("tag", ""),
                       "text": str(node.get("innerText", ""))[:40], "rules": rules})
    checked = [int(x) for x in (item.get("checked_nodes") or [])
               if isinstance(x, (int, float))]
    return {
        "tag_name": item.get("tag_name", ""),
        "xpath": item.get("xpath", ""),
        "attributes": item.get("attributes") or {},
        "outer_html": item.get("outer_html", ""),
        "node_path": node_path,
        "layers": layers,
        "target_index": max(checked) if checked else max(0, len(node_path) - 1),
        "checked_nodes": checked,
        "target_page": item.get("target_page", ""),
        "timestamp": item.get("timestamp") or 0,
    }


class ElementEditorDialog(QDialog):
    """元素编辑窗口：层级勾选 → 标签规则 → 统计/高亮核对 → 验证操作 → 保存。

    数据源 self.info：新捕获来自 CDP 采集；编辑已有元素由 info_from_item 还原。
    UI 只是 layer_rules / layer_checked 的视图，切换层级不会丢编辑。
    """

    def __init__(self, main: "CaptureWindow") -> None:
        super().__init__(main)
        self.main = main
        self.info: dict | None = None
        self._source = "new"                 # "new" = 刚捕获；"saved" = 来自元素库
        self._old_ref: tuple | None = None   # 编辑已有元素时的原身份 (group, name)
        self._base_id = ""                   # 编辑已有元素时保留原 id
        self.layer_rules: dict = {}
        self.layer_checked: set = set()
        self.cur_layer: int | None = None
        self._loading = False
        self._last_samples: list = []

        self._marks_on = False               # 页面上当前是否已画着匹配标记

        self.setWindowTitle("元素编辑")
        self.resize(1120, 720)
        # 关闭即销毁：主窗 _editor 引用会在 closeEvent 里清掉，
        # 下次拾取/打开时重新建一个，避免积累隐藏窗口
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._build_ui()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        head.addWidget(QLabel("元素名"))
        self.ed_name = QLineEdit()
        self.ed_name.setMinimumWidth(240)
        head.addWidget(self.ed_name)
        head.addWidget(QLabel("分组"))
        self.cb_group = QComboBox()
        self.cb_group.setEditable(True)
        self.cb_group.setInsertPolicy(QComboBox.NoInsert)
        self.cb_group.setFixedWidth(170)
        self.cb_group.setToolTip("元素归入的分组（运行时用 find_group_name 过滤）；"
                                 "可下拉选已有分组，也可直接输入新分组名")
        self.cb_group.setEditText("main_page")
        head.addWidget(self.cb_group)
        self.lbl_source = QLabel("新捕获")
        self.lbl_source.setStyleSheet("color:#666;")
        head.addWidget(self.lbl_source)
        head.addStretch(1)
        root.addLayout(head)

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("元素层级（勾选 = 参与定位 → checked_nodes）"))
        self.tree_layers = QTreeWidget()
        self.tree_layers.setHeaderLabels(["层级", "index", "兄弟", "文本"])
        self.tree_layers.setColumnWidth(0, 150)
        self.tree_layers.setColumnWidth(1, 46)
        self.tree_layers.setColumnWidth(2, 46)
        self.tree_layers.itemChanged.connect(self.on_layer_item_changed)
        self.tree_layers.currentItemChanged.connect(self.on_layer_current_changed)
        lv.addWidget(self.tree_layers, 3)
        lv.addWidget(QLabel("匹配样本（点击一行 → 在浏览器里高亮）"))
        self.lst_hits = QListWidget()
        self.lst_hits.currentRowChanged.connect(self.on_hit_selected)
        lv.addWidget(self.lst_hits, 2)
        splitter.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        head2 = QHBoxLayout()
        self.lbl_layer = QLabel("点击左侧任一层级，这里显示它可用的标签")
        f = QFont()
        f.setBold(True)
        self.lbl_layer.setFont(f)
        head2.addWidget(self.lbl_layer)
        head2.addStretch(1)
        self.btn_all = QPushButton("全部取消")
        self.btn_all.clicked.connect(self.on_uncheck_all)
        head2.addWidget(self.btn_all)
        self.btn_reco = QPushButton("恢复推荐")
        self.btn_reco.clicked.connect(self.on_restore_recommend)
        head2.addWidget(self.btn_reco)
        rv.addLayout(head2)
        self.tree_rules = QTreeWidget()
        self.tree_rules.setHeaderLabels(["标签", "匹配方式", "值"])
        self.tree_rules.setColumnWidth(0, 140)
        self.tree_rules.setColumnWidth(1, 90)
        self.tree_rules.itemChanged.connect(self.on_rule_item_changed)
        self.tree_rules.itemDoubleClicked.connect(self.on_rule_double_clicked)
        rv.addWidget(self.tree_rules, 1)
        rv.addWidget(QLabel("提示：勾选框决定该标签是否参与匹配；"
                            "双击“匹配方式”循环切换 等于/包含/正则；双击“值”直接编辑"))
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        root.addWidget(splitter, 1)

        # 统计行
        row_count = QHBoxLayout()
        self.btn_count = QPushButton("统计匹配数量")
        self.btn_count.clicked.connect(self.on_count)
        row_count.addWidget(self.btn_count)
        self.btn_flash = QPushButton("标记全部匹配")
        self.btn_flash.setToolTip("在网页里同时标出全部匹配元素：橙色框 + 左上角序号（最多 100 个），"
                                  "滚动/缩放时跟随；再点一次清除")
        self.btn_flash.clicked.connect(self.on_flash_all)
        row_count.addWidget(self.btn_flash)
        self.lbl_result = QLabel("未统计")
        self.lbl_result.setStyleSheet("font-weight:bold;")
        row_count.addWidget(self.lbl_result)
        row_count.addStretch(1)
        root.addLayout(row_count)

        # 验证操作行
        row_act = QHBoxLayout()
        row_act.addWidget(QLabel("验证操作"))
        self.btn_click = QPushButton("点击元素")
        self.btn_click.setToolTip("对首个匹配元素执行 click()，看页面反应是否正确")
        self.btn_click.clicked.connect(self.on_click_element)
        row_act.addWidget(self.btn_click)
        self.btn_text = QPushButton("读取文本")
        self.btn_text.setToolTip("读取首个匹配元素的 innerText")
        self.btn_text.clicked.connect(self.on_read_text)
        row_act.addWidget(self.btn_text)
        self.ed_input = QLineEdit()
        self.ed_input.setPlaceholderText("要写入的文本")
        row_act.addWidget(self.ed_input, 1)
        self.btn_input = QPushButton("写入文本")
        self.btn_input.setToolTip("对首个匹配元素设置 value 并派发 input/change 事件"
                                  "（对输入框类元素有效）")
        self.btn_input.clicked.connect(self.on_write_text)
        row_act.addWidget(self.btn_input)
        root.addLayout(row_act)

        self.ed_action_result = QLineEdit()
        self.ed_action_result.setReadOnly(True)
        self.ed_action_result.setPlaceholderText("验证操作的结果会显示在这里")
        root.addWidget(self.ed_action_result)

        # 保存行
        row_save = QHBoxLayout()
        hint = QLabel("保存会写回主窗口打开的元素库文件（同分组同名则更新）")
        hint.setStyleSheet("color:#666;")
        row_save.addWidget(hint)
        row_save.addStretch(1)
        self.btn_save = QPushButton("保存到元素库")
        self.btn_save.clicked.connect(self.on_save)
        row_save.addWidget(self.btn_save)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        row_save.addWidget(btn_close)
        root.addLayout(row_save)

    # ---------------- 装载数据 ----------------
    def load_new(self, info: dict) -> None:
        """拾取到新元素：重建全部状态。分组保留上次选择，方便连续录同一页元素。"""
        self.info = info
        self._source = "new"
        self._old_ref = None
        self._base_id = ""
        self._fill(info)
        self._refresh_group_items()
        self._update_header()

    def load_saved(self, item: dict) -> None:
        """编辑元素库里已有的元素（双击库中元素）。"""
        info = info_from_item(item)
        self.info = info
        self._source = "saved"
        self._old_ref = (str(item.get("group_name") or "main_page"),
                         str(item.get("name") or ""))
        self._base_id = str(item.get("id") or "")
        self._fill(info, keep_name=True)
        self.ed_name.setText(self._old_ref[1])
        self._refresh_group_items(self._old_ref[0])
        self._update_header()

    def _refresh_group_items(self, current: str | None = None) -> None:
        """用主窗口库里的分组刷新下拉框。"""
        current = current or self.cb_group.currentText().strip() or "main_page"
        groups = self.main.group_names()
        self.cb_group.clear()
        self.cb_group.addItems(groups)
        if current not in groups:
            self.cb_group.insertItem(0, current)
        self.cb_group.setCurrentText(current)

    def _update_header(self) -> None:
        if self._source == "saved" and self._old_ref:
            self.lbl_source.setText(f"编辑已有元素：[{self._old_ref[0]}] {self._old_ref[1]}")
            self.setWindowTitle(f"元素编辑 — {self._old_ref[1]}")
        else:
            self.lbl_source.setText("新捕获")
            self.setWindowTitle("元素编辑 — 新捕获")
        self.lbl_source.setToolTip((self.info or {}).get("target_page") or "")

    def _fill(self, info: dict, keep_name: bool = False) -> None:
        # 1) 重建数据源
        self._loading = True
        try:
            layers = info.get("layers") or []
            self.layer_rules = {}
            for layer in layers:
                idx = int(layer.get("index"))
                rules = {}
                for rule in layer.get("rules") or []:
                    name = str(rule.get("name"))
                    if "on" in rule:
                        # 来自元素库：直接恢复保存时的启用状态
                        on = bool(rule.get("on"))
                    else:
                        # 新捕获：推荐项默认勾选；index 是纯结构兜底，不做默认
                        on = bool(rule.get("recommended")) and name != "index"
                    rules[name] = [rule.get("type", "等于"), str(rule.get("value", "")), on]
                self.layer_rules[idx] = rules

            saved_checked = info.get("checked_nodes")
            if saved_checked is not None:
                self.layer_checked = {int(x) for x in saved_checked}
            else:
                # 默认只勾两层：目标层（链路最底层）+ 紧邻的上一级
                target = int(info.get("target_index", len(layers) - 1))
                self.layer_checked = {target}
                if target - 1 >= 0:
                    self.layer_checked.add(target - 1)
            target = int(info.get("target_index", len(layers) - 1))

            # 2) 层级树
            self.tree_layers.clear()
            for i, node in enumerate(info.get("node_path") or []):
                label = f"{i} <{node.get('tag')}>"
                attrs = node.get("attributes") or {}
                if attrs.get("id"):
                    label += f"#{attrs['id']}"
                if attrs.get("class"):
                    label += f".{str(attrs['class']).split()[0]}"
                item = QTreeWidgetItem([label, str(node.get("index")),
                                        str(node.get("sibling_count")),
                                        str(node.get("innerText", ""))[:40]])
                item.setData(0, Qt.UserRole, i)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if i in self.layer_checked else Qt.Unchecked)
                item.setToolTip(0, f"层 {i}  <{node.get('tag')}>")
                layer_text = layers[i].get("text", "") if i < len(layers) else ""
                item.setToolTip(3, str(node.get("innerText", "")) or layer_text)
                if i == target:
                    font = QFont()
                    font.setBold(True)
                    item.setFont(0, font)
                    for c in range(4):
                        item.setForeground(c, QColor(200, 0, 0))
                self.tree_layers.addTopLevelItem(item)

            # 3) 展示目标层
            self._show_layer(target, select_row=target)

            self.lst_hits.clear()
            self._last_samples = []
            self.lbl_result.setText("未统计")
            self.lbl_result.setStyleSheet("font-weight:bold;")
            self.ed_action_result.clear()
            if not keep_name:
                self.ed_name.setText(self._auto_name(info))
        finally:
            self._loading = False

    def _show_layer(self, idx: int, select_row: int | None = None) -> None:
        """在右栏展示指定层级的可勾选标签。"""
        rules = self.layer_rules.get(idx) or {}
        self.cur_layer = idx
        self._loading = True
        try:
            self.tree_rules.clear()
            for name, spec in rules.items():
                mtype, value, enabled = spec[0], spec[1], spec[2]
                item = QTreeWidgetItem([name, mtype, value])
                item.setData(0, Qt.UserRole, name)
                item.setToolTip(1, "双击切换 等于 / 包含 / 正则")
                item.setToolTip(2, value)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEditable)
                item.setCheckState(0, Qt.Checked if enabled else Qt.Unchecked)
                if name == "index":
                    item.setForeground(0, QColor(150, 150, 150))     # index 仅兜底，灰显
                self.tree_rules.addTopLevelItem(item)
        finally:
            self._loading = False
        self._refresh_layer_label()

        if select_row is not None and 0 <= select_row < self.tree_layers.topLevelItemCount():
            self._loading = True
            try:
                self.tree_layers.setCurrentItem(self.tree_layers.topLevelItem(select_row))
            finally:
                self._loading = False

    def _refresh_layer_label(self) -> None:
        """右栏标题：显示当前层是谁、以及它有没有参与定位。"""
        idx = self.cur_layer
        if idx is None:
            self.lbl_layer.setText("点击左侧任一层级，这里显示它可用的标签")
            return
        tag = ""
        for layer in (self.info or {}).get("layers") or []:
            if int(layer.get("index", -1)) == idx:
                tag = f"<{layer.get('tag')}>"
                break
        in_use = "✓ 已参与定位" if idx in self.layer_checked else "✗ 未参与定位（左侧未勾选）"
        self.lbl_layer.setText(f"层 {idx} {tag} 的可勾选标签 — {in_use}")

    @staticmethod
    def _auto_name(info: dict) -> str:
        tag = info.get("tag_name") or "ele"
        prefix = {"button": "btn", "input": "input", "textarea": "input", "select": "select",
                  "a": "link", "table": "table", "img": "img"}.get(tag, "ele")
        for layer in reversed(info.get("layers") or []):
            for rule in layer.get("rules") or []:
                if rule.get("recommended") and rule.get("name") != "index":
                    txt = str(rule.get("value", ""))
                    txt = "".join(ch if (ch.isalnum() or ch in "_-" or "\u4e00" <= ch <= "\u9fff") else "_"
                                  for ch in txt).strip("_")[:24]
                    if txt:
                        return f"{prefix}_{txt}"
        return f"{prefix}_{tag}"

    # ---------------- 勾选联动 ----------------
    def on_layer_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """层级勾选变化 → checked_nodes。"""
        if self._loading or column != 0:
            return
        idx = item.data(0, Qt.UserRole)
        if idx is None:
            return
        if item.checkState(0) == Qt.Checked:
            self.layer_checked.add(int(idx))
        else:
            self.layer_checked.discard(int(idx))
        if self.cur_layer is not None:
            self._refresh_layer_label()

    def on_layer_current_changed(self, current: QTreeWidgetItem, _previous) -> None:
        """点击层级 → 右栏切换成该层的标签。"""
        if self._loading or current is None:
            return
        idx = current.data(0, Qt.UserRole)
        if idx is None:
            return
        self._show_layer(int(idx))

    def on_rule_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """标签的勾选/编辑变化 → 直接写回 layer_rules（切层也不会丢）。"""
        if self._loading or self.cur_layer is None:
            return
        name = item.data(0, Qt.UserRole)
        if name is None:
            return
        spec = self.layer_rules.get(self.cur_layer, {}).get(str(name))
        if spec is None:
            return
        if column == 0:
            spec[2] = item.checkState(0) == Qt.Checked
        elif column == 1:
            spec[0] = item.text(1)
        elif column == 2:
            spec[1] = item.text(2)

    def on_rule_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if self._loading or column != 1:
            return
        cur = item.text(1)
        nxt = MATCH_TYPES[(MATCH_TYPES.index(cur) + 1) % len(MATCH_TYPES)] \
            if cur in MATCH_TYPES else MATCH_TYPES[0]
        item.setText(1, nxt)

    def on_uncheck_all(self) -> None:
        self._loading = True
        try:
            for i in range(self.tree_layers.topLevelItemCount()):
                self.tree_layers.topLevelItem(i).setCheckState(0, Qt.Unchecked)
        finally:
            self._loading = False
        self.layer_checked.clear()
        if self.cur_layer is not None:
            self._show_layer(self.cur_layer)

    def on_restore_recommend(self) -> None:
        if not self.info:
            return
        if self._source == "saved":
            QMessageBox.information(
                self, "无法恢复推荐",
                "该元素来自元素库，只保存了勾选结果、没有采集时的推荐信息。\n"
                "可手动勾选/取消标签进行调整。")
            return
        self._fill(self.info, keep_name=True)

    # ---------------- 构造 payload ----------------
    def build_payload(self) -> dict | None:
        if not self.info:
            return None
        total = len(self.info.get("node_path") or [])
        checked, node_rules = [], {}
        for idx in sorted(self.layer_checked):
            if not (0 <= idx < total):
                continue
            # 勾选即参与定位：即使该层一个标签都没启用，也会按 tag 收窄
            checked.append(idx)
            node_rules[str(idx)] = {k: list(v)
                                    for k, v in (self.layer_rules.get(idx) or {}).items()}
        if not checked:
            return None
        return {
            "node_path": self.info.get("node_path") or [],
            "checked": checked,
            "node_rules": node_rules,
        }

    # ---------------- 统计 + 高亮核对 ----------------
    def on_count(self) -> None:
        payload = self.build_payload()
        if not payload:
            QMessageBox.information(self, "没有勾选层级", "请在左侧至少勾选一个层级。")
            return
        if not self.main._busy("正在统计匹配数量 ..."):
            return
        try:
            result = self.main.cdp.count_matches(payload)
        except (CdpClosed, CdpError, CdpTimeout) as exc:
            self.lbl_result.setText(str(exc))
            self.lbl_result.setStyleSheet("font-weight:bold; color:#c00;")
            return
        finally:
            self.main._unbusy()

        if not isinstance(result, dict):
            self.lbl_result.setText("统计失败（脚本未返回结果）")
            self.lbl_result.setStyleSheet("font-weight:bold; color:#c00;")
            return
        if not result.get("ok"):
            self.lbl_result.setText(f"统计失败：{result.get('error')}")
            self.lbl_result.setStyleSheet("font-weight:bold; color:#c00;")
            return

        count = int(result.get("count", 0))
        self.lbl_result.setText(f"匹配到 {count} 个元素" + ("（唯一 ✓）" if count == 1 else ""))
        color = "#0a0" if count == 1 else ("#c00" if count == 0 else "#c60")
        self.lbl_result.setStyleSheet(f"font-weight:bold; color:{color};")

        # 逐层收窄过程 + 匹配样本
        self.lst_hits.clear()
        for step in result.get("trace") or []:
            self.lst_hits.addItem(f"[收窄] 层 {step.get('layer')} <{step.get('tag')}> → 剩 {step.get('count')} 个")
        samples = result.get("samples") or []
        self._last_samples = samples
        for s in samples:
            self.lst_hits.addItem(
                f"<{s.get('tag')}>  ({int(s.get('x', 0))},{int(s.get('y', 0))})  {s.get('text', '')}")

        # 统计完直接在页面上标记全部匹配（带序号）——核对"是不是目标元素"就看这个
        if count > 0:
            self._paint_marks(quiet=True)
        else:
            self._clear_marks()

    def on_hit_selected(self, row: int) -> None:
        """点击样本列表时把对应标记滚到视口中间并强调（"[收窄]"行跳过）。"""
        if not self._last_samples:
            return
        trace_len = 0
        for i in range(self.lst_hits.count()):
            if self.lst_hits.item(i).text().startswith("[收窄]"):
                trace_len += 1
        idx = row - trace_len
        if 0 <= idx < len(self._last_samples):
            self.main.cdp.focus_mark(idx)      # 没有标记时该调用无副作用

    def on_flash_all(self) -> None:
        """标记 / 清除：在页面上同时标出全部匹配位置（左上角数字是序号）。"""
        if self._marks_on:
            self._clear_marks()
            self.ed_action_result.setText("已清除页面上的匹配标记")
            return
        self._paint_marks()

    def _paint_marks(self, quiet: bool = False) -> None:
        """在页面里给全部匹配元素画标记框 + 左上角序号（滚动/缩放时跟随）。"""
        payload = self.build_payload()
        if not payload:
            if not quiet:
                QMessageBox.information(self, "没有勾选层级", "请在左侧至少勾选一个层级。")
            return
        if not self.main._busy("正在标记匹配位置 ..."):
            return
        try:
            result = self.main.cdp.paint_marks(payload)
        except (CdpClosed, CdpError, CdpTimeout) as exc:
            self.ed_action_result.setText(str(exc))
            return
        finally:
            self.main._unbusy()
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") or "脚本未返回结果"
            if not quiet:
                self.ed_action_result.setText(f"标记失败：{err}")
            return
        count = int(result.get("count", 0))
        marked = int(result.get("marked", 0))
        self._marks_on = marked > 0
        self.btn_flash.setText("清除标记" if self._marks_on else "标记全部匹配")
        if marked <= 0:
            if not quiet:
                self.ed_action_result.setText("没有匹配到元素，无可标记位置")
            return
        extra = f"（共 {count} 个，只标记前 {marked} 个）" if count > marked else ""
        if not quiet:
            self.ed_action_result.setText(
                f"已标记 {marked} 个匹配位置，左上角数字是序号{extra}；页面滚动会自动跟随")

    def _clear_marks(self) -> None:
        self.main.cdp.clear_marks()
        self._marks_on = False
        self.btn_flash.setText("标记全部匹配")

    # ---------------- 验证操作（点击 / 读取文本 / 写入文本） ----------------
    def _run_action(self, action: str, text: str = "") -> dict | None:
        """执行一个基于当前规则的定位动作。失败时结果写进 ed_action_result。"""
        payload = self.build_payload()
        if not payload:
            QMessageBox.information(self, "没有勾选层级", "请在左侧至少勾选一个层级。")
            return None
        tip = {"click": "正在点击元素 ...", "text": "正在读取文本 ...",
               "input": "正在写入文本 ..."}.get(action, "执行中 ...")
        if not self.main._busy(tip):
            return None
        try:
            result = self.main.cdp.run_action(payload, action, text)
        except (CdpClosed, CdpError, CdpTimeout) as exc:
            self.ed_action_result.setText(str(exc))
            return None
        finally:
            self.main._unbusy()
        if not isinstance(result, dict) or not result.get("ok"):
            err = (result or {}).get("error") or "脚本未返回结果"
            self.ed_action_result.setText(f"操作失败：{err}")
            return None
        if action in ("click", "text", "input") and not result.get("found"):
            self.ed_action_result.setText("操作失败：当前规则没有匹配到任何元素")
            return None
        return result

    def on_click_element(self) -> None:
        res = self._run_action("click")
        if not res:
            return
        self.ed_action_result.setText(
            f"已点击 <{res.get('tag')}>：{str(res.get('text', ''))[:120]}")
        self.main.cdp.focus_mark(0)            # 把操作目标（第 1 个匹配）滚到视口并强调

    def on_read_text(self) -> None:
        res = self._run_action("text")
        if not res:
            return
        text = str(res.get("text", ""))
        self.ed_action_result.setText(text[:2000] + ("..." if len(text) > 2000 else ""))
        self.main.cdp.focus_mark(0)

    def on_write_text(self) -> None:
        text = self.ed_input.text()
        res = self._run_action("input", text)
        if not res:
            return
        self.ed_action_result.setText(
            f"已写入 {len(text)} 字符；元素当前值：{str(res.get('value', ''))[:200]}")
        self.main.cdp.focus_mark(0)

    # ---------------- 保存 ----------------
    def on_save(self) -> None:
        if not self.info:
            return
        if not self.main._lib_path:
            QMessageBox.information(
                self, "未打开元素库",
                "请先在主窗口点“打开元素库 ...”选择要维护的 json 文件。")
            return
        name = self.ed_name.text().strip()
        if not name:
            QMessageBox.warning(self, "缺少元素名", "请填写元素名")
            return
        group = self.cb_group.currentText().strip() or "main_page"
        payload = self.build_payload()
        if not payload:
            QMessageBox.information(self, "没有勾选层级", "请在左侧至少勾选一个层级。")
            return

        item = {
            "id": self._base_id or uuid.uuid4().hex[:8],
            "name": name,
            "target_page": self.info.get("target_page")
            or ((self.main.cdp.current or {}).get("url", "")),
            "tag_name": self.info.get("tag_name", ""),
            "outer_html": self.info.get("outer_html", ""),
            "screenshot_b64": "",
            "timestamp": self.info.get("timestamp") or time.time(),
            "group_name": group,
            "xpath": self.info.get("xpath", ""),
            "verify_mode": "default",
            "target_type": "page",
            "node_path": self.info.get("node_path") or [],
            "attributes": self.info.get("attributes") or {},
            "checked_nodes": payload["checked"],
            "node_match_rules": payload["node_rules"],
        }
        try:
            items = self.main.upsert_item(item, old=self._old_ref)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._old_ref = (group, name)          # 之后的再次保存基于新身份
        self._base_id = item["id"]
        self._source = "saved"
        self._update_header()
        self.main.statusBar().showMessage(
            f"已保存 [{group}] {name}（现共 {len(items)} 条）")
        QMessageBox.information(self, "保存成功",
                                f"[{group}] {name} 已写入\n{self.main._lib_path}\n\n"
                                f"checked_nodes = {payload['checked']}\n"
                                f"参与匹配的层 = {list(payload['node_rules'].keys())}")

    # ---------------- 关闭 ----------------
    def closeEvent(self, event) -> None:
        self.main.cdp.clear_marks()            # 顺手清掉页面上的标记，别留在网页里
        if self.main._editor is self:
            self.main._editor = None
        super().closeEvent(event)


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class _ShotLabel(QLabel):
    """截图控件：把显示坐标换算回截图原始像素坐标发出去。"""

    clicked = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scale = 1.0                       # 图像素 -> 屏幕像素
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setCursor(Qt.CrossCursor)

    def set_scale(self, scale: float) -> None:
        self._scale = max(0.01, float(scale))

    def _to_image(self, pos):
        s = self._scale or 1.0
        return pos.x() / s, pos.y() / s

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.pixmap() is not None:
            x, y = self._to_image(event.pos())
            self.clicked.emit(x, y)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        x, y = self._to_image(event.pos())
        self.setToolTip(f"截图坐标 ({x:.0f}, {y:.0f})")
        super().mouseMoveEvent(event)


class ShotPickDialog(QDialog):
    """截图拾取：Chrome 窗口在屏幕外、被挡住或标签页在后台时用它取元素。

    原生拾取（Overlay.setInspectMode + 真实鼠标）在页面 hidden 时收不到任何输入，
    而且 CDP 合成的鼠标事件本来就会被 inspect 命中忽略；这里改走
    "截图 → 在图上点选 → DOM.getNodeForLocation 按坐标命中"，两个接口都不要求
    窗口可见，也不要求页面有焦点。

    操作：刷新截图 → 在图上单击目标元素（目标不在截图里时先"向下滚动"再刷新）
    → 弹出的编辑窗口里继续勾选规则。
    """

    picked = pyqtSignal(dict)

    def __init__(self, parent=None, cdp: CdpClient | None = None):
        super().__init__(parent)
        self.setWindowTitle("截图拾取 — 窗口隐藏/屏幕外也能用")
        self.setMinimumSize(780, 560)
        self.cdp = cdp
        self._image: QImage | None = None
        self._css_w = 1.0
        self._css_h = 1.0
        self._dpr = 1.0

        v = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新截图")
        self.btn_refresh.setToolTip("重新截取当前视口（页面变化或滚动后点它）")
        self.btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(self.btn_refresh)

        self.btn_up = QPushButton("向上滚动")
        self.btn_up.clicked.connect(lambda: self.scroll_page(-SHOT_SCROLL_STEP))
        bar.addWidget(self.btn_up)
        self.btn_down = QPushButton("向下滚动")
        self.btn_down.clicked.connect(lambda: self.scroll_page(SHOT_SCROLL_STEP))
        bar.addWidget(self.btn_down)

        self.btn_fit = QPushButton("适应窗口")
        self.btn_fit.setCheckable(True)
        self.btn_fit.setChecked(True)
        self.btn_fit.setToolTip("勾选=整幅缩放到窗口内；取消=按原始像素 1:1 显示（配滚动条看细节）")
        self.btn_fit.toggled.connect(lambda _=False: self._apply_scale())
        bar.addWidget(self.btn_fit)

        bar.addStretch(1)
        self.lb_state = QLabel("")
        bar.addWidget(self.lb_state)
        v.addLayout(bar)

        self.area = QScrollArea(self)
        self.area.setWidgetResizable(False)
        self.area.setAlignment(Qt.AlignCenter)
        self.label = _ShotLabel()
        self.label.clicked.connect(self.on_click)
        self.area.setWidget(self.label)
        v.addWidget(self.area, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("在图上单击目标元素即可捕获。"), 1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        bottom.addWidget(btn_close)
        v.addLayout(bottom)

    # ---------------- 截图 ----------------
    def refresh(self) -> None:
        if self.cdp is None or not self.cdp.alive():
            self.lb_state.setText("未连接页面")
            return
        self.lb_state.setText("正在截图 ...")
        QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        try:
            shot = self.cdp.screenshot()
        except Exception as exc:
            self.lb_state.setText(f"截图失败：{exc}")
            return
        if not shot:
            self.lb_state.setText("截图失败（页面可能正在加载，稍后重试）")
            return
        raw, self._css_w, self._css_h, self._dpr = shot
        image = QImage.fromData(raw, "PNG")
        if image.isNull():
            self.lb_state.setText("截图解码失败")
            return
        self._image = image
        self._apply_scale()
        self.lb_state.setText(
            f"截图 {image.width()}×{image.height()}px / 视口 {self._css_w:.0f}×{self._css_h:.0f}")

    def scroll_page(self, delta: int) -> None:
        """滚动页面后重截：截图只有当前视口，页面下半部分得滚出来。"""
        if self.cdp is None or not self.cdp.alive():
            return
        try:
            self.cdp.evaluate(f"window.scrollBy(0, {int(delta)})")
        except Exception as exc:
            self.lb_state.setText(f"滚动失败：{exc}")
            return
        QTimer.singleShot(250, self.refresh)     # 等一帧渲染完再截，否则截到旧画面

    def _apply_scale(self) -> None:
        if self._image is None:
            return
        if self.btn_fit.isChecked():
            avail_w = max(160, self.area.viewport().width() - 4)
            avail_h = max(160, self.area.viewport().height() - 4)
            scale = min(avail_w / self._image.width(),
                        avail_h / self._image.height(), 1.0)
        else:
            scale = 1.0
        pix = QPixmap.fromImage(self._image)
        target = (max(1, int(round(pix.width() * scale))),
                  max(1, int(round(pix.height() * scale))))
        if target != (pix.width(), pix.height()):
            pix = pix.scaled(target[0], target[1], Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        self.label.set_scale(pix.width() / self._image.width())
        self.label.setPixmap(pix)
        self.label.resize(pix.size())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.btn_fit.isChecked():
            self._apply_scale()

    # ---------------- 点选 ----------------
    def on_click(self, img_x: float, img_y: float) -> None:
        """截图坐标 → 页面 CSS 坐标 → 命中元素。"""
        if self.cdp is None or not self.cdp.alive() or self._image is None:
            return
        # 截图是设备像素：宽 = 视口 CSS × dpr（dpr ≠ 1 的高分屏会放大）；
        # 万一版式和 dpr 对不上（浏览器缩放等），退回等比例的线性映射。
        if abs(self._image.width() - self._css_w * self._dpr) > 2:
            css_x = img_x * self._css_w / max(1, self._image.width())
            css_y = img_y * self._css_h / max(1, self._image.height())
        else:
            css_x = img_x / self._dpr
            css_y = img_y / self._dpr
        css_x = min(max(css_x, 0.0), max(self._css_w - 1.0, 0.0))
        css_y = min(max(css_y, 0.0), max(self._css_h - 1.0, 0.0))

        self.lb_state.setText(f"正在命中 ({css_x:.0f}, {css_y:.0f}) ...")
        QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        try:
            node = self.cdp.node_at(css_x, css_y)
        except CdpClosed as exc:
            self.lb_state.setText(str(exc))
            return
        except Exception as exc:
            self.lb_state.setText(f"命中失败：{exc}")
            return
        if not node:
            self.lb_state.setText("该位置没有元素（空白/滚动条），换一处再点")
            return
        try:
            info = self.cdp.capture(node)
        except CdpClosed as exc:
            self.lb_state.setText(str(exc))
            return
        except (CdpError, CdpTimeout) as exc:
            self.lb_state.setText(f"读取元素信息失败：{exc}")
            return
        if not info:
            self.lb_state.setText("读取元素信息失败，刷新截图后重试")
            return
        self.lb_state.setText(
            f"已捕获 <{info.get('tag_name')}> {(info.get('xpath') or '')[:60]}")
        self.picked.emit(info)


class CaptureWindow(QMainWindow):

    def __init__(self) -> None:
        super().__init__()
        self.cdp = CdpClient()
        self._lib_path: str = ""                      # 当前维护的元素库文件，保存直接写回它
        self._lib_items: list = []                    # 最近一次读入的库元素（资源管理器数据源）
        self._extra_groups: set = set()               # 会话内新建、文件里还没有元素的分组
        self._current_item: dict | None = None        # 详情面板当前展示的元素
        self._loading_explorer = False                # 重建资源管理器树时屏蔽选中事件
        self._viewer: "LibraryViewerDialog | None" = None   # 元素库总览窗口（复用一个）
        self._editor: "ElementEditorDialog | None" = None   # 元素编辑窗口（复用一个）
        self._shot: "ShotPickDialog | None" = None    # 截图拾取窗口（复用一个）
        self._busy_flag = False                       # 忙碌中，挡住重入操作

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_INTERVAL_MS)
        self.timer.timeout.connect(self.on_poll)

        # 启动浏览器后异步等端口就绪（同步等会把界面冻住十几秒）
        self._port_wait: dict | None = None
        self._port_timer = QTimer(self)
        self._port_timer.setInterval(500)
        self._port_timer.timeout.connect(self._check_port_ready)

        self.setWindowTitle("元素捕获器 — CDP 拾取")
        self.resize(1240, 780)
        self._build_ui()

    # ---------------- 界面 ----------------
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        # 顶部：连接栏
        top = QHBoxLayout()
        top.addWidget(QLabel("地址"))
        self.ed_host = QLineEdit("127.0.0.1")
        self.ed_host.setFixedWidth(110)
        top.addWidget(self.ed_host)
        top.addWidget(QLabel("端口"))
        self.ed_port = QLineEdit("9223")
        self.ed_port.setFixedWidth(70)
        top.addWidget(self.ed_port)
        self.btn_launch = QPushButton("启动 Chrome")
        self.btn_launch.setToolTip("以远程调试模式启动浏览器（自动带上 --remote-allow-origins=*）")
        self.btn_launch.clicked.connect(self.on_launch_chrome)
        top.addWidget(self.btn_launch)
        self.btn_connect = QPushButton("连接")
        self.btn_connect.clicked.connect(self.on_connect)
        top.addWidget(self.btn_connect)
        self.btn_refresh = QPushButton("刷新列表")
        self.btn_refresh.setToolTip("重新枚举标签页（新开了标签页用它）；当前页面还在就不重连")
        self.btn_refresh.clicked.connect(self.on_refresh_pages)
        top.addWidget(self.btn_refresh)
        top.addWidget(QLabel("页面"))
        self.cb_pages = QComboBox()
        self.cb_pages.setMinimumWidth(300)
        self.cb_pages.currentIndexChanged.connect(self.on_page_changed)
        top.addWidget(self.cb_pages, 1)
        self.btn_pick = QPushButton("开始拾取")
        self.btn_pick.setEnabled(False)
        self.btn_pick.clicked.connect(self.on_pick)
        top.addWidget(self.btn_pick)
        self.btn_shot = QPushButton("截图拾取")
        self.btn_shot.setEnabled(False)
        self.btn_shot.setToolTip(
            "Chrome 窗口在屏幕外、被挡住或标签页在后台时用它：\n"
            "把当前视口截图显示出来，在图上点选即可捕获元素\n"
            "（原生拾取在这种状态下收不到鼠标输入）")
        self.btn_shot.clicked.connect(self.on_shot_pick)
        top.addWidget(self.btn_shot)
        outer.addLayout(top)

        # 中部：左 = 元素库资源管理器（分组/元素）；右 = 选中元素的勾选情况
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_library_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        outer.addWidget(splitter, 1)

        self.statusBar().showMessage("填端口 → 连接 → 选择页面 → 开始拾取")
        self._restore_library()

    def _build_library_panel(self) -> QWidget:
        """左栏：像 IDE 的资源管理器 —— 分组是文件夹（可展开/折叠），组下是元素。"""
        panel = QGroupBox("元素库")
        v = QVBoxLayout(panel)

        frow = QHBoxLayout()
        self.ed_lib = QLineEdit()
        self.ed_lib.setReadOnly(True)
        self.ed_lib.setPlaceholderText("尚未打开 —— 点“打开”，之后保存都写回该文件")
        frow.addWidget(self.ed_lib, 1)
        self.btn_open_lib = QPushButton("打开 ...")
        self.btn_open_lib.setToolTip("选择已有 json 元素库；新元素追加进去，旧元素原样保留")
        self.btn_open_lib.clicked.connect(self.on_open_library)
        frow.addWidget(self.btn_open_lib)
        self.btn_new_lib = QPushButton("新建 ...")
        self.btn_new_lib.setToolTip("新建一个空元素库文件并设为当前维护目标")
        self.btn_new_lib.clicked.connect(self.on_new_library)
        frow.addWidget(self.btn_new_lib)
        self.btn_view_lib = QPushButton("总览 ...")
        self.btn_view_lib.setToolTip("独立的查看窗口：按分组总览库里全部元素")
        self.btn_view_lib.clicked.connect(self.on_view_library)
        frow.addWidget(self.btn_view_lib)
        v.addLayout(frow)

        trow = QHBoxLayout()
        self.btn_new_group = QPushButton("新建分组")
        self.btn_new_group.clicked.connect(self.on_new_group)
        trow.addWidget(self.btn_new_group)
        self.btn_del_group = QPushButton("删除分组")
        self.btn_del_group.setToolTip("把选中分组连同组内元素一起从文件删除（会确认）")
        self.btn_del_group.clicked.connect(self.on_delete_group)
        trow.addWidget(self.btn_del_group)
        self.btn_rename_ele = QPushButton("重命名")
        self.btn_rename_ele.setToolTip("重命名选中的元素")
        self.btn_rename_ele.clicked.connect(self.on_rename_element)
        trow.addWidget(self.btn_rename_ele)
        self.btn_del_ele = QPushButton("删除元素")
        self.btn_del_ele.setToolTip("删除选中的元素（会确认，直接写回文件）")
        self.btn_del_ele.clicked.connect(self.on_delete_element)
        trow.addWidget(self.btn_del_ele)
        self.btn_refresh_lib = QPushButton("刷新")
        self.btn_refresh_lib.setToolTip("重新从磁盘读取（文件可能被本工具之外的操作改过）")
        self.btn_refresh_lib.clicked.connect(self.on_refresh_library)
        trow.addWidget(self.btn_refresh_lib)
        v.addLayout(trow)

        self.tree_lib = QTreeWidget()
        self.tree_lib.setHeaderLabels(["分组 / 元素", "标签", "页面"])
        self.tree_lib.setColumnWidth(0, 200)
        self.tree_lib.setColumnWidth(1, 54)
        self.tree_lib.currentItemChanged.connect(self.on_explorer_selected)
        self.tree_lib.itemDoubleClicked.connect(self.on_explorer_double_clicked)
        v.addWidget(self.tree_lib, 1)
        v.addWidget(QLabel("单击元素 → 右侧看勾选情况；双击 → 在编辑窗口打开"))
        return panel

    def _build_detail_panel(self) -> QWidget:
        """右栏：选中元素的基本信息 + 层级勾选情况。"""
        panel = QGroupBox("元素详情")
        v = QVBoxLayout(panel)
        self.lbl_detail = QLabel("在左侧选择一个元素查看它的标签勾选情况；双击可在编辑窗口打开。")
        self.lbl_detail.setWordWrap(True)
        self.lbl_detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.lbl_detail)

        self.tree_detail = QTreeWidget()
        self.tree_detail.setHeaderLabels(["层级 / 启用规则", "方式", "值"])
        self.tree_detail.setColumnWidth(0, 220)
        self.tree_detail.setColumnWidth(1, 80)
        v.addWidget(self.tree_detail, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_edit_ele = QPushButton("在编辑窗口打开 ...")
        self.btn_edit_ele.setEnabled(False)
        self.btn_edit_ele.setToolTip("带着该元素的规则打开编辑窗口：可统计、验证、修改后保存")
        self.btn_edit_ele.clicked.connect(self.on_edit_current)
        row.addWidget(self.btn_edit_ele)
        v.addLayout(row)
        return panel

    def _busy(self, text: str) -> bool:
        """耗时操作前给反馈：状态栏文字 + 等待光标。

        CDP 调用是同步阻塞的，这里 processEvents 让界面先刷一帧；但期间用户可能
        再点一次按钮，所以用 _busy_flag 挡住重入。返回 False 表示当前正忙。
        """
        if self._busy_flag:
            return False
        self._busy_flag = True
        self.statusBar().showMessage(text)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        # 只刷新绘制、不处理用户输入：否则 processEvents 期间用户的再次点击
        # 会被立刻执行，造成重入（连点两下"连接"会跑两遍）
        QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
        return True

    def _unbusy(self) -> None:
        if not self._busy_flag:
            return
        self._busy_flag = False
        QApplication.restoreOverrideCursor()

    # ---------------- 启动浏览器 ----------------
    def on_launch_chrome(self) -> None:
        try:
            port = int(self.ed_port.text().strip())
        except ValueError:
            QMessageBox.warning(self, "端口无效", "端口必须是数字")
            return
        dlg = ChromeLaunchDialog(self, port)
        if dlg.exec_() != QDialog.Accepted:
            return
        cmd = dlg.build_command()
        if not cmd[0] or not os.path.exists(cmd[0]):
            QMessageBox.warning(self, "路径无效", "请先指定浏览器可执行文件的完整路径。")
            return
        try:
            subprocess.Popen(cmd, shell=False,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            QMessageBox.critical(self, "启动失败", str(exc))
            return
        host = self.ed_host.text().strip() or "127.0.0.1"
        self._port_wait = {"host": host, "port": port, "end": time.time() + 30.0}
        self._port_timer.start()
        self.btn_launch.setEnabled(False)
        self.statusBar().showMessage(f"已启动浏览器，正在等调试端口 {port} 就绪 ...")

    def _check_port_ready(self) -> None:
        """定时器轮询端口（同步等待会把界面冻住十几秒）。"""
        info = self._port_wait
        if not info:
            self._port_timer.stop()
            return
        try:
            pages = CdpClient.list_pages(info["host"], info["port"])
        except Exception:
            if time.time() > info["end"]:
                self._port_timer.stop()
                self._port_wait = None
                self.btn_launch.setEnabled(True)
                QMessageBox.warning(
                    self, "端口未就绪",
                    f"30 秒内没能连上 {info['host']}:{info['port']}。\n\n"
                    "常见原因：浏览器路径不对、端口被占用，"
                    "或该用户数据目录已被另一个实例占用导致新进程直接退出了。")
            return
        self._port_timer.stop()
        self._port_wait = None
        self.btn_launch.setEnabled(True)
        self.statusBar().showMessage(f"调试端口 {info['port']} 已就绪，发现 {len(pages)} 个页面")
        self._reload_pages()

    # ---------------- 连接 / 刷新 ----------------
    def on_connect(self) -> None:
        self._reload_pages()

    def on_refresh_pages(self) -> None:
        """重新枚举页面列表（比如刚开了新标签页）。当前页面还在就保持连接不重连。"""
        self._reload_pages()

    def _reload_pages(self) -> None:
        host = self.ed_host.text().strip() or "127.0.0.1"
        try:
            port = int(self.ed_port.text().strip())
        except ValueError:
            QMessageBox.warning(self, "端口无效", "端口必须是数字")
            return
        if not self._busy(f"正在读取 {host}:{port} 的页面列表 ..."):
            return
        try:
            pages = CdpClient.list_pages(host, port)
        except Exception as exc:
            self._unbusy()
            QMessageBox.critical(self, "连接失败",
                                 f"无法访问 http://{host}:{port}/json\n{exc}\n\n"
                                 f"请确认浏览器以该参数启动：\n"
                                 f"--remote-debugging-port={port} --remote-allow-origins=*")
            return
        if not pages:
            self._unbusy()
            QMessageBox.warning(self, "没有页面", "该端口下没有可调试的页面标签。")
            return

        current_id = (self.cdp.current or {}).get("id")
        keep = 0
        self.cb_pages.blockSignals(True)
        self.cb_pages.clear()
        for i, p in enumerate(pages):
            title = (p.get("title") or "")[:36]
            url = (p.get("url") or "")[:80]
            self.cb_pages.addItem(f"{title}  |  {url}", p)
            if current_id and p.get("id") == current_id:
                keep = i                      # 当前连着的页面还在，保持选中
        self.cb_pages.setCurrentIndex(keep)
        self.cb_pages.blockSignals(False)
        self._unbusy()
        self.statusBar().showMessage(f"页面列表已刷新：共 {len(pages)} 个页面")
        self._connect_selected()              # 内部判断是否同一个页面，不会无谓重连

    def on_page_changed(self, _index: int) -> None:
        """下拉框换页面时重新连接（旧连接先断开）。"""
        if self.cb_pages.currentData():
            self._connect_selected()

    def _connect_selected(self) -> None:
        tab = self.cb_pages.currentData()
        if not tab:
            self._unbusy()
            return
        # 已经是这个页面就别重连了：WebSocket 握手 + CDP 往返是实打实的开销。
        # 但要确认连接还活着——页面关掉后 current 仍在，只比 id 会"连"到一个死会话。
        if self.cdp.alive() and (self.cdp.current or {}).get("id") == tab.get("id"):
            self._unbusy()
            self._set_pick_enabled(True)
            self.statusBar().showMessage(f"已连接：{tab.get('title', '')}")
            return
        try:
            self.cdp.connect(self.ed_host.text().strip() or "127.0.0.1",
                             int(self.ed_port.text().strip()), tab)
        except Exception as exc:
            self._unbusy()
            QMessageBox.critical(self, "连接失败", str(exc))
            return
        self._unbusy()
        self._set_pick_enabled(True)
        self.statusBar().showMessage(f"已连接：{tab.get('title', '')}")

    def _set_pick_enabled(self, enabled: bool) -> None:
        """两种拾取入口（原生 / 截图）的可用状态总是一致。"""
        self.btn_pick.setEnabled(enabled)
        self.btn_shot.setEnabled(enabled)

    # ---------------- 拾取 ----------------
    def _on_cdp_closed(self, msg: str = "") -> None:
        """连接已断开：退出拾取状态并提示重连，而不是让界面卡在"停止拾取"上。"""
        self.timer.stop()
        self.btn_pick.setText("开始拾取")
        self._set_pick_enabled(False)
        self.statusBar().showMessage(msg or "浏览器连接已断开，请刷新页面列表后重新连接")

    def on_pick(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.cdp.stop_pick()                 # 断线时静默，不再抛异常
            self.btn_pick.setText("开始拾取")
            if not self.cdp.alive():
                self._on_cdp_closed()
                return
            self.statusBar().showMessage("已停止拾取")
            return
        if not self._busy("正在进入拾取模式 ..."):
            return
        try:
            self.cdp.start_pick()
        except CdpClosed as exc:
            self._unbusy()
            self._on_cdp_closed(str(exc))
            return
        except (CdpError, CdpTimeout) as exc:
            # 命令被 Chrome 拒绝/超时：一定不能继续装成"拾取中"，否则界面提示
            # "请在浏览器中点击"，而浏览器那边根本没进检查模式。
            self._unbusy()
            QMessageBox.critical(self, "拾取失败",
                                 f"{exc}\n\n如果浏览器是刚启动的，请先点“连接”"
                                 f"刷新页面列表再试。")
            return
        except Exception as exc:
            self._unbusy()
            QMessageBox.critical(self, "拾取失败", str(exc))
            return
        self._unbusy()
        self.timer.start()
        self.btn_pick.setText("停止拾取")
        self.statusBar().showMessage("请在浏览器中点击目标元素（ESC 取消）")

    def on_poll(self) -> None:
        node = self.cdp.poll_pick()
        if node is None:
            return
        self.timer.stop()
        self.btn_pick.setText("开始拾取")
        if node == PICK_CLOSED:
            self._on_cdp_closed("浏览器连接已断开（页面被关闭？），拾取已停止")
            return
        self.statusBar().showMessage("正在读取元素信息 ...")
        self.cdp.stop_pick()
        if node == -1:
            self.statusBar().showMessage("已取消拾取（ESC）")
            return
        try:
            info = self.cdp.capture(node)
        except CdpClosed as exc:
            self._on_cdp_closed(str(exc))
            return
        except (CdpError, CdpTimeout) as exc:
            self.statusBar().showMessage(f"读取元素失败：{exc}")
            return
        if not info:
            self.statusBar().showMessage("读取元素失败，请重试")
            return
        self.statusBar().showMessage(
            f"已捕获 <{info.get('tag_name')}>，共 {len(info.get('node_path') or [])} 层；"
            f"在编辑窗口勾选标签，验证后保存")
        self._open_editor(new_info=info)

    def on_shot_pick(self) -> None:
        """截图拾取：给"窗口在屏幕外/被挡住/后台标签"准备的入口。

        原生拾取要求 Chrome 窗口可见且有真实鼠标事件，这里换成在截图上点选，
        命中靠 DOM.getNodeForLocation，页面 hidden 也能用。
        """
        if not self.cdp.alive():
            QMessageBox.warning(self, "未连接", "先连接一个页面再拾取。")
            return
        if self._shot is None:
            self._shot = ShotPickDialog(self, self.cdp)
            self._shot.picked.connect(self.on_shot_picked)
        self._shot.show()
        self._shot.raise_()
        self._shot.activateWindow()
        self._shot.refresh()

    def on_shot_picked(self, info: dict) -> None:
        self.statusBar().showMessage(
            f"已捕获 <{info.get('tag_name')}>，共 {len(info.get('node_path') or [])} 层；"
            f"在编辑窗口勾选标签，验证后保存")
        self._open_editor(new_info=info)

    # ---------------- 资源管理器（分组 → 元素） ----------------
    def _find_item(self, group: str, name: str) -> dict | None:
        for e in self._lib_items:
            if (str(e.get("group_name") or "main_page") == group
                    and str(e.get("name") or "") == name):
                return e
        return None

    def _reload_explorer(self, select: tuple | None = None) -> None:
        """重建资源管理器树：分组像文件夹（可展开/折叠），组下是元素名。"""
        self._loading_explorer = True
        try:
            self.tree_lib.clear()
            groups: dict = {}
            for e in self._lib_items:
                groups.setdefault(str(e.get("group_name") or "main_page"), []).append(e)
            names = sorted(set(groups) | self._extra_groups)
            if not names:
                tip = QTreeWidgetItem(["(库为空 —— 拾取元素后保存即出现在这里)", "", ""])
                tip.setForeground(0, QColor(150, 150, 150))
                tip.setFlags(Qt.NoItemFlags)
                self.tree_lib.addTopLevelItem(tip)
            for gname in names:
                els = groups.get(gname, [])
                top = QTreeWidgetItem([gname, f"{len(els)} 个" if els else "空", ""])
                font = top.font(0)
                font.setBold(True)
                top.setFont(0, font)
                top.setData(0, Qt.UserRole, ("group", gname))
                top.setToolTip(0, f"分组 {gname}：{len(els)} 个元素")
                self.tree_lib.addTopLevelItem(top)
                for e in sorted(els, key=lambda x: str(x.get("name") or "")):
                    name = str(e.get("name") or "")
                    child = QTreeWidgetItem([name, str(e.get("tag_name") or ""),
                                             str(e.get("target_page") or "")])
                    child.setData(0, Qt.UserRole, ("element", gname, name))
                    child.setToolTip(0, name)
                    child.setToolTip(2, str(e.get("target_page") or ""))
                    top.addChild(child)
                top.setExpanded(True)
            self._select_in_explorer(select)
        finally:
            self._loading_explorer = False
        # 重建期间事件被屏蔽，这里补一次选中 → 刷新右侧详情
        cur = self.tree_lib.currentItem()
        if cur is not None:
            self.on_explorer_selected(cur, None)

    def _select_in_explorer(self, select: tuple | None) -> None:
        """按 ("group", g) / ("element", g, n) 恢复选中。"""
        if not select:
            return
        for i in range(self.tree_lib.topLevelItemCount()):
            top = self.tree_lib.topLevelItem(i)
            ref = top.data(0, Qt.UserRole) or ()
            if select[0] == "group" and len(ref) > 1 and ref[1] == select[1]:
                self.tree_lib.setCurrentItem(top)
                return
            if select[0] == "element":
                for j in range(top.childCount()):
                    child = top.child(j)
                    cref = child.data(0, Qt.UserRole) or ()
                    if len(cref) > 2 and cref[1] == select[1] and cref[2] == select[2]:
                        self.tree_lib.setCurrentItem(child)
                        return

    def on_explorer_selected(self, current: QTreeWidgetItem, _previous) -> None:
        """左栏选中变化 → 右侧展示该元素的标签勾选情况。"""
        if self._loading_explorer or current is None:
            return
        ref = current.data(0, Qt.UserRole) or ()
        if not ref:
            return
        if ref[0] == "group":
            self._clear_detail(f"分组 {ref[1]}：{current.text(1)}。展开可看到组内元素，"
                               "双击元素可在编辑窗口打开。")
            return
        item = self._find_item(ref[1], ref[2])
        if item is None:
            self._clear_detail("")
            return
        self._show_detail(item)

    def on_explorer_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        """双击元素 → 在编辑窗口打开（可查看/修改勾选并保存）。"""
        ref = item.data(0, Qt.UserRole) or ()
        if len(ref) > 2 and ref[0] == "element":
            target = self._find_item(ref[1], ref[2])
            if target is not None:
                self._open_editor(item=target)

    def _show_detail(self, item: dict) -> None:
        """右侧详情：基本信息 + 层级勾选情况（层 → 启用的规则）。"""
        self._current_item = item
        name = str(item.get("name") or "")
        group = str(item.get("group_name") or "main_page")
        self.lbl_detail.setText(
            f"<b>{name}</b>　分组：{group}<br>"
            f"标签：&lt;{item.get('tag_name', '')}&gt;　"
            f"页面：{str(item.get('target_page') or '')[:90]}<br>"
            f"xpath：{item.get('xpath', '')}")
        self.lbl_detail.setToolTip(str(item.get("outer_html") or "")[:1000])

        self.tree_detail.clear()
        node_path = item.get("node_path") or []
        checked = {int(x) for x in (item.get("checked_nodes") or [])
                   if isinstance(x, (int, float))}
        rules = item.get("node_match_rules") or {}
        for i, node in enumerate(node_path):
            mark = "✓" if i in checked else "✗"
            label = f"{mark} {i} <{node.get('tag', '')}>"
            attrs = node.get("attributes") or {}
            if attrs.get("id"):
                label += f"#{attrs['id']}"
            top = QTreeWidgetItem([label, "", ""])
            font = top.font(0)
            if i in checked:
                font.setBold(True)
            top.setFont(0, font)
            self.tree_detail.addTopLevelItem(top)
            hit = 0
            for k, spec in (rules.get(str(i)) or {}).items():
                if not isinstance(spec, (list, tuple)) or len(spec) < 3 or not spec[2]:
                    continue
                top.addChild(QTreeWidgetItem(["　" + str(k), str(spec[0]), str(spec[1])]))
                hit += 1
            if not hit:
                tip = "　（未参与定位）" if i not in checked else "　（按 tag 收窄，无启用标签）"
                child = QTreeWidgetItem([tip, "", ""])
                child.setForeground(0, QColor(150, 150, 150))
                top.addChild(child)
            top.setExpanded(True)
        self.btn_edit_ele.setEnabled(True)

    def _clear_detail(self, hint: str) -> None:
        self._current_item = None
        self.tree_detail.clear()
        self.btn_edit_ele.setEnabled(False)
        self.lbl_detail.setText(hint)

    def on_edit_current(self) -> None:
        """“在编辑窗口打开 ...”按钮：编辑当前选中的元素。"""
        if self._current_item is None:
            QMessageBox.information(self, "先选元素", "请在左栏选中一个元素。")
            return
        self._open_editor(item=self._current_item)

    # ---------------- 库读写与维护 ----------------
    def group_names(self) -> list:
        """当前库的全部分组（含会话内新建的空分组），始终带 main_page。"""
        groups = {str(e.get("group_name") or "main_page") for e in self._lib_items}
        groups |= self._extra_groups
        groups.add("main_page")
        return sorted(groups)

    def read_library(self) -> list:
        """读当前库文件（未打开时返回空列表）。"""
        if not self._lib_path:
            return []
        return self._load_lib(self._lib_path)

    def _write_library(self, items: list, status: str = "",
                       select: tuple | None = None) -> list:
        """备份 + 写回当前库，并全局刷新（资源管理器/详情/总览窗/编辑器的分组下拉）。"""
        if not self._lib_path:
            raise RuntimeError("尚未打开元素库文件")
        if os.path.exists(self._lib_path):
            shutil.copy2(self._lib_path, self._lib_path + ".bak")
        with open(self._lib_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=4)
        self._lib_items = items
        self._reload_explorer(select)
        self._refresh_viewer_if_open()
        if self._editor is not None and self._editor.isVisible():
            self._editor._refresh_group_items()
        if status:
            self.statusBar().showMessage(f"{status}（共 {len(items)} 条）")
        return items

    def upsert_item(self, item: dict, old: tuple | None = None) -> list:
        """新增/更新元素。old=(group, name)：先把旧身份移除（支持重命名/换分组）。"""
        name = str(item.get("name") or "")
        group = str(item.get("group_name") or "main_page")
        items = self.read_library()

        def same(entry: dict, target_name: str, target_group: str) -> bool:
            return (str(entry.get("name") or "") == target_name
                    and str(entry.get("group_name") or "main_page") == target_group)

        items = [e for e in items if not same(e, name, group)]
        if old and (old[1], old[0]) != (name, group):
            items = [e for e in items if not same(e, old[1], old[0])]
        items.append(item)
        self._extra_groups.discard(group)
        return self._write_library(items, f"已写入 [{group}] {name}",
                                   select=("element", group, name))

    def delete_item(self, name: str, group: str) -> list:
        items = [e for e in self.read_library()
                 if not (str(e.get("name") or "") == name
                         and str(e.get("group_name") or "main_page") == group)]
        return self._write_library(items, f"已删除 [{group}] {name}")

    def rename_item(self, name: str, group: str, new_name: str) -> list:
        items = self.read_library()
        for e in items:
            if (str(e.get("name") or "") == name
                    and str(e.get("group_name") or "main_page") == group):
                e["name"] = new_name
        return self._write_library(items, f"已重命名 {name} → {new_name}",
                                   select=("element", group, new_name))

    def delete_group(self, group: str) -> list:
        items = [e for e in self.read_library()
                 if str(e.get("group_name") or "main_page") != group]
        self._extra_groups.discard(group)
        return self._write_library(items, f"已删除分组 {group}")

    def on_new_group(self) -> None:
        """新建一个空分组（先存在于会话里，保存进第一个元素后写入文件）。"""
        if not self._lib_path:
            QMessageBox.information(self, "未打开元素库", "请先打开或新建一个元素库文件。")
            return
        text, ok = QInputDialog.getText(self, "新建分组", "分组名：")
        text = (text or "").strip()
        if not ok or not text:
            return
        if text in self.group_names():
            QMessageBox.information(self, "已存在", f"分组 {text} 已存在。")
            return
        self._extra_groups.add(text)
        self._reload_explorer(("group", text))
        self.statusBar().showMessage(f"已新建空分组 {text}（保存第一个元素后写入文件）")

    def _current_group(self) -> tuple | None:
        """当前选中项所属分组：(组名, 组内元素数)；未选中返回 None。"""
        cur = self.tree_lib.currentItem()
        if cur is None:
            return None
        ref = cur.data(0, Qt.UserRole) or ()
        if len(ref) < 2:
            return None
        group = str(ref[1])
        count = sum(1 for e in self._lib_items
                    if str(e.get("group_name") or "main_page") == group)
        return group, count

    def on_delete_group(self) -> None:
        """删除分组 = 连同组内元素一起从文件移除（会确认）。"""
        info = self._current_group()
        if info is None:
            QMessageBox.information(self, "先选分组", "请在左栏选中一个分组（或组内元素）。")
            return
        group, count = info
        if count and QMessageBox.question(
                self, "删除分组",
                f"分组 {group} 下有 {count} 个元素，会一并删除并直接写回文件。\n\n继续？"
        ) != QMessageBox.Yes:
            return
        self.delete_group(group)

    def on_rename_element(self) -> None:
        if self._current_item is None:
            QMessageBox.information(self, "先选元素", "请在左栏选中一个元素。")
            return
        old = str(self._current_item.get("name") or "")
        group = str(self._current_item.get("group_name") or "main_page")
        text, ok = QInputDialog.getText(self, "重命名元素", "新的元素名：", text=old)
        text = (text or "").strip()
        if not ok or not text or text == old:
            return
        if self._find_item(group, text) is not None:
            QMessageBox.warning(self, "重名", f"分组 {group} 下已有名为 {text} 的元素。")
            return
        self.rename_item(old, group, text)

    def on_delete_element(self) -> None:
        if self._current_item is None:
            QMessageBox.information(self, "先选元素", "请在左栏选中一个元素。")
            return
        name = str(self._current_item.get("name") or "")
        group = str(self._current_item.get("group_name") or "main_page")
        if QMessageBox.question(
                self, "删除元素",
                f"删除 [{group}] {name}？会直接写回文件。") != QMessageBox.Yes:
            return
        self.delete_item(name, group)

    def on_refresh_library(self) -> None:
        """重新从磁盘读库（文件可能被外部改过）。"""
        if not self._lib_path:
            QMessageBox.information(self, "未打开元素库", "请先打开或新建一个元素库文件。")
            return
        try:
            items = self.read_library()
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return
        self._lib_items = items
        self._extra_groups = set()
        self._reload_explorer()
        self._refresh_viewer_if_open()
        self.statusBar().showMessage(
            f"已刷新：{len(items)} 条元素 / {len(self.group_names())} 个分组")

    # ---------------- 编辑窗口 ----------------
    def _open_editor(self, new_info: dict | None = None, item: dict | None = None) -> None:
        """打开/复用编辑窗口：new_info=新捕获；item=库中已有元素。"""
        if self._editor is None:
            self._editor = ElementEditorDialog(self)
        if new_info is not None:
            self._editor.load_new(new_info)
        elif item is not None:
            self._editor.load_saved(item)
        self._editor.show()
        self._editor.raise_()
        self._editor.activateWindow()

    # ---------------- 元素库（打开 / 新建 / 查看 / 恢复） ----------------
    def _set_library(self, path: str, items: list) -> None:
        """把某个元素库设为当前维护目标：路径上屏 + 重建资源管理器与详情。"""
        self._lib_path = path
        self._lib_items = items
        self._extra_groups = set()
        self.ed_lib.setText(path)
        self.ed_lib.setToolTip(path)
        self._reload_explorer()
        self._clear_detail("在左侧选择一个元素查看它的标签勾选情况；双击可在编辑窗口打开。")
        self._refresh_viewer_if_open()
        if self._editor is not None and self._editor.isVisible():
            self._editor._refresh_group_items()

    def _refresh_viewer_if_open(self) -> None:
        if self._viewer is not None and self._viewer.isVisible() and self._lib_path:
            self._viewer.load(self._lib_path, self._lib_items)

    def on_open_library(self) -> None:
        """打开已有元素库：之后的保存都写回它，旧元素原样保留。"""
        start = os.path.dirname(self._lib_path) if self._lib_path else HERE
        path, _ = QFileDialog.getOpenFileName(
            self, "打开元素库", start, "JSON 元素库 (*.json)")
        if not path:
            return
        try:
            items = self._load_lib(path)
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        self._set_library(path, items)
        save_launch_config({"library_path": path})
        self.statusBar().showMessage(
            f"已打开元素库：{os.path.basename(path)}"
            f"（{len(items)} 条元素 / {len(self.group_names())} 个分组）")

    def on_new_library(self) -> None:
        """新建（或接管）一个元素库文件并设为当前维护目标。"""
        start = os.path.dirname(self._lib_path) if self._lib_path else HERE
        # 注：选中已存在的文件是“接管”（读入继续维护），不会清空，
        # 所以禁掉了原生“是否替换”确认，避免误导
        path, _ = QFileDialog.getSaveFileName(
            self, "新建元素库", os.path.join(start, "element_library.json"),
            "JSON 元素库 (*.json)", options=QFileDialog.DontConfirmOverwrite)
        if not path:
            return
        try:
            if os.path.exists(path):
                items = self._load_lib(path)      # 已存在就当“打开”接管，不清空
            else:
                items = []
                with open(path, "w", encoding="utf-8") as f:
                    json.dump([], f)
        except Exception as exc:
            QMessageBox.critical(self, "新建失败", str(exc))
            return
        self._set_library(path, items)
        save_launch_config({"library_path": path})
        self.statusBar().showMessage(
            f"已新建元素库：{os.path.basename(path)}"
            f"（{len(items)} 条元素 / {len(self.group_names())} 个分组）")

    def on_view_library(self) -> None:
        """打开（或复用）总览窗口，按分组展示当前元素库里的已有元素。"""
        if not self._lib_path:
            QMessageBox.information(
                self, "未打开元素库",
                "请先点“打开 ...”选择要维护的 json 文件。")
            return
        try:
            items = self._load_lib(self._lib_path)
        except Exception as exc:
            QMessageBox.critical(self, "读取失败", str(exc))
            return
        self._lib_items = items
        self._show_viewer(items)

    def _show_viewer(self, items: list) -> None:
        """显示总览窗口并刷新数据（窗口只建一个，反复查看时复用）。"""
        if not self._lib_path:
            return
        if self._viewer is None:
            self._viewer = LibraryViewerDialog(self)
        self._viewer.load(self._lib_path, items)
        self._viewer.show()
        self._viewer.raise_()
        self._viewer.activateWindow()

    def _restore_library(self) -> None:
        """启动时恢复上次维护的元素库，省得每次重选文件。"""
        path = str(load_launch_config().get("library_path") or "")
        if not path or not os.path.exists(path):
            return
        try:
            items = self._load_lib(path)
        except Exception as exc:
            self.statusBar().showMessage(f"上次的元素库读取失败：{exc}")
            return
        self._set_library(path, items)
        self.statusBar().showMessage(
            f"已恢复元素库：{os.path.basename(path)}"
            f"（{len(items)} 条元素 / {len(self.group_names())} 个分组）")

    # ---------------- 元素库文件读写 ----------------
    @staticmethod
    def _load_lib(path: str) -> list:
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise RuntimeError(f"元素库解析失败，为免覆盖已中止：{exc}")
        if not isinstance(data, list):
            raise RuntimeError("元素库顶层不是数组，为免覆盖已中止")
        return data

    # ---------------- 关闭 ----------------
    def closeEvent(self, event) -> None:
        self.timer.stop()
        self._port_timer.stop()
        try:
            self.cdp.stop_pick()
            self.cdp.clear_marks()
        except Exception:
            pass
        self.cdp.close()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = CaptureWindow()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
