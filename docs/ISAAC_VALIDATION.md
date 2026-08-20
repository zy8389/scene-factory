# Isaac Sim 6.0.1 安装与验收

本机已安装好一套独立环境：

- Python：`F:\scene_factory_isaac_py312\Scripts\python.exe`
- Isaac Sim：6.0.1.0（`all` + `extscache`）
- OpenUSD / pxr：25.05
- PyTorch：2.11.0+cu128
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU

## 一键生成并验收

在项目根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File tools\run_isaac_acceptance.ps1
```

默认会完成三件事：

1. 用 SceneFactory 直接生成 `F:\scene_factory_runtime\acceptance\scene.usd`；
2. 用 Isaac 自带的 `pxr` 检查 USD 单位、Z-up、PhysicsScene、Collision、RigidBody、Mass 和资产依赖；
3. 无界面启动 Isaac Sim / PhysX，推进 240 个物理步并输出刚体位姿报告。

产物中的 `openusd_report.json` 和 `isaac_runtime_report.json` 都应显示
`"valid": true`。

也可以改配方、seed、步数和输出目录：

```powershell
powershell -ExecutionPolicy Bypass -File tools\run_isaac_acceptance.ps1 `
  -Recipe living_room_recent_snacking `
  -Seed 1001 `
  -Steps 360 `
  -Output F:\scene_factory_runtime\living_room_1001
```

## Windows 路径限制

OpenUSD 25.05 在当前 Windows 环境中可以创建中文路径下的 USD，但重新打开时会失败。
因此，要进入 Isaac worker 的 USD 输出目录必须使用纯 ASCII 路径，例如
`F:\scene_factory_runtime\...`。验收脚本会在启动前主动检查这一点。

## 硬件注意事项

Isaac 6.0.1 兼容性检查器的结果是 `FAILED`，原因是本机约 8.59 GB VRAM 和
16.77 GB RAM，低于检查器要求的 10 GB VRAM / 32 GB RAM。GPU 型号和 610.47 驱动均受支持。

本项目的小型无头 PhysX 验收已可运行，但大型房间、高分辨率 RTX 相机、Replicator
多传感器和并行多 worker 仍容易受内存/显存限制。批量 worker 建议首先使用纯物理、低分辨率、单 GPU
配置，并根据显存实测控制并发数。
