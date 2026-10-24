#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""element_operator.py -- 按元素库（分组 + 名称）定位页面元素并执行操作。

设计要点
--------
* 定位算法与元素捕获器（element_capture_qt.py）完全一致：
  用 `node_path` + `checked_nodes` + `node_match_rules`，从 document 开始逐层收窄，
  每层先按 tag 收集后代（含 Shadow DOM），再用该层启用的属性规则过滤。
* 每次操作都重新定位一次，页面重渲染、元素重建、列表刷新后依然有效。
* 不依赖 PyQt5，只依赖 PyChromeDevTools（其内部用 requests + websocket-client），
  可以放在无 GUI 的自动化脚本里跑。

快速开始
--------
    from cdp_flow import ElementOperator

    op = ElementOperator(port=9223, library="elements.json").connect()
    el = op.element("main_page", "login_button")

    el.wait_loaded(timeout=5)          # 等元素出现并可见
    el.scroll_into_view()              # 移动至可见
    el.click()                         # 点击
    print(el.text())                   # 文本
    print(el.attr("data-id"))          # 单个属性
    el.input_text("hello")             # 文本框输入
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

__all__ = [
    "ElementOperator",
    "ElementLibrary",
    "Element",
    "CdpSession",
    "ElementOperatorError",
    "ElementNotFound",
    "ElementNotInteractable",
    "OperationTimeout",
]

log = logging.getLogger("element_operator")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9223
DEFAULT_GROUP = "main_page"
DEFAULT_TIMEOUT = 5.0
POLL_INTERVAL = 0.2


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class ElementOperatorError(RuntimeError):
    """操作失败的基础异常。"""


class ElementNotFound(ElementOperatorError):
    """元素库里没有该元素，或页面上定位不到。"""


class ElementNotInteractable(ElementOperatorError):
    """元素存在但不可交互（隐藏 / 禁用 / 只读）。"""


class OperationTimeout(ElementOperatorError):
    """等待超时。"""


