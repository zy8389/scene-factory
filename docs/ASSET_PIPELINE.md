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
