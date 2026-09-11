# SceneFactory

SceneFactory 是面向具身智能仿真工作流的确定性场景、任务、交互规划和执行器契约工具包。

v0.1 候选版本提供离线 Python SDK，可在无需 Isaac Sim、GPU、NumPy、LLM API
或网络连接的情况下完成克隆、安装、检查和使用。

## 功能概览

```text
配方 / 外部 SceneIntent
          |
          v
确定性 SceneFactory 编译
          |
          +--> 场景规范和 SVG 预览
          +--> 可复现的批量数据集
          +--> 可动部件资产契约
          +--> 符号化 InteractionPlan
          +--> DryRun ExecutionTrace
          +--> 执行器一致性报告
```

核心流程提供：

- 根据配方或外部 JSON 生成确定性的家庭场景；
- 资产注册表、元数据、支撑面和可动部件契约；
- 具备可移植清单、验证和复现能力的批量数据集；
- 符号化可动部件规划与离线干运行执行；
- 执行轨迹验证及核心执行器一致性套件；
- 可选的 Isaac Sim USD 导出和依赖环境的机器人集成。

## 安装

SceneFactory 支持 Python 3.12 或更高版本，核心 SDK 没有必需的运行时依赖：

```bash
python -m pip install .
scene-factory list-recipes
```

开发检查环境可使用：

```bash
python -m pip install ".[dev]"
```

wheel 包含配方、模式、Web 文件、资产注册表，以及离线工作流所需的已提交资产
元数据；不包含 Isaac Sim 或 NVIDIA Local Assets。

## 5 分钟快速上手

无需模拟器即可构建一个确定性场景：

```bash
scene-factory build \
  --recipe living_room_recent_snacking \
  --seed 42 \
  --output outputs/basic-scene
```

输出包含 `scene_spec.json`、`layout.json`、`validation.json` 和离线
`preview.svg`。安装后，相同命令可在任意当前目录中运行。

等价的 Python API 为：

```python
from scene_factory import SceneFactory

result = SceneFactory().build_from_recipe(
    "living_room_recent_snacking",
    seed=42,
)
assert result.valid
print(result.scene.scene_id)
```

可运行示例位于 [`examples/`](examples/)：

- [`basic_scene`](examples/basic_scene/README.md)：配方编译；
- [`external_intent`](examples/external_intent/README.md)：结构化输入；
- [`deterministic_dataset`](examples/deterministic_dataset/README.md)：批量验证
  和复现；
- [`articulated_drawer`](examples/articulated_drawer/README.md)：规划、干运行
  执行和轨迹验证。

## 外部 SceneIntent

外部程序可以提交带版本的 `SceneIntent` JSON 文档。使用相同的确定性流水线
验证并编译：

```bash
scene-factory intent validate examples/external_intent/scene.json
scene-factory build \
  --intent examples/external_intent/scene.json \
  --seed 42 \
  --output outputs/external-scene
```

原始 intent 以及 `scene_factory.external_scene.v1` 封装会在编译前验证。
生成方元数据记录来源，但不影响场景身份。

## 确定性数据集

```bash
scene-factory batch \
  --recipe living_room_recent_snacking \
  --count 3 \
  --seed-start 100 \
  --output outputs/dataset
scene-factory dataset validate outputs/dataset
scene-factory dataset reproduce outputs/dataset
```

数据集清单使用可移植的相对路径、内容哈希和语义指纹。验证和复现完全离线，
不会调用 LLM 或外部服务。

## 可动部件规划与执行

符号规划器使用可动部件元数据生成 `InteractionPlan`。干运行执行器会应用
语义状态转换，并输出经过验证的 `ExecutionTrace`：

```bash
scene-factory task plan \
  --scene examples/articulated_drawer/scene.json \
  --object drawer_1 \
  --state open \
  --output outputs/drawer-plan.json
scene-factory task execute \
  --scene examples/articulated_drawer/scene.json \
  --plan outputs/drawer-plan.json \
  --executor dry-run \
  --output outputs/drawer-trace.json
scene-factory task execution-validate \
  --scene examples/articulated_drawer/scene.json \
  --plan outputs/drawer-plan.json \
  --trace outputs/drawer-trace.json
```

`DryRunInteractionExecutor` 会报告 `physical=false`。符号计划、轨迹或一致性
报告通过，并不表示已经证明运动无碰撞或操作具备物理可行性。