# --------------------------------------------------------------------------- #
# 页面内定位 + 动作脚本
#
# 定位部分与 element_capture_qt.py 的 COUNT_FN_TMPL 同源（同样的 node_path 语义、
# 同样的匹配方式、同样穿透 Shadow DOM），这样"捕获器存下来的规则"才能直接复用。
# payload = {node_path, checked, rules, action, ...动作参数}
# --------------------------------------------------------------------------- #
_ACTION_JS = r"""
(function () {
    try {
        var data = __PAYLOAD__;
        var nodePath = Array.isArray(data.node_path) ? data.node_path : [];
        var rawChecked = Array.isArray(data.checked) ? data.checked : [];
        var rulesByNode = data.rules || {};
        var action = (data.action || 'count').toString();

        // ---------- 匹配规则 ----------
        function matchValue(val, rule) {
            val = (val === undefined || val === null) ? '' : val.toString();
            var t = (rule[0] || '等于').toString();
            var v = (rule[1] === undefined || rule[1] === null) ? '' : rule[1].toString();
            if (t === '等于' || t === 'equals') return val === v;
            if (t === '包含' || t === 'contains') return val.indexOf(v) >= 0;
            if (t === '正则' || t === 'regex') {
                try { return new RegExp(v).test(val); } catch (e) { return false; }
            }
            return false;
        }
        function activeRules(ruleMap) {
            var out = [];
            if (!ruleMap) return out;
            for (var k in ruleMap) {
                var r = ruleMap[k];
                if (!r || !r[2]) continue;                 // [方式, 值, 是否启用]
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

        // ---------- 候选收集（穿透 Shadow DOM） ----------
        function traverseShadowRoots(root, tagName) {
            var out = [];
            var queryTag = (tagName || '').toLowerCase() || '*';
            function add(node) {
                if (node && node.nodeType === 1 && out.indexOf(node) < 0) out.push(node);
            }
            function collectWithin(r) {
                if (!r || !r.querySelectorAll) return;
                var nodes = [];
                try { nodes = Array.prototype.slice.call(r.querySelectorAll(queryTag)); }
                catch (e) { nodes = []; }
                for (var i = 0; i < nodes.length; i++) add(nodes[i]);
            }
            function collectAllShadow(r) {
                if (!r || !r.querySelectorAll) return;
                var hosts = [];
                try { hosts = Array.prototype.slice.call(r.querySelectorAll('*')); }
                catch (e) { hosts = []; }
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

        // ---------- 定位 ----------
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
            if (!next.length) return { ok: true, found: false, count: 0, trace: trace, action: action };
            scopes = next;
        }

        // ---------- 元素状态 ----------
        var rectOf = function (e) {
            var r = e.getBoundingClientRect();
            return { x: r.left, y: r.top, left: r.left, top: r.top,
                     width: r.width, height: r.height,
                     right: r.right, bottom: r.bottom };
        };
        var pageRectOf = function (e) {
            var r = rectOf(e);
            var sx = window.scrollX || 0, sy = window.scrollY || 0;
            r.x += sx; r.y += sy; r.left += sx; r.top += sy;
            r.right += sx; r.bottom += sy;
            return r;
        };
        var isVisible = function (e) {
            if (!e || !e.isConnected) return false;
            var r = e.getBoundingClientRect();
            if (r.width <= 0 && r.height <= 0) return false;
            var cs = window.getComputedStyle(e);
            if (!cs) return true;
            if (cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse') return false;
            if (parseFloat(cs.opacity) === 0) return false;
            return true;
        };
        var inViewport = function (r) {
            var vw = window.innerWidth || 0, vh = window.innerHeight || 0;
            return r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
        };
        var currentValue = function (e) {
            try {
                if (e.isContentEditable) return (e.textContent || '');
                if (e.value === undefined || e.value === null) return '';
                return '' + e.value;
            } catch (err) { return ''; }
        };
        var isEditable = function (e) {
            try {
                if (e.disabled || e.readOnly) return false;
                var tag = (e.tagName || '').toLowerCase();
                return tag === 'input' || tag === 'textarea' || tag === 'select' || !!e.isContentEditable;
            } catch (err) { return false; }
        };

        var el = scopes[0];
        var out = {
            ok: true, found: true, count: scopes.length, action: action,
            tag: (el.tagName || '').toLowerCase(), trace: trace,
            rect: rectOf(el), page_rect: pageRectOf(el)
        };

        // ---------- 动作 ----------
        if (action === 'count') return out;

        if (action === 'state') {
            out.state = {
                tag: (el.tagName || '').toLowerCase(),
                visible: isVisible(el),
                in_viewport: inViewport(rectOf(el)),
                enabled: !el.disabled && el.getAttribute('aria-disabled') !== 'true',
                editable: isEditable(el),
                focused: document.activeElement === el,
                value: currentValue(el).slice(0, 200),
                checked: (typeof el.checked === 'boolean') ? el.checked : null,
                text: ((el.innerText || el.textContent || '') + '').trim().slice(0, 200),
                rect: rectOf(el)
            };
            return out;
        }

        if (action === 'scroll') {
            try {
                el.scrollIntoView({ block: data.block || 'center', inline: 'nearest', behavior: 'instant' });
            } catch (e) {
                try { el.scrollIntoView(); } catch (e2) {}
            }
            out.rect = rectOf(el);
            out.page_rect = pageRectOf(el);
            out.in_viewport = inViewport(out.rect);
            return out;
        }

        if (action === 'text') {
            out.text = ((el.innerText || '') + '').trim();
            out.text_content = ((el.textContent || '') + '').trim();
            return out;
        }

        if (action === 'value') {
            out.value = currentValue(el);
            return out;
        }

        if (action === 'attr') {
            out.value = (data.name === undefined || data.name === null)
                ? null : el.getAttribute(String(data.name));
            out.exists = out.value !== null;
            return out;
        }

        if (action === 'attrs') {
            var map = {};
            for (var ai = 0; ai < el.attributes.length; ai++) {
                var a = el.attributes[ai];
                map[a.name] = a.value;
            }
            out.attributes = map;
            return out;
        }

        if (action === 'html') {
            out.html = (el.outerHTML || '').slice(0, 200000);
            return out;
        }

        if (action === 'rect') {
            out.rect = rectOf(el);
            out.page_rect = pageRectOf(el);
            out.in_viewport = inViewport(out.rect);
            return out;
        }

        if (action === 'visible') {
            out.visible = isVisible(el);
            return out;
        }

        if (action === 'enabled') {
            out.enabled = !el.disabled && el.getAttribute('aria-disabled') !== 'true';
            return out;
        }

        if (action === 'click') {
            try { el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (e) {}
            try {
                el.click();
            } catch (e) {
                try {
                    var mc = { bubbles: true, cancelable: true, view: window };
                    el.dispatchEvent(new MouseEvent('mousedown', mc));
                    el.dispatchEvent(new MouseEvent('mouseup', mc));
                    el.dispatchEvent(new MouseEvent('click', mc));
                } catch (e2) { return { ok: false, error: String(e2) }; }
            }
            out.rect = rectOf(el);
            return out;
        }

        if (action === 'hover') {
            try { el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (e) {}
            var mv = { bubbles: true, cancelable: true, view: window };
            try {
                el.dispatchEvent(new MouseEvent('mouseover', mv));
                el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false, cancelable: true, view: window }));
                el.dispatchEvent(new MouseEvent('mousemove', mv));
            } catch (e) { return { ok: false, error: String(e) }; }
            out.rect = rectOf(el);
            return out;
        }

        if (action === 'focus' || action === 'blur') {
            try { el[action](); } catch (e) { return { ok: false, error: String(e) }; }
            out.focused = (document.activeElement === el);
            return out;
        }

        if (action === 'input') {
            var text = (data.text === undefined || data.text === null) ? '' : String(data.text);
            try { el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (e) {}
            try { el.focus(); } catch (e) {}
            if (!data.clear) text = currentValue(el) + text;
            try {
                if (el.isContentEditable) {
                    el.textContent = text;
                } else {
                    // React / Vue 会劫持 value 的 setter，必须走原型上的原生 setter
                    var proto = (el.tagName === 'TEXTAREA') ? HTMLTextAreaElement.prototype
                              : (el.tagName === 'SELECT') ? HTMLSelectElement.prototype
                              : HTMLInputElement.prototype;
                    var desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) desc.set.call(el, text);
                    else el.value = text;
                }
            } catch (e) { return { ok: false, error: String(e) }; }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            out.value = currentValue(el);
            return out;
        }

        if (action === 'clear') {
            try { el.focus(); } catch (e) {}
            try {
                if (el.isContentEditable) el.textContent = '';
                else {
                    var p2 = (el.tagName === 'TEXTAREA') ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                    var d2 = Object.getOwnPropertyDescriptor(p2, 'value');
                    if (d2 && d2.set) d2.set.call(el, '');
                    else el.value = '';
                }
            } catch (e) { return { ok: false, error: String(e) }; }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            out.value = currentValue(el);
            return out;
        }

        if (action === 'select') {
            var opts = el.options || [];
            var target = null, i2;
            if (data.value !== undefined && data.value !== null) {
                for (i2 = 0; i2 < opts.length; i2++) {
                    if (opts[i2].value === String(data.value)) { target = opts[i2]; break; }
                }
            } else if (data.label !== undefined && data.label !== null) {
                for (i2 = 0; i2 < opts.length; i2++) {
                    if (((opts[i2].textContent || '').trim()) === String(data.label)) { target = opts[i2]; break; }
                }
            } else if (typeof data.index === 'number') {
                target = opts[data.index] || null;
            }
            if (!target) return { ok: false, found: true, error: '没有匹配的 option' };
            try {
                el.value = target.value;
                if (el.selectedIndex !== target.index) el.selectedIndex = target.index;
            } catch (e) { return { ok: false, error: String(e) }; }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            out.value = currentValue(el);
            out.label = (target.textContent || '').trim();
            return out;
        }

        if (action === 'check' || action === 'uncheck') {
            var want = (action === 'check');
            try {
                if (!!el.checked !== want) el.click();
            } catch (e) { return { ok: false, error: String(e) }; }
            out.checked = !!el.checked;
            return out;
        }

        if (action === 'press') {
            var key = (data.key === undefined || data.key === null) ? 'Enter' : String(data.key);
            var codes = { Enter: 13, Tab: 9, Escape: 27, ' ': 32, Backspace: 8, Delete: 46,
                          ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39 };
            var keyCode = codes[key] !== undefined ? codes[key]
                : (key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0);
            try { el.focus(); } catch (e) {}
            var base = { key: key, code: key, keyCode: keyCode, which: keyCode,
                         bubbles: true, cancelable: true };
            try {
                el.dispatchEvent(new KeyboardEvent('keydown', base));
                el.dispatchEvent(new KeyboardEvent('keypress', base));
                el.dispatchEvent(new KeyboardEvent('keyup', base));
            } catch (e) { return { ok: false, error: String(e) }; }
            return out;
        }

        if (action === 'submit') {
            var form = el.form || (el.closest ? el.closest('form') : null);
            if (!form) return { ok: false, found: true, error: '元素不在表单内' };
            try {
                if (form.requestSubmit) form.requestSubmit();
                else form.submit();
            } catch (e) { return { ok: false, error: String(e) }; }
            return out;
        }

        if (action === 'highlight') {
            try { el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' }); } catch (e) {}
            var rr = rectOf(el);
            var box = document.createElement('div');
            box.setAttribute('data-elecap-highlight', '1');
            box.style.cssText = 'position:fixed;box-sizing:border-box;pointer-events:none;'
                + 'z-index:2147483647;border:2px solid #2196f3;background:rgba(33,150,243,0.18);';
            box.style.left = rr.x + 'px';
            box.style.top = rr.y + 'px';
            box.style.width = Math.max(rr.width, 2) + 'px';
            box.style.height = Math.max(rr.height, 2) + 'px';
            (document.documentElement || document.body).appendChild(box);
            var ms = (typeof data.duration === 'number' && data.duration > 0) ? data.duration : 1500;
            setTimeout(function () { try { box.parentNode.removeChild(box); } catch (e) {} }, ms);
            out.rect = rr;
            return out;
        }

        return { ok: false, error: '未知动作: ' + action };
    } catch (e) {
        return { ok: false, error: String((e && e.stack) || e) };
    }
})()
"""


