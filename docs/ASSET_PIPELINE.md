# 离线 USD 资产接入

这套流程只处理你已经放在本机的 USD 文件，**不会联网，也不会下载任何资产**。它解决的是“一个模型文件怎样安全进入 SceneFactory”，而不是资产获取。

## 目录建议

```text
F:\scene_factory_assets\
├── incoming\       原始 USD，只读保留
├── wrapped\        统一为 Z-up、米制、中心原点后的 USD
├── reports\        检查和 PhysX 报告
└── records\        等待加入资产库的 JSON 记录
```

Windows 上 Isaac Sim/OpenUSD 对中文路径兼容性不稳定，因此 USD 和报告目录使用纯 ASCII 路径。

## 资产状态

```text
本地 USD → inspect → wrap → quarantine → PhysX 跌落测试 → validated → 正式资产库
```

- `quarantine`：可以检查，但不会被场景生成器选中。
- `validated`：OpenUSD 结构与 PhysX 测试通过，可以加入 `data/assets/registry.jsonl`。
- `rejected`：尺寸、几何或物理测试存在问题，暂不使用。

## Registry v2

正式 registry 仍然是 JSONL，一行对应一个资产。项目同时接受历史的扁平
`AssetRecord`（例如 `bbox_m`、`source_path`、`mass_kg`）和 v2 字段：

```text
data/assets/
├── registry.jsonl       资产元数据索引
├── source/              原始资产（不由仓库自动下载）
├── usd/                 经过标准化的可引用 USD
├── collision/           独立碰撞网格（可选）
├── metadata/            扩展元数据（可选）
└── qa_reports/          Asset Validator 输出（可选）
```

v2 记录的核心字段如下。`bbox_m` 对真实 USD 可以省略，Validator 会在 USD
检查后给出尺寸结果；如果资产要参与现有布局生成，建议显式填写它。

```json
{
  "asset_id": "mug_blue",
  "name": "Blue ceramic mug",
  "category": "mug",
  "source": "internal_capture",
  "license": "internal",
  "hash": "sha256:...",
  "usd_path": "usd/mug_blue.usda",
  "collision_path": "collision/mug_blue.usda",
  "bbox_m": [0.09, 0.09, 0.11],
  "mass": 0.32,
  "friction": 0.48,
  "support_surface": [],
  "grasp_region": {"type": "center", "radius_m": 0.025},
  "status": "quarantine"
}
```

`AssetRegistry.load()` 会将 v2 字段归一化为兼容的 `AssetRecord`，因此
`SceneIntent`、Recipe、LayoutSolver、Web UI 和 USD exporter 不需要改 API。
`AssetRegistry.list()` 返回所有记录，`get()` 继续返回可用于布局的记录，
`metadata(asset_id)` 可读取完整 v2 元数据，`validate()` 只做不依赖 Isaac Sim
的 schema、路径和物理元数据检查。只有 `validated`/`ready` 资产进入随机候选池。

`AssetLoader` 负责根据 registry 文件位置解析相对 `usd_path` 和
`collision_path`，并在加载前检查本地文件是否存在；proxy/primitive 资产没有
USD 路径时仍可正常工作。

## P0-2 真实 USD 单资产闭环

当前仓库只提供 `mug_001` 的 metadata 模板，不包含虚构的高保真模型或假的
collision。模板位于 `data/assets/metadata/mug_001.template.json`，状态是 `raw`。
将真实文件放入目录后，使用以下流程：

```text
source USD -> AssetNormalizer -> normalized USD
                                      ↓
                         authored collision (optional)
                                      ↓
                         PhysX metadata + QA report
                                      ↓
                              registry status ready
```

`AssetNormalizer` 的 `normalize()` 会把输入标准化为 Z-up、米制 wrapper，并固定
使用 `collision_mode="none"`，因此不会创建 proxy collision。没有 `pxr`、源文件
不存在或 USD 无法打开时，只返回失败报告，不会留下输出文件。

`CollisionProcessor` 的 `process()` 只接受 `collision_path` 并检查它是否存在，
支持 `not_provided`、`pending`、`provided`、`authored`、`validated`、`rejected`
状态；它不会复制、重建或生成 collision。`collision_enabled=true` 时必须提供
`collision_path`。

Registry v2 现在支持以下 PhysX 元数据：

```json
{
  "mass": 0.3,
  "static_friction": 0.5,
  "dynamic_friction": 0.4,
  "rigid_body": true,
  "collision_enabled": false,
  "collision_status": "not_provided"
}
```

真实资产状态为 `raw -> normalized -> validated -> ready`；旧的 `quarantine` 和
`validated` 记录仍兼容，只有 `validated`/`ready` 资产会进入场景候选池。

## Asset QA CLI

普通 Python 可以先检查 registry 元数据和文件路径：

```powershell
python -m scene_factory asset inspect `
  --registry data/assets/registry.jsonl `
  --asset-id mug_blue `
  --report outputs/asset_qa/mug_blue.json
