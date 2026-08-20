# SceneFactory MVP

Windows 本地无下载部署见
[`docs/LOCAL_DEPLOYMENT.md`](docs/LOCAL_DEPLOYMENT.md)。它复用当前已经安装好的
Isaac Python/pxr，提供启动、停止、状态、测试和离线 USD 演示命令。

本地 USD 资产的单位/坐标标准化、隔离登记和 PhysX 跌落验收见
[`docs/ASSET_PIPELINE.md`](docs/ASSET_PIPELINE.md)。这套流程不会下载资产。

Isaac Sim 6.0.1 的本机安装、一键 USD/PhysX 验收和已知硬件限制见
[`docs/ISAAC_VALIDATION.md`](docs/ISAAC_VALIDATION.md)。

可以直接输入中文需求、查看布局和多 seed 变体的本地界面见
[`docs/WEB_UI.md`](docs/WEB_UI.md)。

结构化 LLM/VLM 语义解析、环境变量配置、缓存和降级策略见
[`docs/LLM_INTEGRATION.md`](docs/LLM_INTEGRATION.md)。
LLM 的直接接入口是 [`config/llm.json`](config/llm.json)；密钥只通过
`SCENE_FACTORY_LLM_API_KEY` 环境变量传入。

SceneFactory 把自然语言或事件配方编译为可复现的家庭仿真场景。当前版本可在纯
Python 环境中完成资产选择、约束布局、几何质检、批量生成和俯视预览；在 Isaac
Sim Python 环境中还可以导出 USD。

当前自带三个“生活痕迹”事件：

- `living_room_recent_snacking`：刚吃完零食的客厅；
- `living_room_returned_home`：刚回家后的入口区域；
- `kitchen_after_cooking`：刚做完饭的厨房。

## 快速开始

本项目核心没有第三方运行时依赖，当前目录直接运行：

```powershell
python -m scene_factory list-recipes

python -m scene_factory build `
  --prompt "刚回家，背包和鞋在入口附近，钥匙放在长凳上" `
  --seed 42 `
  --output outputs\demo
```

生成结果包括：

```text
outputs/demo/
├── scene_spec.json   输入配方、自然语言和随机种子
├── layout.json       已解析资产与确定性位姿
├── validation.json   几何、支撑和碰撞质检
└── preview.svg       无需仿真器即可查看的俯视预览
```

批量生成：

```powershell
python -m scene_factory batch `
  --recipe living_room_recent_snacking `
  --count 1000 `
  --seed-start 10000 `
  --output outputs\living-room
```

每个场景都有由配方、描述和 seed 决定的稳定 `scene_id`。批次根目录的
`manifest.jsonl` 可直接交给训练或数据管线。

## Isaac Sim 6.0.1 导出

使用 Isaac Sim 的 Python 3.12 环境安装和运行：

```powershell
conda create -n scene-factory-isaac python=3.12 -y
conda activate scene-factory-isaac
python -m pip install --upgrade pip
pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
pip install "isaacsim[all,extscache]==6.0.1.0" --extra-index-url https://pypi.nvidia.com

python -m scene_factory build `
  --recipe kitchen_after_cooking `
  --seed 42 `
  --output outputs\isaac-kitchen `
  --usd
```

`--usd` 会额外生成 `scene.usd`，包括房间地面、碰撞体、刚体、质量、语义类别和
对象 ID。当前示例使用程序化几何体；把 `data/assets/registry.jsonl` 中的
`source_path` 指向经过物理清洗的 USD 后，导出器会改为引用真实资产。

## Agent 接口

`SceneFactoryEnv` 提供 Gymnasium 风格的接口。没有绑定机器人时可以使用
`DryRunBackend` 测试数据管线：

```python
from scene_factory.agent import SceneFactoryEnv

