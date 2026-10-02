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
- 分层质量报告、失败原因汇总、精确去重筛选与数据多样性统计；
- 符号化可动部件规划与离线干运行执行；
- 执行轨迹验证及核心执行器一致性套件；
- 可选的 Isaac Sim USD 导出和依赖环境的机器人集成。

## 当前集成边界与 Blender case

- 机器人本体：**1 种，Franka Emika Panda**（Isaac Sim 内的抓杯、搬放与 RGB-D 验收）；UR/xArm、移动底盘、双臂、人形与真实硬件均未接入。机器人有独立的 `RobotSpec` / `RobotAdapter` 接口，不等于新增机器人已经支持。
- Blender：`--blender` 生成 `blender_manifest.json`、独立 `blender_render.py` 与所需 GLB 副本；**不需要 Blender 即可准备 case**。装好 Blender 后通过 `scene-factory blender render` 得到 `.blend` 和 PNG。静态场景可视化不包含 Franka 机器人模型、动作轨迹或物理验收。
- 生成后的 case 位于指定输出目录，可先查看 `preview.svg` 与同目录清单。截至 2026-10-02，参考机器未安装 Blender，因而 `.blend` / PNG 尚未实机渲染验证。

### 推荐的 uv / .venv 工作流（PowerShell）

```powershell
powershell -ExecutionPolicy Bypass -File tools\bootstrap_uv.ps1
# 若 uv 已安装：uv run scene-factory doctor
py -3.12 -m uv run scene-factory doctor
py -3.12 -m uv run scene-factory build --recipe kitchen_after_cooking --seed 42 --output outputs\kitchen_blender_case --blender
# 安装 Blender 后：
py -3.12 -m uv run scene-factory blender render outputs\kitchen_blender_case\blender_manifest.json --blender-exe C:\path\to\blender.exe
```

启动脚本在 `.venv` 中同步锁定的开发依赖与 MuJoCo；没有 uv 时将它安装在当前用户的 Python 3.12 中。Isaac Sim 使用单独的本地 Python（优先读取 `SCENE_FACTORY_ISAAC_PYTHON`，其次检测 `local_resources/environments/scene_factory_isaac_py312`），不会被 `uv sync` 覆盖。可选 `-InstallIsaacProject` 将本项目以无依赖模式安装到已经存在的 Isaac 环境。CUDA 12.8 是本地 Isaac/PyTorch 验证基线，不是纯 Python 编译器的硬依赖。

**Isaac 路径兼容处理**：参考机器在 Windows 中文路径下遇到 `pxr` 可导入但无法创建 USD Stage 的问题，已通过复用现有 Isaac 环境并创建 ASCII-only Junction 解决，未复制、删除或重建 Isaac Sim。2026-10-01 的 fresh `build --usd` 和 headless Kit/PhysX 检查已通过；这不代表其他机器或所有机器人任务已通过验收。Windows 下请让脚本自动准备该 Junction，并继续使用 ASCII-only USD/输出路径。

## 安装

SceneFactory 支持 Python 3.12 或更高版本，核心编译 SDK 没有必需的运行时依赖：

```bash
python -m pip install .
scene-factory list-recipes
```

默认 `SceneFactoryEnv` 后端使用 MuJoCo；要创建该环境并调用 `reset()`，请安装模拟器扩展：

```bash
python -m pip install ".[mujoco]"
```

Web UI、SVG/Three.js 预览、MJCF 导出和场景包导出本身不需要安装 MuJoCo；它们会使用
随包的浏览器资源和本地 GLB，缺少 GLB 时会显示包围盒代理。只需离线契约环境时，可向
`SceneFactoryEnv` 显式传入 `DryRunBackend`。

开发检查环境可使用：

```bash
python -m pip install ".[dev]"
```

wheel 包含配方、模式、Web 文件、资产注册表，以及离线工作流所需的已提交资产
元数据；不包含 Isaac Sim、个人运行目录或 NVIDIA Local Assets。USD 导出和编辑器启动
使用随 wheel 安装的模块，不再依赖源码仓库中的 `tools` 目录。

CI 的核心测试不安装模拟器；MuJoCo 物理测试在单独安装 `.[dev,mujoco]` 的任务中执行。

## 5 分钟快速上手

无需模拟器即可构建一个确定性场景：

```bash
scene-factory build \
  --recipe living_room_recent_snacking \
  --seed 42 \
  --output outputs/basic-scene
```

输出包含 `scene_spec.json`、`layout.json`、`validation.json`、离线
`preview.svg`、默认生成的 MuJoCo `scene.xml`，以及可移植的
`<scene_id>.scene.zip`。场景包带版本清单和 SHA-256 校验，包含当前场景使用的本地
GLB；可在 Web UI 中点击“导出场景包”直接下载。如果启用 USD 导出，ZIP 内的
`scene/scene.usdz` 会封装所引用的 USD、碰撞层和纹理；解压外层 ZIP 后直接打开这个
USDZ，不要再拆开 USDZ。缺失的依赖会使打包报错，而不是生成不完整的下载文件。
单独导出的 `scene.usd` 仍可能引用本机资产。安装后，相同命令可在任意当前目录中运行。

如不需要 MJCF，可在 CLI 中添加 `--no-mjcf`。

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

### 数据质量与筛选

```bash
scene-factory dataset audit outputs/dataset --minimum-level layout --deduplicate --output outputs/reports/dataset-quality.json
```

每个新场景输出 `quality.json`，明确区分布局通过、物理未验证和任务未验证。
审计先检查源文件完整性，再输出合格场景索引、失败原因及筛选前后的多样性统计，
不删除源数据。`--minimum-level physics` 或 `task` 不会把缺失证据当作通过。
报告写在源数据集目录以外；旧 v1 数据仍可读取。详见[数据路线工作流](docs/DATA_WORKFLOW.md)。

抓杯与抓取放置数据可使用独立的 `kitchen_franka_mug_lift_data`、
`kitchen_franka_mug_pick_place_data` 配方，按种子随机化杯子起点。原固定验收配方保留。
新配方只提供布局候选，物理和任务验收仍需单独执行。布局采样失败会进行有次数上限的
重新布局；已成功种子的布局保持原采样结果。

分层采样计划可为不同配方及起点区域设置合格配额，按有限候选预算生成、过滤和跨组去重。
任一配额不足时，整个集合保持未完成；通过后输出均衡选择索引和覆盖报告。
参见[分层数据示例](examples/stratified_dataset/README.md)：

```bash
scene-factory dataset sample examples/stratified_dataset/plan.json --output outputs/stratified
scene-factory dataset sampling-validate outputs/stratified
scene-factory dataset sampling-reproduce outputs/stratified
```

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