```

也可以直接检查一个 USD：

```powershell
python -m scene_factory asset inspect `
  --usd F:\scene_factory_assets\wrapped\my_mug.usda `
  --report F:\scene_factory_assets\reports\my_mug_qa.json
```

报告包含 `valid`、`issues`、路径、元数据检查和 USD 检查结果。USD 检查会验证
Z-up、米制、default Prim、mesh 数量、碰撞体和 stage 结构。没有 Isaac Sim 的
`pxr` 时不会让 CLI import 失败，而是写出 `usd_inspection_unavailable` 的报告；
使用 Isaac Sim Python 重新运行即可完成真实几何 QA。

## 1. 检查原始 USD

在项目根目录执行：

```powershell
$IsaacPython = "F:\scene_factory_isaac_py312\Scripts\python.exe"

& $IsaacPython tools\prepare_asset.py inspect `
  F:\scene_factory_assets\incoming\my_mug.usd `
  --report F:\scene_factory_assets\reports\my_mug_source.json
```

报告会记录默认 Prim、坐标轴、单位、包围盒、几何、材质、碰撞体和刚体数量。`warnings` 不一定意味着文件损坏；例如原资产没有碰撞体时，包装步骤可以创建保守的盒状碰撞体。

## 2. 标准化并生成隔离记录

```powershell
& $IsaacPython tools\prepare_asset.py wrap `
  F:\scene_factory_assets\incoming\my_mug.usd `
  --output F:\scene_factory_assets\wrapped\my_mug.usda `
  --report F:\scene_factory_assets\reports\my_mug_wrapper.json `
  --record F:\scene_factory_assets\records\my_mug.json `
  --asset-id my_mug `
  --category mug `
  --target-bbox 0.09 0.09 0.11 `
  --collision proxy_box `
  --mass-kg 0.30 `
  --friction 0.5 `
  --license "internal"
```

包装器会：

- 统一为 Z-up 和 1 米单位；
- 把资产几何中心对齐到局部原点；
- 保留对原始 USD 的引用，不复制网格；
- 可按目标包围盒缩放；
- 在 `proxy_box` 模式下增加不可见盒状碰撞体；
- 生成 `status=quarantine` 的资产记录。

`proxy_box` 适合先打通系统，凹形物体或机器人需要精细抓取时应换成 authored collision 或未来的凸分解碰撞。

## 3. 一键运行 PhysX 跌落验收

```powershell
powershell -ExecutionPolicy Bypass -File tools\run_asset_acceptance.ps1 `
  -AssetUsd F:\scene_factory_assets\wrapped\my_mug.usda `
  -Output F:\scene_factory_assets\reports\my_mug_acceptance `
  -MassKg 0.30 `
  -Steps 180 `
  -Record F:\scene_factory_assets\records\my_mug.json
```

脚本会生成单资产跌落场景，在真实 Isaac Sim/PhysX 中运行，并写出 `physx_report.json`。只有报告的 `valid=true` 时，`-Record` 指定的记录才会从 `quarantine` 提升为 `validated`。

如果暂时只想测试、不想改变记录，省略 `-Record`。

## 4. 加入正式资产库

验收通过后，把记录 JSON 压成一行并追加到 `data/assets/registry.jsonl`。正式资产库只接受 `validated` 记录。当前没有自动追加命令，这是为了避免批处理时把未经人工确认的类别、质量、授权信息误加入生产库。

## 5. 完全离线的演示

没有现成资产也可以验证工具链。下面会在本机程序化生成一个 Y-up、厘米制的测试杯子，然后标准化和验收；它不是下载的资产，也不会自动加入正式资产库。

```powershell
& $IsaacPython tools\create_offline_demo_asset.py `
  --output F:\scene_factory_runtime\asset_demo\source_mug_cm_yup.usda

& $IsaacPython tools\prepare_asset.py wrap `
  F:\scene_factory_runtime\asset_demo\source_mug_cm_yup.usda `
  --output F:\scene_factory_runtime\asset_demo\demo_mug_normalized.usda `
  --report F:\scene_factory_runtime\asset_demo\wrapper_report.json `
  --record F:\scene_factory_runtime\asset_demo\asset_record.json `
  --asset-id demo_mug_normalized `
  --category mug `
  --target-bbox 0.12 0.09 0.11 `
  --collision proxy_box `
  --mass-kg 0.32 `
  --license internal-demo

powershell -ExecutionPolicy Bypass -File tools\run_asset_acceptance.ps1 `
  -AssetUsd F:\scene_factory_runtime\asset_demo\demo_mug_normalized.usda `
  -Output F:\scene_factory_runtime\asset_demo\acceptance `
  -MassKg 0.32 `
  -Record F:\scene_factory_runtime\asset_demo\asset_record.json
```

资产记录字段定义见 `schemas/asset_record.schema.json`。