env = SceneFactoryEnv("outputs/demo/layout.json")
obs, info = env.reset()
obs, reward, terminated, truncated, info = env.step([0.0])
```

真实 Isaac Sim 接入时实现同一个 `SimulatorBackend` 协议即可：

```text
reset(scene) -> observation, info
step(action) -> observation, reward, terminated, truncated, info
render() -> rgb
close()
```

机器人本体、动作空间和传感器与具体项目强相关，因此没有在通用后端中猜测。

## 添加资产

在 `data/assets/registry.jsonl` 增加一行：

```json
{"asset_id":"my_mug","category":"mug","bbox_m":[0.09,0.09,0.11],"source_path":"D:/assets/my_mug/model.usd","mass_kg":0.3,"friction":0.5,"status":"validated"}
```

支持摆放其他物体的家具需要声明局部支撑面：

```json
{"name":"top","center":[0,0,0.4],"size":[1.0,0.6]}
```

资产只有 `status=validated` 时才会被随机选中。混元生成的资产建议先标记为
`quarantine`，完成单位、网格、碰撞体、质量和跌落测试后再改为 `validated`。

### P0 Asset Pipeline

资产登记同时兼容旧 proxy 记录和 Registry v2。v2 记录可以增加 `name`、`hash`、
`usd_path`、`collision_path`、`mass`、`friction`、`support_surface` 和
`grasp_region`；旧字段 `source_path`、`mass_kg`、`support_surfaces` 仍然有效。
`AssetRegistry` 负责读取和筛选状态，`AssetLoader` 负责解析相对 USD/碰撞路径，
`scene_factory.asset_validator` 负责元数据与 USD QA。状态流转为：

```text
incoming USD -> inspect -> wrap -> quarantine -> PhysX acceptance -> validated
```

在不使用 Isaac Sim 的开发机上可以先运行：

```powershell
python -m scene_factory asset inspect `
  --registry data/assets/registry.jsonl `
  --asset-id mug_blue `
  --report outputs/asset_qa/mug_blue.json
```

真实 USD 的 Z-up、米制、mesh、碰撞体和 stage 结构检查需要 Isaac Sim 提供的
`pxr`。普通 Python 会输出结构化的 unavailable QA 报告，不会影响 proxy 场景生成。

### P0-2 Real USD Asset Integration

真实资产接入目录已经建立，但仓库当前没有伪造的高保真 `mug_001` 模型：

```text
data/assets/
├── source/       原始 USD（只读输入）
├── usd/          Z-up、米制、标准化 USD
├── collision/    外部 authored collision（可选，不自动生成）
├── metadata/     PhysX 元数据模板
└── qa_reports/   标准化与碰撞 QA 报告
```

`data/assets/metadata/mug_001.template.json` 是待接入模板，状态为 `raw`，不会被
场景生成器选中。真实闭环为：

```text
Asset Source -> USD Normalize -> Collision -> PhysX Metadata -> Registry -> Isaac Sim
```

可以先在普通 Python 环境验证导入边界：

```powershell
python -m scene_factory asset normalize `
  data/assets/source/mug_001.usda `
  --output data/assets/usd/mug_001.usda `
  --asset-id mug_001 `
  --category mug `
  --report data/assets/qa_reports/mug_001_normalize.json

python -m scene_factory asset collision `
  --collision-path data/assets/collision/mug_001.usda `
  --status provided `
  --enabled `
  --report data/assets/qa_reports/mug_001_collision.json
```

`AssetNormalizer` 在 Isaac Sim/`pxr` 环境中调用现有 USD wrapper，强制不创建
collision；`CollisionProcessor` 只检查和登记已有 collision 文件，报告中的
`generated` 永远为 `false`。状态依次为 `raw`、`normalized`、`validated`、`ready`。

## 添加生活事件

复制 `recipes/` 中的 JSON，修改：

- 固定家具及其 `fixed_pose`；
- 动态物体的 `support`；
- `near`、`edge_bias`、`region_xy` 等布局约束；
- 自然语言匹配用的 `keywords`；
- `task.success` 任务成功条件。

完整结构约束在 `schemas/scene_spec.schema.json`。核心加载器还会检查重复 ID、未知
资产、依赖环、支撑面、房间边界和初始碰撞。

## 测试

```powershell
python -m unittest discover -s tests -v
```

当前测试覆盖随机种子复现、中文提示词路由、所有配方多 seed 生成、批量清单、Agent
接口和任务判定。纯几何质检是第一道门；生产环境仍需在 Isaac Sim 中增加 2–5 秒
物理落稳、速度阈值、机器人 IK/导航可达性和脚本化 oracle 任务测试。

## 目前边界

- 自然语言层目前是离线关键词到事件配方的确定性路由，接口已保留；下一步可以替换为
  结构化输出的 LLM，同时继续使用同一个 SceneSpec 校验器。
- 当前碰撞检测使用保守的旋转包围盒，物理落稳需要 Isaac Sim worker 执行。
- 尚未实现照片解析、混元自动清洗、机器人专用 Isaac backend 和 MuJoCo 编译器。
  这些都可以在不改变配方和训练清单格式的前提下逐步加入。
