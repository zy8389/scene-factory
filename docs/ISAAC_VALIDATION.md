# Isaac Sim 6.0.1 安装与验收

本机已安装好一套独立环境：

- Python：`F:\scene_factory_isaac_py312\Scripts\python.exe`
- Isaac Sim：6.0.1.0（`all` + `extscache`）
- OpenUSD / pxr：25.05
- PyTorch：2.11.0+cu128
- GPU：NVIDIA GeForce RTX 4060 Laptop GPU

环境探针使用 6.0.1 的实际入口 `isaacsim.SimulationApp`，并通过已安装 distribution
检查 core、robot-motion 与 PhysX extension cache。`omni.*`/`isaacsim.core.*` 运行时
import 必须等 `SimulationApp` 启动后执行，因此探针不会在 Kit 启动前错误导入它们：

```powershell
& F:\scene_factory_isaac_py312\Scripts\python.exe tools\detect_isaac_env.py --require
```

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

## Franka mug-lift manual gate

P1-1 新增 `IsaacSimBackend`，其普通 Python 模块只包含延迟导入。真实 runtime 的结构为：

```text
SceneFactory recipe + ready registry asset
  -> parent process exports scene.usd
  -> clean Isaac child starts SimulationApp
  -> SimulationContext opens /World/PhysicsScene
  -> SingleArticulation loads Franka 6.0 asset
  -> Lula IK drives right_gripper
  -> ParallelGripper executes physical contact
  -> TaskEvaluator reads mug_1 world pose
  -> robot_acceptance.json
```

父/子进程隔离是必要的：父进程 USD 导出会加载 `pxr`，而 Isaac 的 Carbonite/plugin
runtime 要求 `SimulationApp` 是子进程内第一个 Isaac/OpenUSD 入口。backend 不会回退到
`DryRunBackend`，也没有伪造 attachment 或成功位姿。

运行：

```powershell
$env:OMNI_KIT_ACCEPT_EULA = "YES"
& F:\scene_factory_isaac_py312\Scripts\python.exe `
  tools\run_franka_mug_lift.py `
  --output F:\scene_factory_runtime\p1_1_franka_mug_lift
```

状态机固定为 `PRE_GRASP -> APPROACH -> GRASP -> LIFT -> DONE/FAILED`，包含总超时、
阶段超时、连续 IK failure、grasp failure 与 object lost。位置和 `right_gripper` frame
姿态必须同时收敛后才会切换 reach 阶段；抬升目标以每 physics step `2 mm` 递增。

真实 `mug_001` visual USD 与 authored collision USD 原点不同。导出器通过
`UsdGeom.BBoxCache` 计算 collision local midpoint，并在 `AuthoredCollision` reference 上
施加逆平移，使 visual/collision 中心一致。修复后杯子在桌面稳定于约
`z=0.964435 m`；修复前约为 `z=0.944196 m`。

### 当前真实结果

最近与当前稳定控制配置一致的完整运行产物：

```text
F:\scene_factory_runtime\p1_1_franka_mug_lift_v29\robot_acceptance.json
```

关键指标：

```text
Isaac Sim 6.0.1.0
result=failed
failure_reason=grasp_failure
steps=456
ik=passed
grasp=failed
task_success=false
initial_target_z=0.9644354582
final_target_z=0.9644355178
lift_delta_m=0.0000000596
```

Franka、Lula IK、真实 PhysX stepping、ready `mug_001`、碰撞接触和 TaskEvaluator 都已
实际运行，但杯柄未在抬升阶段保持夹持，因此 P1-1 真实机器人 acceptance 仍是
`FAILED`。在新的 `robot_acceptance.json` 同时满足 `task_success=true` 与
`lift_delta_m>=0.10` 前，不得写成 `PASSED`。