## 执行器一致性

查看参考执行器并运行与模拟器无关的核心套件：

```bash
scene-factory executor inspect --executor dry-run
scene-factory executor conformance \
  --executor dry-run \
  --output executor-conformance.json
scene-factory executor validate-report executor-conformance.json
```

该套件检查生命周期、能力声明、命令与结果关联、证据、最终目标和轨迹语义。
它是 `InteractionExecutor` 的兼容性门槛，而非物理验收门槛。

## 架构与 API

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) 说明编译流程和模拟器边界。
- [`docs/PUBLIC_API.md`](docs/PUBLIC_API.md) 记录受支持的 Python API。
- [`docs/CLI.md`](docs/CLI.md) 列出 CLI 命令和退出码行为。
- [`docs/COMPATIBILITY.md`](docs/COMPATIBILITY.md) 记录环境支持范围。
- [`docs/SCHEMA_POLICY.md`](docs/SCHEMA_POLICY.md) 定义模式兼容性策略。
- 更深入的资料仍可参阅[资产流水线](docs/ASSET_PIPELINE.md)、
  [LLM 集成](docs/LLM_INTEGRATION.md)和 [Web UI](docs/WEB_UI.md)。

## Isaac Sim 状态

Isaac 专用模块采用延迟导入，因此 `import scene_factory` 始终是纯 Python 操作。
Isaac Sim 6.0.1 可用于 USD 导出，以及
[`docs/ISAAC_VALIDATION.md`](docs/ISAAC_VALIDATION.md) 中描述的依赖环境的
Franka/RGB-D 工作流。

当前公开状态有意保持谨慎：

| 能力 | 状态 |
| --- | --- |
| 纯 Python 场景、数据集、规划和干运行工作流 | 可用 |
| 执行器一致性 | 可用 |
| 真实 Isaac Franka 验收（P1-1/P1-2） | 已在参考 Isaac Sim 6.0.1 环境中验证 |
| 真实 Isaac RGB-D 验收（P1-3） | 已在参考 Isaac Sim 6.0.1 环境中验证 |
| Isaac Lab | 尚未开始 |

官方 Isaac Sim Local Assets 不随 SceneFactory 打包。真实 Franka 和 RGB-D 验收需要
单独验证的 Isaac 环境和官方资产。参考 Isaac Sim 6.0.1 本地环境已通过 P1-1、P1-2
和 P1-3；这些证据仅适用于该环境，并不代表通用硬件兼容性。项目不会将随附的
URDF 或离线结果作为该验收的替代证明。

## CLI 参考

公开命令组如下：

```text
list-recipes
build
batch
intent
dataset
task
executor
asset
llm-status
llm-test
```

运行 `scene-factory --help` 或查看 [`docs/CLI.md`](docs/CLI.md) 了解完整语法。
CLI 成功时使用退出码 `0`；命令、配置或运行时输入错误使用 `1`；
验证或验收失败使用 `2`。

## 开发

离线发布检查如下：

```bash
python tools/check_release.py
python tools/release_smoke.py
python -m ruff check scene_factory tools tests
python -B -m compileall -q scene_factory tools tests
python -B -m pytest -p no:cacheprovider -q
```

`tools/release_smoke.py` 应在全新的虚拟环境中安装 wheel 后运行。它会从仓库外的
临时目录运行，且不会导入仓库源文件。

## Episode 验证与回放

导出的 RGB-D episode 可在无需启动 Isaac Sim 的常规 Python 环境中检查：

```powershell
scene-factory episode inspect <episode_path>
scene-factory episode validate <episode_path>
scene-factory episode replay <episode_path>
```

`validate` 会检查 episode 文件、媒体、标定、帧同步、状态机转换和结果一致性。
`replay` 是确定性的离线一致性检查，不会重新运行 Isaac Sim 物理仿真。若 episode
元数据包含任务快照，它还会重新计算纯 Python 任务预言机；否则会明确报告
`task_replay=not_available`。

## 许可证与资产署名

代码采用 MIT 许可证。随包提供的 YCB 源资产署名见
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)，其上游许可证条款仍然适用。
发布就绪范围和状态见 [`RELEASE_CHECKLIST.md`](RELEASE_CHECKLIST.md) 与
[`CHANGELOG.md`](CHANGELOG.md)。