# --------------------------------------------------------------------------- #
# 定位执行（ElementOperator / Browser.tab 共用）
# --------------------------------------------------------------------------- #
def checked_layers(definition: dict) -> list:
    """取 checked_nodes；缺失或为空时退化为「全部层都参与」，否则必然定不到位。"""
    n = len(definition.get("node_path") or [])
    raw = definition.get("checked_nodes")
    checked: list[int] = []
    if isinstance(raw, list):
        for x in raw:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= i < n and i not in checked:
                checked.append(i)
    checked.sort()
    return checked or list(range(n))


def exec_action(session: "CdpSession", definition: dict, action: str,
                **params) -> dict:
    """在指定会话里执行一次「定位 + 动作」，返回页面原始结果字典。"""
    node_path = definition.get("node_path") or []
    if not node_path:
        raise ElementOperatorError(
            f"元素 {definition.get('name')!r} 缺少 node_path，无法定位（捕获器版本过旧？）")
    payload = {
        "node_path": node_path,
        "checked": checked_layers(definition),
        "rules": definition.get("node_match_rules") or {},
        "action": action,
    }
    payload.update(params)
    script = _ACTION_JS.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    res = session.evaluate(script)
    if not isinstance(res, dict):
        raise ElementOperatorError("页面没有返回有效结果（页面可能已跳转或关闭）")
    return res


