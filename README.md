# CDP Processing Tool

基于 Chrome DevTools Protocol 的网页元素拾取 + 流程编排 + 多进程执行工具。

不写 CSS/XPath，也不依赖 Selenium：用捕获器在浏览器里点选元素，存成 JSON 元素库；脚本里按「分组 + 元素名」操作元素，页面重渲染后仍然有效。

## 组成

| 目录 | 作用 |
|---|---|
| `capture/` | 元素捕获器（PyQt5 桌面程序）：连接浏览器、点选元素、勾选定位规则、保存元素库 |
| `cdp_flow/` | 流程编排库：`Browser` / `Tab` / `Element`，按元素库定位并操作页面 |
| `cdp_flow/parallel.py` | 多进程执行：`@task` 注册任务，一个任务一个端口、一个浏览器进程 |
| `utils/` | 通用工具：`log()` / `setup()`，像 print 一样用，日志目录可指定 |
| `examples/` | 可直接运行的示例（离线页面 + 真实网站 + 多进程） |

## 目录结构

```
cdp_processing_tool/
├── capture/
│   └── element_capture_qt.py     # 捕获器（独立运行，PyQt5）
├── cdp_flow/
│   ├── __init__.py               # 对外导出 Browser / Tab / Element / task
│   ├── element_operator.py       # 元素库、定位、元素操作、CDP 会话
│   ├── browser_manager.py        # 按端口管理浏览器、标签页、分组视图
│   └── parallel.py               # @task 注册 + 多进程调度
├── utils/
│   ├── __init__.py
│   └── logger.py                 # log() / setup()：日志目录可指定
├── examples/
│   ├── demo_page.html            # 离线演示页面
│   ├── demo_lib.json             # 离线示例元素库
│   ├── demo_local_page.py        # 离线示例任务
│   ├── bilibili_lib.json         # 真实网站示例元素库
│   ├── demo_web_search.py        # 真实网站示例任务
│   ├── run_parallel.py           # 多进程入口
│   └── README.md
├── README.md
├── requirements.txt
└── .gitignore
```

## 环境要求

- Python 3.9+
- Chrome 或 Edge（CDP 走 `--remote-debugging-port`）
- 捕获器需要图形界面（Windows / macOS / Linux 桌面环境均可）

## 安装

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

## 快速开始

### 1. 捕获元素

```bash
python capture/element_capture_qt.py
```

1. 「新建 / 打开」元素库 JSON；
2. 填端口（默认 9223）→「连接」→ 选择页面；
3. 「开始拾取」→ 在浏览器里单击目标元素；
   Chrome 窗口在后台或被挡住时，改用「截图拾取」；
4. 在编辑窗口勾选参与定位的层和属性规则，点「统计匹配数量」确认只命中目标；
5. 填元素名、选分组 → 保存。

优先使用稳定的 `id` / `class` / `name` 作为规则；`placeholder`、`value`、`title` 这类属性可能被页面脚本改写，尽量不用。

### 2. 用元素库编排流程

```python
from pathlib import Path

from cdp_flow import Browser

browser = Browser.launch(
    port=9222,
    library="examples/demo_lib.json",
    headless=True,
)

try:
    tab = browser.new_tab(Path("examples/demo_page.html").resolve().as_uri())
    group = tab.group("demo")

    group.element("search_input").input_text("hello cdp")
    group.element("btn_search").click()
    print(group.element("result_text").text())
finally:
    browser.close(kill=True)
```

也可以不绑定分组：

```python
tab.element("demo", "search_input")      # 分组 + 元素名
tab.element("demo/search_input")         # 同上，斜杠写法
tab.element("search_input")              # 全库唯一时可省略分组
```

### 3. 多进程执行

给任务函数加 `@task`，入口脚本用 `cdp_flow.parallel.main()`：

```python
from cdp_flow import Browser
from cdp_flow.parallel import task

@task(port=9222)                 # 不写 port 会自动分配 9222、9223...
def my_task(port: int = 9222) -> None:
    browser = Browser.launch(port=port, library="examples/demo_lib.json")
    try:
        tab = browser.new_tab("https://example.com/")
        tab.group("main").element("btn_submit").click()
    finally:
        browser.close(kill=True)

if __name__ == "__main__":
    my_task(my_task.port)
```

```bash
python examples/run_parallel.py              # 扫描入口所在目录，每个任务一个进程
python examples/run_parallel.py --list
python examples/run_parallel.py --only demo_local
python examples/run_parallel.py --dry-run
```

规则：

- 一个任务 = 一个端口 = 一个浏览器进程，端口不能重复；
- 任务函数必须是**模块级函数**，只接收一个 `port`（子进程按「模块名 + 函数名」重新导入）；
- 浏览器 profile 默认按端口分开（`%LOCALAPPDATA%\ElementCapture\profile-<端口>`），互不影响。

### 4. 日志

不需要建 logger，也不需要写 `logger.info()` 这种长名字，像 `print` 一样用：

```python
from utils import log, setup, warning, error

setup(log_dir="D:/rpa-logs")     # 可选；不调用就默认写到项目根 logs/
log("普通信息")
warning("警告")
error("错误")
```

