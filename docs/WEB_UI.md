# SceneFactory 自然语言可视化界面

界面默认地址：

```text
http://127.0.0.1:8765
```

## 启动

使用一键脚本启动。脚本优先使用项目 `.venv` 中的 Python；MuJoCo 仅在使用
`SceneFactoryEnv` 物理环境时需要。如需从页面导出 USD，可通过 `-IsaacPython` 指定能
导入 `pxr` 的 Isaac Python：

```powershell
cd <scene-factory-checkout>
powershell -ExecutionPolicy Bypass -File tools\start_web.ps1 -Restart

# 可选：让页面导出 USD
powershell -ExecutionPolicy Bypass -File tools\start_web.ps1 -Restart -IsaacPython <isaac_python.exe>
```

## 使用方法

1. 在“自然语言需求”中输入中文描述；
2. 设置 seed；同一 seed 会稳定复现同一布局；
3. “生成变体”可填 1–12，用连续 seed 生成多种摆法；
4. 按需要勾选 USD，然后点击“生成仿真场景”；
5. 页面会显示匹配配方、俯视预览、校验结果、物体位姿和下载链接；
6. 点击“导出场景包”下载完整 `.scene.zip`。

左侧的 LLM 状态卡会读取 `config/llm.json`。配置模型后，“测试连接”会发送一次真实
结构化请求；未配置或调用失败时，系统会显示离线关键词模式及降级原因。

生成文件保存在 `outputs\web\<scene_id>`。

场景包包含 SceneSpec、布局、校验报告、SVG、MuJoCo MJCF、可选 USD 和实际使用的
本地 GLB。`manifest.json` 记录格式版本、后端说明、文件大小和每个文件的 SHA-256。
MuJoCo 文件使用包围盒碰撞代理，GLB 用于 Three.js 视觉预览，Isaac USD 仍是可选
高保真产物。

Three.js 会读取每个 GLB 来源清单中的轴向变换，并以等比缩放、底部对齐方式放入场景；
它不会为了填满碰撞代理而拉伸模型。厨房预览还包含地板、两面墙、挡板和局部顶灯，便于
观察家具与餐厨物体在室内空间中的比例。

## 当前语言能力

当前界面已可以识别并可视化三类事件：

- 刚做完饭的厨房；
- 刚在客厅吃完零食；
- 刚回到家的入口区域。

未配置 LLM 时使用“事件配方匹配 + 约束布局”。配置 LLM 后，模型会先将需求解析为
`SceneIntent`，再由确定性编译器生成 SceneSpec、布局和 USD。

## 页面接口

- `GET /api/health`：网页进程状态和当前解析器；
- `GET /api/llm/status`：LLM 配置状态，不包含密钥；
- `POST /api/llm/test`：真实发送一次结构化 LLM 测试；
- `POST /api/generate`：生成一个或多个场景；
- `POST /api/revise`：读取已有 `scene_intent.json`，按自然语言要求生成一个新版本；
- `POST /api/open-isaac`：在 Isaac Sim 中打开已导出的 USD。

## 继续修改场景

LLM 场景生成成功后，预览下方会出现“继续修改当前场景”。只需描述变化，例如：

```text
删掉一个杯子，把背包移到沙发旁边，再在茶几上加一张纸巾。
```

系统会把当前 `scene_intent.json` 和修改要求一起发给 LLM，要求模型返回完整的新
`SceneIntent`。未提到的物体与关系会保留，修改结果写入新的场景目录，同时生成
`revision.json` 记录来源场景。原版本不会被覆盖，可以连续修改多轮。
