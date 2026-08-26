# SceneFactory 本地无下载部署

这套部署只复用当前电脑已经存在的代码、Isaac Python、OpenUSD/pxr 和 Isaac Sim，
不会下载资产包、模型或 Python 依赖。

## 本地可运行的部分

| 组件 | 本地状态 | 是否需要新下载 |
| --- | --- | --- |
| 自然语言网页与 REST API | 可运行 | 否 |
| LLM 场景生成与增量修改 | 可运行；调用已经配置的远端 API | 否 |
| 离线事件配方 | 可运行 | 否 |
| 布局、支撑关系和碰撞校验 | 可运行 | 否 |
| SVG 俯视预览 | 可运行 | 否 |
| USD 导出 | 使用现有 Isaac Python | 否 |
| OpenUSD/pxr 验证 | 使用现有 Isaac Python | 否 |
| Isaac Sim/PhysX Headless 验收 | 使用现有 Isaac Sim 6.0.1 | 否 |
| 批量场景制造 | 可运行 | 否 |
| 四个 ready YCB 真实资产 | 可运行；仓库已包含固定 revision 与 attribution | 否 |
| Franka mug-lift manual gate | 可运行；当前真实结果为 grasp failure | 否 |

## 最简单的启动方式

双击：

```text
tools\start_local.cmd
```

服务会在后台启动，并打开：

```text
http://127.0.0.1:8765/
```

双击 `tools\stop_local.cmd` 可以安全停止。停止脚本只会结束命令行中包含
`scene_factory.webapp` 的进程，不会占用或误停其他程序。

## PowerShell 管理命令

在项目根目录运行：

```powershell
# 启动
powershell -ExecutionPolicy Bypass -File tools\local_stack.ps1 -Action Start -OpenBrowser

# 查看状态
powershell -ExecutionPolicy Bypass -File tools\local_stack.ps1 -Action Status

# 重启
powershell -ExecutionPolicy Bypass -File tools\local_stack.ps1 -Action Restart

# 停止
powershell -ExecutionPolicy Bypass -File tools\local_stack.ps1 -Action Stop

# 完全离线测试，不调用 LLM
powershell -ExecutionPolicy Bypass -File tools\local_stack.ps1 -Action Test

# 完全离线生成一个 USD 并用 pxr 验证
powershell -ExecutionPolicy Bypass -File tools\local_stack.ps1 -Action Demo
```

## LLM 密钥

脚本不会读取配置文件中的明文密钥，也不会把密钥写进 PID、状态或日志文件。若要使用
LLM，应在启动服务的同一个 PowerShell 会话中设置：

```powershell
$env:SCENE_FACTORY_LLM_API_KEY = "你的密钥"
powershell -ExecutionPolicy Bypass -File tools\local_stack.ps1 -Action Restart -OpenBrowser
```

只使用离线配方时不需要密钥。

## 本地目录

```text
F:\scene_factory_runtime\web          网页生成结果
F:\scene_factory_runtime\local_stack  PID 状态和服务日志
F:\scene_factory_runtime\local_demo   离线 USD 演示
```

网页只监听 `127.0.0.1`，没有登录认证，也不会暴露到局域网或公网。