- 目录优先级：`setup(log_dir=...)` > 环境变量 `CDP_LOG_DIR` > 项目根 `logs/`；
- 文件名 `cdp-<pid>.log`，多进程各写各的，不会互相抢文件；
- 单文件 5MB 轮转，保留 3 个备份；
- `setup()` 会顺带把 `cdp_flow` 内部的连接/定位日志接到同一份输出；
- `log()` 同时支持 print 风格和 `%s`：

```python
log("a", "b")          # a b
log("value=%s", 42)    # value=42
```

不调用 `setup()` 也能直接用，第一次写日志时会自动初始化。

## 元素库格式

元素库是一个 JSON 数组，每条记录对应一个元素：

```json
{
  "name": "search_input",
  "group_name": "demo",
  "node_path": [
    {"tag": "html", "attributes": {}, "index": 0, "sibling_count": 1, "innerText": ""},
    {"tag": "body", "attributes": {}, "index": 1, "sibling_count": 2, "innerText": ""},
    {"tag": "input", "attributes": {"id": "kw"}, "index": 0, "sibling_count": 3, "innerText": ""}
  ],
  "checked_nodes": [2],
  "node_match_rules": {
    "2": {"id": ["等于", "kw", true]}
  }
}
```

- `node_path`：从 `html` 到目标元素的层级链；
- `checked_nodes`：参与定位的层索引（示例只勾最后一层）；
- `node_match_rules`：每层的属性规则 `[匹配方式, 值, 是否启用]`；
- 定位从 `document` 开始，逐层按 `tag` 收集后代，再用该层启用的规则过滤；勾选层越少、规则越精确，页面改版后越容易维护。

## API 速查

### Browser

| 方法 | 说明 |
|---|---|
| `Browser.launch(port, url=None, library=None, headless=False, window_size=None)` | 确保端口有浏览器并连接 |
| `.new_tab(url)` | 新建标签页并设为当前页 |
| `.get_tab(title=...)` / `.get_tab(url=...)` | 按标题 / URL 子串查找标签页 |
| `.switch(tab)` | 切换当前标签页 |
| `.element(group, name)` | 当前标签页上的元素 |
| `.group(name)` | 当前标签页的分组视图 |
| `.close(kill=True)` | 断开连接；`kill` 结束本次启动的浏览器 |

### Tab

| 方法 | 说明 |
|---|---|
| `.element(group, name)` / `.group(name)` / `.find(name)` | 元素 / 分组视图 / 按名字搜索 |
| `.goto(url)` / `.reload()` / `.back()` | 导航 |
| `.wait_ready(timeout=10)` | 等 `document.readyState === 'complete'` |
| `.activate(front=True)` | 激活标签页 |
| `.is_alive()` / `.close()` | 页面是否还在 / 关闭标签页 |
| `.session.evaluate(js)` / `.session.call(method, **params)` | 直接执行 JS / 发 CDP 命令 |

### Element

| 方法 | 说明 |
|---|---|
| `.click(mouse=False)` | 点击；svg / 自绘控件用 `mouse=True`（CDP 坐标点击） |
| `.input_text(text, clear=True, timeout=5)` | 写入文本 |
| `.text()` / `.value()` / `.attr(name)` | 读取文本 / 输入值 / 属性 |
| `.count()` / `.exists(timeout=0)` | 匹配数量 / 是否存在 |
| `.wait_loaded(timeout=5, require_visible=True)` | 等元素出现（提前返回，不是固定等待） |
| `.scroll_into_view()` / `.highlight()` | 滚动到可见 / 页面高亮 |

## 示例

```bash
# 离线：本地页面输入 → 点击 → 读取结果
python examples/run_parallel.py --only demo_local

# 真实网站：B 站首页搜索（需要网络，会打开浏览器窗口）
python examples/run_parallel.py --only demo_web
```

更多说明见 `examples/README.md`。

## 常见问题

**连接不上浏览器**

确认浏览器是用 `--remote-debugging-port` 启动的；`Browser.launch()` 在本机没有可用调试端口时会自己启动 Chrome。

**元素定位超时**

先用 `element.count()` 看命中数量，再用 `element.highlight()` 在页面上确认位置；规则不对时用捕获器重新拾取，优先 `id` / `class`。

**点击 svg / 图标没有反应**

改用 `element.click(mouse=True)`，走 CDP 坐标点击，事件会冒泡到父级监听器。

**页面加载慢**

`wait_loaded(timeout=20)` 是上限，元素一出现就返回；真正要等的是元素，不是整页 `readyState`。

**多进程端口冲突**

一个端口只能对应一个浏览器；`@task` 自动分配端口，显式指定时不要重复。

## 提交前检查（重要）

- 元素库里的 `target_page`、`outer_html`、`innerText` 可能包含页面 URL、业务数据、个人信息，提交前检查或脱敏；
- 不要把账号、密码、Cookie、Token 写进脚本或元素库；
- `%LOCALAPPDATA%\ElementCapture` 是浏览器运行时数据，不要提交；
- 示例目录只保留公开页面和演示数据。
