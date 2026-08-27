# SceneFactory LLM 接入

SceneFactory 将模型输出限制为 `SceneIntent`。LLM 负责房间、事件、物体和语义关系；
确定性编译器负责选择资产类别、支撑面、精确位姿、碰撞检查和 USD 导出。

## 接口具体接在哪里

先从公开模板创建本地配置：

```powershell
Copy-Item config\llm.example.json config\llm.json
```

例如，接入任意提供 OpenAI-compatible `POST /chat/completions` 的服务：

```json
{
  "mode": "auto",
  "base_url": "https://your-provider.example/v1",
  "model": "your-model-name",
  "api_key_env": "SCENE_FACTORY_LLM_API_KEY",
  "timeout_seconds": 60,
  "ca_bundle": "system",
  "transport": "urllib",
  "proxy_url": "",
  "cache_dir": "../.cache/llm_intents"
}
```

不要把密钥写入 JSON。打开 PowerShell，在启动网页的同一个窗口里执行：

```powershell
$env:SCENE_FACTORY_LLM_API_KEY = "你的密钥"
powershell -ExecutionPolicy Bypass -File tools\start_web.ps1 -Restart
```

然后打开 `http://127.0.0.1:8765/`。页面左侧会显示模型和 endpoint；点击“测试连接”会
发送一次真实的结构化请求。修改配置或密钥后必须重启网页服务。

## 本地模型服务

本地服务通常不需要 API key：

```json
{
  "mode": "auto",
  "base_url": "http://127.0.0.1:8000/v1",
  "model": "local-model",
  "api_key_env": "SCENE_FACTORY_LLM_API_KEY",
  "timeout_seconds": 120,
  "ca_bundle": "system",
  "transport": "urllib",
  "proxy_url": "",
  "cache_dir": "../../scene_factory_runtime/llm_cache"
}
```

这里假设本地模型服务已经提供 OpenAI-compatible Chat Completions API。SceneFactory 不会
自动下载或启动模型。

## 环境变量覆盖

支持提供 `POST /chat/completions` 的结构化 JSON 接口。不要把密钥写入项目文件，使用
当前终端的环境变量：

```powershell
$env:SCENE_FACTORY_LLM_MODE = "auto"
$env:SCENE_FACTORY_LLM_BASE_URL = "https://your-provider.example/v1"
$env:SCENE_FACTORY_LLM_MODEL = "your-structured-output-model"
$env:SCENE_FACTORY_LLM_API_KEY = "..."
```

`SCENE_FACTORY_LLM_MODE` 支持：

- `auto`：配置完整时调用 LLM；调用、Schema 或布局失败时回退关键词配方；
- `off`：始终使用离线关键词配方；
- `required`：必须成功使用 LLM，失败则返回错误，适合验收环境。

可选配置：

```powershell
$env:SCENE_FACTORY_LLM_TIMEOUT_SECONDS = "60"
$env:SCENE_FACTORY_LLM_CACHE = ".\runtime-work\llm_cache"
$env:SCENE_FACTORY_LLM_CA_BUNDLE = "certifi"
$env:SCENE_FACTORY_LLM_TRANSPORT = "curl_schannel"
$env:SCENE_FACTORY_LLM_PROXY_URL = "http://proxy.example:8080"
```

`ca_bundle` 可填 `system`、`certifi` 或本地 CA PEM 文件路径。不要使用关闭 TLS 校验的
配置；如果服务商证书无法通过 `certifi` 校验，应向服务商索取正确证书链或 CA 文件。

`transport=urllib` 是跨平台默认值。只有在 Windows 系统证书栈是部署要求时才使用
`curl_schannel`；它仍校验证书链和主机名。`proxy_url` 默认留空，需要代理的部署应在
本地 `config/llm.json` 或环境变量中显式设置，公开仓库不携带个人 endpoint/proxy。

## 查看状态

```powershell
python -m scene_factory llm-status
python -m scene_factory llm-test
```

网页接口：

- `GET /api/llm/status`：安全返回配置状态，不返回密钥；
- `POST /api/llm/test`：真实调用一次 LLM 并校验返回的 `SceneIntent`；
- `POST /api/generate`：正常生成场景。
- `POST /api/revise`：对一个已有的 LLM 场景做增量修改并生成新版本。

每个生成结果包含 `prompt_parser` 和 `parser_warning`：

- `llm:<model>`：结构化 LLM 解析成功；
- `keyword`：未配置 LLM；
- `keyword_fallback`：LLM 或语义布局失败，已自动降级。

当出现 `keyword_fallback` 时，页面会显示具体降级原因，不再静默吞掉错误。

## 缓存和批量生成

缓存键包含 endpoint、模型、类别集合、事件集合和完整提示词。同一句需求批量生成时只
调用一次 LLM，后续 seed 复用 `SceneIntent`，但资产选择和布局仍可变化。

## 输出文件

LLM 成功时，场景目录会增加 `scene_intent.json`：

```text
自然语言
  -> scene_intent.json
  -> scene_spec.json
  -> layout.json
  -> scene.usd
```

Schema 位于 `schemas/scene_intent.schema.json`。当前只允许注册表已有类别及
`on`、`near`、`partly_occluded_by` 三种关系，避免模型虚构资产和不可执行关系。

增量修改同样受这份 Schema 约束。服务会把原始 `SceneIntent` 和修改指令一起发送，
并要求模型返回完整的新意图，而不是难以校验的 JSON Patch。每个修改版本额外包含
`revision.json`，记录 `source_scene_id`、修改指令与解析器名称。
