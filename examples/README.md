# 示例

全部示例都在本目录，任务入口用 `@task` 注册，端口自动分配或显式指定。

| 文件 | 说明 | 是否需要网络 |
|---|---|---|
| `demo_page.html` | 演示用的本地页面 | 否 |
| `demo_lib.json` | 针对本地页面的元素库 | 否 |
| `demo_local_page.py` | 输入 → 点击 → 读取结果 | 否 |
| `bilibili_lib.json` | B 站首页搜索框 / 按钮的元素库 | 否 |
| `demo_web_search.py` | 打开 B 站并提交搜索 | 是 |
| `run_parallel.py` | 多进程入口：扫描本目录的 `@task` | — |

## 运行

```bash
# 列出任务和端口
python examples/run_parallel.py --list

# 只跑离线示例（无头浏览器）
python examples/run_parallel.py --only demo_local

# 跑全部（demo_web 需要网络，会打开浏览器窗口）
python examples/run_parallel.py
```

也可以单独运行任一任务脚本：

```bash
python examples/demo_local_page.py
python examples/demo_web_search.py
```

## 新增一个任务

在本目录新建 `my_task.py`：

```python
from cdp_flow import Browser
from cdp_flow.parallel import task

@task(port=9224)
def my_task(port: int = 9224) -> None:
    browser = Browser.launch(port=port, library="path/to/library.json")
    try:
        tab = browser.new_tab("https://example.com/")
        tab.group("main").element("btn_submit").click()
    finally:
        browser.close(kill=True)

if __name__ == "__main__":
    my_task(my_task.port)
```

`run_parallel.py` 会自动发现它，不需要改入口。

## 元素库怎么来

用捕获器生成：`python capture/element_capture_qt.py` → 打开元素库 JSON → 连接浏览器 →
「开始拾取」或「截图拾取」→ 勾选稳定的 `id` / `class` 规则 → 保存到本目录。

示例里的 `demo_lib.json` 只勾了最后一层，并用 `id` 作为唯一规则，便于阅读和手写。