# --------------------------------------------------------------------------- #
# 元素库
# --------------------------------------------------------------------------- #
class ElementLibrary:
    """元素库文件（元素捕获器保存的 JSON 数组）。

    结构：每个元素含 name / group_name / node_path / checked_nodes / node_match_rules …
    """

    def __init__(self, path: str | None = None):
        self.path = ""
        self.items: list = []
        if path:
            self.load(path)

    # ---------------- 读取 ----------------
    def load(self, path: str) -> list:
        """加载元素库（替换内存中的内容）。路径不存在或格式不对会直接报错。"""
        path = os.path.abspath(os.path.expanduser(str(path)))
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ElementOperatorError(f"元素库格式错误（顶层应为数组）：{path}")
        self.path = path
        self.items = [x for x in data if isinstance(x, dict)]
        log.debug("元素库已加载：%s（%d 条）", path, len(self.items))
        return self.items

    def reload(self) -> list:
        if not self.path:
            raise ElementOperatorError("还没有加载过元素库，无法 reload")
        return self.load(self.path)

    def set_items(self, items: list) -> None:
        """直接注入元素列表（不落盘），方便测试或从数据库读出后使用。"""
        if not isinstance(items, list):
            raise ElementOperatorError("items 必须是 list")
        self.items = [x for x in items if isinstance(x, dict)]
        self.path = ""

    # ---------------- 查询 ----------------
    @staticmethod
    def _group_of(item: dict) -> str:
        return str(item.get("group_name") or DEFAULT_GROUP)

    @staticmethod
    def _name_of(item: dict) -> str:
        return str(item.get("name") or "")

    def groups(self) -> dict:
        """{分组: [元素名...]}，按名字排序。"""
        out: dict[str, list] = {}
        for item in self.items:
            out.setdefault(self._group_of(item), []).append(self._name_of(item))
        return {g: sorted(n for n in names if n) for g, names in sorted(out.items())}

    def names(self, group: str) -> list:
        return self.groups().get(str(group), [])

    def get(self, group: str, name: str) -> dict | None:
        for item in self.items:
            if self._group_of(item) == str(group) and self._name_of(item) == str(name):
                return item
        return None

    def find(self, name: str) -> list:
        """按名字全局搜索（可能跨分组重名，返回全部）。"""
        return [x for x in self.items if self._name_of(x) == str(name)]

    def require(self, group: str, name: str) -> dict:
        item = self.get(group, name)
        if item is None:
            hint = ""
            names = self.names(group)
            if names:
                hint = f"；该分组下有：{', '.join(names[:10])}"
            else:
                hint = f"；现有分组：{', '.join(self.groups().keys()) or '(空)'}"
            raise ElementNotFound(f"元素库里没有 [{group}] {name}{hint}")
        return item

    def __len__(self) -> int:
        return len(self.items)

    def __contains__(self, key) -> bool:
        if isinstance(key, tuple) and len(key) == 2:
            return self.get(key[0], key[1]) is not None
        return bool(self.find(str(key)))


