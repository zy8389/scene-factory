# 开发与浏览器验证

## Python 核心测试

先按 README 的 `tools/bootstrap_uv.ps1` 同步开发环境，再在仓库根目录执行：

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m ruff check scene_factory tools tests
```

Windows 也可以双击 `tools/test_local.cmd`。该入口优先使用仓库 `.venv`，
禁用 LLM，运行 pytest（包括函数式测试），不要求 Isaac 或 ASCII 输出目录。
通过 `local_stack.ps1 -Action Test -IsaacPython <python>` 可指定测试解释器。
Linux 使用 `.venv/bin/python -m pytest`。测试数量以 `python -m pytest --collect-only`
为准；可选运行时缺失的跳过项须与真正通过的测试分开记录。

`tools` 是仓库内的 Python 包，避免被其他项目安装的同名包遮蔽；
维护脚本不属于 wheel 的公共 SDK。

## 可移植浏览器验收

Node.js 22 或更新版本；首次安装依赖和 Chromium 需要网络：

```powershell
npm ci
npx playwright install chromium
.venv\Scripts\python.exe tools/run_web_qa.py
```

Linux 新机器可使用 `npx playwright install --with-deps chromium` 安装系统依赖。
Playwright 版本由 `package-lock.json` 固定。执行入口会禁用 LLM，使用临时目录和
随机本地端口启动独立服务，结束时关闭服务并清理临时场景。它不提交 Isaac 任务。
检查覆盖桌面/移动端生成、GLB/WebGL 预览、相机交互、二维切换和场景包下载；
截图与下载样本保存在 `outputs/web-qa`，可用 `--output` 指定其他目录。
CI 的 `Offline browser preview` 作业执行同一入口并保存检查产物。

## 已有服务与 Isaac 专项验收

以下脚本使用已经运行的服务，默认地址为 `http://127.0.0.1:8765/`：

| 命令 | 前置条件 |
| --- | --- |
| `npm run qa:preview` | Web 服务；不要求 Isaac |
| `npm run qa:kitchen` | Web 服务及可用 USD 导出；检查餐厨配方 |
| `npm run qa:runtime` | Isaac 运行服务和已登记的历史对照数据；默认检查 tracking 40 次成功、baseline 4 次成功/36 次失败 |
| `npm run qa:current-scene` | Isaac 运行服务；设置 `SCENE_FACTORY_QA_PROMPT` 指定厨房单杯输入 |

`qa:runtime` 的 `SCENE_FACTORY_RUNTIME_QA_MODE=submit` 会提交一个布局的运行任务；
`qa:current-scene` 的 `SCENE_FACTORY_CURRENT_SCENE_QA_MODE=submit` 会提交当前场景。
后者设为 `inspect` 时须提供 `SCENE_FACTORY_RUNTIME_QA_JOB_ID`。
这些依赖环境和真实运行证据的验收不包含在通用浏览器 CI 中。

可选环境变量：

- `SCENE_FACTORY_QA_URL`：已有服务地址，四个脚本均支持。
- `SCENE_FACTORY_QA_OUTPUT`：专项脚本产物目录。
- `SCENE_FACTORY_BROWSER`：已有 Chromium/Chrome 可执行文件；兼容旧变量 `SCENE_FACTORY_CHROME`。
- `SCENE_FACTORY_PLAYWRIGHT`：已有 Playwright 模块路径；不设置时使用 `npm ci` 安装的本地依赖。