# --------------------------------------------------------------------------- #
# 轻量 CDP 连接（不依赖 PyQt5）
# --------------------------------------------------------------------------- #
class CdpSession:
    """连到一个页面标签页，只做两件事：执行 JS、派发鼠标事件。"""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 10.0):
        self.host = str(host or DEFAULT_HOST)
        self.port = int(port or DEFAULT_PORT)
        self.timeout = float(timeout)
        self.chrome = None
        self.page: dict | None = None

    # ---------------- 页面发现 ----------------
    @staticmethod
    def list_pages(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> list:
        url = f"http://{host}:{port}/json"
        with urllib.request.urlopen(url, timeout=3) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
        return [t for t in raw if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]

    def connect(self, page=None) -> dict:
        """连接页面。

        page 可以是：None（第一个标签页）/ 下标（int）/ 关键字（匹配 url 或 title）/
        `/json` 返回的 target 字典。
        """
        try:
            from PyChromeDevTools import ChromeInterface
        except ImportError as exc:                       # pragma: no cover
            raise ElementOperatorError(
                "缺少依赖 PyChromeDevTools，请先执行：pip install PyChromeDevTools") from exc

        pages = self.list_pages(self.host, self.port)
        if not pages:
            raise ElementOperatorError(
                f"{self.host}:{self.port} 下没有可调试页面（浏览器是否用 "
                f"--remote-debugging-port={self.port} 启动？）")

        target = self._pick_page(pages, page)
        chrome = ChromeInterface(host=self.host, port=self.port, tab=0,
                                 timeout=self.timeout, auto_connect=False,
                                 suppress_origin=True)
        chrome.connect_targetID(target["id"])
        self.close()                                     # 关掉旧连接（同一实例先连后断会有句柄泄漏）
        self.chrome = chrome
        self.page = target
        log.debug("已连接：%s", target.get("title") or target.get("url"))
        return target

    @staticmethod
    def _pick_page(pages: list, page) -> dict:
        if page is None:
            return pages[0]
        if isinstance(page, dict):
            for p in pages:
                if p.get("id") == page.get("id"):
                    return p
            raise ElementOperatorError(f"指定的页面已关闭：{page.get('title') or page.get('id')}")
        if isinstance(page, int):
            if not (0 <= page < len(pages)):
                raise ElementOperatorError(f"页面下标越界：{page}（共 {len(pages)} 个）")
            return pages[page]
        key = str(page).strip().lower()
        for p in pages:
            if key in str(p.get("url") or "").lower() or key in str(p.get("title") or "").lower():
                return p
        titles = "；".join((p.get("title") or p.get("url") or "")[:50] for p in pages)
        raise ElementOperatorError(f"没有匹配 '{page}' 的页面。现有：{titles}")

    def close(self) -> None:
        if self.chrome is not None:
            try:
                self.chrome.close()
            except Exception:
                pass
        self.chrome = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # ---------------- 命令 ----------------
    def call(self, cmd: str, **kwargs):
        if self.chrome is None:
            raise ElementOperatorError("还没有连接页面，先调用 connect()")
        domain, _, method = cmd.partition(".")
        fn = getattr(getattr(self.chrome, domain), method)
        msg, _ = fn(**kwargs)
        if not isinstance(msg, dict):
            return None
        if "error" in msg and "result" not in msg:
            raise ElementOperatorError(f"CDP {cmd} 失败：{msg.get('error')}")
        return msg.get("result", msg)

    def evaluate(self, expression: str):
        """执行 JS 并按值取回结果。"""
        res = self.call("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=False)
        if not isinstance(res, dict):
            return None
        if "exceptionDetails" in res:
            detail = json.dumps(res.get("exceptionDetails"), ensure_ascii=False)
            raise ElementOperatorError(f"页面 JS 执行异常：{detail[:500]}")
        inner = res.get("result") or {}
        return inner.get("value")

    # ---------------- 鼠标 ----------------
    def click_at(self, x: float, y: float, button: str = "left") -> None:
        """在视口坐标处派发一次真实鼠标点击（适合只认坐标事件的自绘控件）。

        注意：坐标必须是整数（协议要求），且元素必须先在视口内。
        """
        px, py = int(round(float(x))), int(round(float(y)))
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py)
        time.sleep(0.05)
        self.call("Input.dispatchMouseEvent", type="mousePressed", x=px, y=py,
                  button=button, clickCount=1)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", x=px, y=py,
                  button=button, clickCount=1)

    def move_to(self, x: float, y: float) -> None:
        self.call("Input.dispatchMouseEvent", type="mouseMoved",
                  x=int(round(float(x))), y=int(round(float(y))))


# --------------------------------------------------------------------------- #
# 单个元素的操作入口
# --------------------------------------------------------------------------- #
class Element:
    """元素库中的一条元素记录，绑定到某个页面上。

    每次调用都会重新定位一次（页面刷新/重渲染后不需要重新拿句柄）。
    """

    def __init__(self, operator: "ElementOperator", definition: dict):
        self.op = operator
        self.defn = definition
        self.group = str(definition.get("group_name") or DEFAULT_GROUP)
        self.name = str(definition.get("name") or "")

    # ---------------- 展示 ----------------
    @property
    def label(self) -> str:
        return f"[{self.group}] {self.name}"

    def __repr__(self) -> str:
        return f"<Element {self.label} tag={self.defn.get('tag_name') or '?'}>"

    @property
    def definition(self) -> dict:
        """原始元素记录（含 node_path / 规则等），只读用途。"""
        return self.defn

    # ---------------- 内部 ----------------
    def _run(self, action: str, **params) -> dict:
        return self.op._exec(self.defn, action, **params)

    def _require(self, action: str, **params) -> dict:
        res = self._run(action, **params)
        if not res.get("ok"):
            raise ElementOperatorError(f"{self.label} {action} 失败：{res.get('error')}")
        if not res.get("found"):
            raise ElementNotFound(f"{self.label} 在页面上定位不到"
                                  f"（匹配链：{self._trace_text(res)}）")
        return res

    @staticmethod
    def _trace_text(res: dict) -> str:
        trace = res.get("trace") or []
        return " -> ".join(f"{t.get('tag')}:{t.get('count')}" for t in trace)

    def _wait(self, probe, timeout: float, interval: float, what: str) -> bool:
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            try:
                if probe():
                    return True
            except ElementNotFound:
                pass
            if time.time() >= deadline:
                raise OperationTimeout(f"{self.label} 等待超时（{timeout}s）：{what}")
            time.sleep(interval)

    # ---------------- 存在 / 状态 ----------------
    def count(self) -> int:
        """页面上匹配到的元素个数（>1 说明规则不够精确）。"""
        res = self._run("count")
        if not res.get("ok"):
            raise ElementOperatorError(f"{self.label} 定位失败：{res.get('error')}")
        return int(res.get("count") or 0)

    def exists(self, timeout: float = 0.0) -> bool:
        """元素是否存在于 DOM。timeout > 0 时轮询等待。"""
        if timeout and timeout > 0:
            try:
                self._wait(lambda: self.count() > 0, timeout, POLL_INTERVAL, "元素出现")
                return True
            except OperationTimeout:
                return False
        return self.count() > 0

    def state(self) -> dict | None:
        """一次拿到可见性/可用性/值/矩形等状态；不存在时返回 None。

        返回示例：
            {"tag": "input", "visible": True, "in_viewport": True, "enabled": True,
             "editable": True, "focused": False, "value": "", "checked": None,
             "text": "", "rect": {...}}
        """
        res = self._run("state")
        if not res.get("ok"):
            raise ElementOperatorError(f"{self.label} 状态读取失败：{res.get('error')}")
        return res.get("state") if res.get("found") else None

    def is_visible(self) -> bool:
        res = self._run("visible")
        if not res.get("ok"):
            raise ElementOperatorError(f"{self.label} 可见性判断失败：{res.get('error')}")
        return bool(res.get("visible")) if res.get("found") else False

    def is_enabled(self) -> bool:
        res = self._run("enabled")
        if not res.get("ok"):
            raise ElementOperatorError(f"{self.label} 可用性判断失败：{res.get('error')}")
        return bool(res.get("enabled")) if res.get("found") else False

    def is_editable(self) -> bool:
        st = self.state() or {}
        return bool(st.get("editable"))

    # ---------------- 等待 ----------------
    def wait_loaded(self, timeout: float = DEFAULT_TIMEOUT,
                    require_visible: bool = True,
                    interval: float = POLL_INTERVAL) -> bool:
        """等待元素“加载完毕”：出现在 DOM，且（默认）渲染可见。

        适用场景：页面异步渲染、点击后弹窗、列表懒加载。
        """

        def ready() -> bool:
            st = self.state()
            if not st:
                return False
            return bool(st.get("visible")) if require_visible else True

        return self._wait(ready, timeout, interval,
                          "元素可见" if require_visible else "元素出现")

    def wait_visible(self, timeout: float = DEFAULT_TIMEOUT) -> bool:
        return self.wait_loaded(timeout=timeout, require_visible=True)

    def wait_gone(self, timeout: float = DEFAULT_TIMEOUT,
                  interval: float = POLL_INTERVAL) -> bool:
        """等待元素消失（弹窗关闭、loading 结束）。"""
        return self._wait(lambda: self.count() == 0, timeout, interval, "元素消失")

    def wait_text(self, expected: str, timeout: float = DEFAULT_TIMEOUT,
                  contains: bool = True, interval: float = POLL_INTERVAL) -> bool:
        """等待文本变成期望值（默认包含匹配）。"""
        expected = str(expected)

        def matched() -> bool:
            actual = self.text()
            return expected in actual if contains else actual == expected

        return self._wait(matched, timeout, interval, f"文本包含 {expected!r}")

    def wait_value(self, expected: str, timeout: float = DEFAULT_TIMEOUT,
                   interval: float = POLL_INTERVAL) -> bool:
        expected = str(expected)
        return self._wait(lambda: self.value() == expected, timeout, interval,
                          f"值为 {expected!r}")

    # ---------------- 移动 / 定位信息 ----------------
    def scroll_into_view(self, block: str = "center") -> dict:
        """移动至可见：滚动页面（含滚动容器），让元素进入视口。"""
        res = self._require("scroll", block=block)
        return res.get("rect") or {}

    # 语义化别名
    move_to_view = scroll_into_view

    def rect(self) -> dict:
        """元素在视口中的矩形 {x, y, width, height, right, bottom}。"""
        res = self._require("rect")
        return res.get("rect") or {}

    def page_rect(self) -> dict:
        """元素在文档坐标系中的矩形（含滚动偏移）。"""
        res = self._require("rect")
        return res.get("page_rect") or {}

    def in_viewport(self) -> bool:
        res = self._require("rect")
        return bool(res.get("in_viewport"))

    # ---------------- 读取 ----------------
    def text(self) -> str:
        """元素可见文本（innerText，已 trim）。"""
        return str(self._require("text").get("text") or "")

    def text_content(self) -> str:
        """textContent（含隐藏文字，不受 CSS 影响）。"""
        return str(self._require("text").get("text_content") or "")

    def value(self) -> str:
        """输入框 / textarea / contenteditable 的当前值。"""
        return str(self._require("value").get("value") or "")

    def attr(self, name: str) -> str | None:
        """读取单个属性；属性不存在返回 None。"""
        res = self._require("attr", name=name)
        return res.get("value")

    def attrs(self) -> dict:
        """读取全部属性。"""
        return dict(self._require("attrs").get("attributes") or {})

    def html(self) -> str:
        """outerHTML（最长 200000 字符）。"""
        return str(self._require("html").get("html") or "")

    # ---------------- 交互 ----------------
    def click(self, timeout: float = DEFAULT_TIMEOUT, mouse: bool = False,
              force: bool = False) -> None:
        """点击元素。

        mouse=False：页面内 `el.click()`，对绝大多数元素有效（默认）。
        mouse=True ：用 CDP 在元素中心派发真实坐标鼠标事件，适合 canvas / 自绘控件。
        force=True ：跳过“等待可见”，直接点（元素可能被遮挡时用）。
        """
        if not force:
            self.wait_loaded(timeout)
        self.scroll_into_view()
        if mouse:
            r = self.rect()
            self.op.session.click_at(r["x"] + r["width"] / 2.0,
                                     r["y"] + r["height"] / 2.0)
            return
        self._require("click")

    def double_click(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """双击（用两次坐标点击，间隔很短）。"""
        self.wait_loaded(timeout)
        self.scroll_into_view()
        r = self.rect()
        x, y = r["x"] + r["width"] / 2.0, r["y"] + r["height"] / 2.0
        px, py = int(round(x)), int(round(y))
        self.op.session.call("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py)
        for click_count in (1, 2):
            self.op.session.call("Input.dispatchMouseEvent", type="mousePressed",
                                 x=px, y=py, button="left", clickCount=click_count)
            self.op.session.call("Input.dispatchMouseEvent", type="mouseReleased",
                                 x=px, y=py, button="left", clickCount=click_count)

    def hover(self, timeout: float = DEFAULT_TIMEOUT, mouse: bool = False) -> None:
        """鼠标悬停（触发 mouseover / mouseenter / mousemove）。"""
        self.wait_loaded(timeout, require_visible=False)
        self.scroll_into_view()
        if mouse:
            r = self.rect()
            self.op.session.move_to(r["x"] + r["width"] / 2.0,
                                    r["y"] + r["height"] / 2.0)
            return
        self._require("hover")

    def focus(self) -> None:
        self._require("focus")

    def blur(self) -> None:
        self._require("blur")

    def input_text(self, text: str, clear: bool = True,
                   timeout: float = DEFAULT_TIMEOUT) -> str:
        """向输入框写入文本，返回写入后的值。

        clear=True 覆盖原内容，False 追加。
        内部走原型上的原生 value setter，React / Vue 受控组件也能感知到变化。
        """
        if not self.is_editable():
            self.wait_loaded(timeout, require_visible=False)
            if not self.is_editable():
                raise ElementNotInteractable(f"{self.label} 不是可输入元素（或已禁用/只读）")
        self.scroll_into_view()
        res = self._require("input", text=str(text), clear=bool(clear))
        return str(res.get("value") or "")

    def clear(self) -> str:
        """清空输入框，返回清空后的值。"""
        res = self._require("clear")
        return str(res.get("value") or "")

    def select_option(self, value=None, label=None, index=None,
                      timeout: float = DEFAULT_TIMEOUT) -> str:
        """选择 <select> 的选项，返回选中项的 value。三个参数任选一个。"""
        if value is None and label is None and index is None:
            raise ElementOperatorError("select_option 需要 value / label / index 之一")
        self.wait_loaded(timeout, require_visible=False)
        params = {}
        if value is not None:
            params["value"] = value
        elif label is not None:
            params["label"] = label
        else:
            params["index"] = int(index)
        res = self._require("select", **params)
        return str(res.get("value") or "")

    def check(self) -> bool:
        """勾选 checkbox / radio，返回最终 checked 状态。"""
        return bool(self._require("check").get("checked"))

    def uncheck(self) -> bool:
        """取消勾选，返回最终 checked 状态。"""
        return bool(self._require("uncheck").get("checked"))

    def press(self, key: str = "Enter") -> None:
        """在元素上派发一次按键（keydown/keypress/keyup）。

        注意：合成按键不会触发浏览器默认行为（例如 Enter 提交表单），
        需要真实默认行为时请用 `submit()` 或坐标点击。
        """
        self._require("press", key=str(key))

    def submit(self) -> None:
        """提交元素所在表单。"""
        self._require("submit")

    def highlight(self, duration_ms: int = 1500) -> None:
        """在页面上高亮该元素（用于调试定位是否正确）。"""
        self._require("highlight", duration=int(duration_ms))


# --------------------------------------------------------------------------- #
# 对外主类
# --------------------------------------------------------------------------- #
class ElementOperator:
    """元素库 + 页面会话的组合入口。

    典型用法::

        op = ElementOperator(port=9223, library="elements.json").connect()
        op.element("main_page", "login_button").click()
        op.group("main_page")                    # 列出该组元素名
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 library: str | None = None, session: CdpSession | None = None):
        self.session = session or CdpSession(host=host, port=port)
        self.library = ElementLibrary(library) if library else ElementLibrary()
        self._own_session = session is None

    # ---------------- 连接 ----------------
    def connect(self, page=None) -> "ElementOperator":
        """连接页面并返回自身，方便链式调用。page 见 CdpSession.connect。"""
        self.session.connect(page)
        return self

    def close(self) -> None:
        if self._own_session:
            self.session.close()

    def __enter__(self) -> "ElementOperator":
        if self.session.chrome is None:
            self.connect()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---------------- 元素库 ----------------
    def load_library(self, path: str) -> "ElementOperator":
        self.library.load(path)
        return self

    def reload_library(self) -> "ElementOperator":
        self.library.reload()
        return self

    def groups(self) -> dict:
        return self.library.groups()

    def group(self, name: str) -> list:
        """某个分组下的元素名列表。"""
        return self.library.names(name)

    # ---------------- 找元素 ----------------
    def element(self, group: str, name: str | None = None) -> Element:
        """按「分组 + 名称」取元素；也支持 `element("group/name")` 简写。"""
        if name is None:
            text = str(group)
            if "/" not in text:
                found = self.library.find(text)
                if len(found) == 1:
                    return Element(self, found[0])
                if len(found) > 1:
                    groups = ", ".join(str(x.get("group_name") or DEFAULT_GROUP) for x in found)
                    raise ElementOperatorError(
                        f"元素名 {text!r} 在多个分组中存在（{groups}），请用 element('分组', '名称')")
                raise ElementNotFound(f"元素库里没有名为 {text!r} 的元素")
            group, _, name = text.partition("/")
        return Element(self, self.library.require(str(group).strip(), str(name).strip()))

    def find(self, name: str) -> list:
        """按名字跨分组搜索，返回全部匹配的 Element。"""
        return [Element(self, item) for item in self.library.find(name)]

    # ---------------- 执行 ----------------
    def _exec(self, definition: dict, action: str, **params) -> dict:
        return exec_action(self.session, definition, action, **params)

    @staticmethod
    def _checked_of(definition: dict) -> list:
        return checked_layers(definition)
