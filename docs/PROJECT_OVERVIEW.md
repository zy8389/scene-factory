# SceneFactory 项目说明

> 面向具身智能的仿真环境自动化构建系统  
> 当前阶段：MVP 已完成，正在进入真实资产与机器人闭环集成阶段  
> 更新时间：2026-08-20

## 1. 项目概述

SceneFactory 将自然语言需求或生活事件配方编译为可复现的家庭机器人仿真场景。

系统目标不是简单地随机摆放物体，而是将场景语义、物体类别、物体关系、任务目标、资产物理属性和机器人可执行条件，统一编译成可以进入 Isaac Sim 的结构化场景。

核心流程：

```text
中文需求 / 生活事件
    -> SceneIntent
    -> SceneSpec
    -> 约束布局
    -> 几何与碰撞检查
    -> JSON / SVG 预览
    -> Isaac Sim USD
    -> PhysX 验收
    -> 机器人任务执行与评测
```

## 2. 当前已实现能力

### 场景生成

当前支持三个生活事件：

- `living_room_recent_snacking`：刚吃完零食的客厅；
- `living_room_returned_home`：刚回家后的入口区域；
- `kitchen_after_cooking`：刚做完饭的厨房。

支持：

- 自然语言提示词；
- 结构化事件配方；
- 基于随机种子的确定性生成；
- 多 seed 场景变体；
- 批量生成；
- 场景唯一 `scene_id`；
- 批次级 `manifest.jsonl`；
- 俯视 SVG 预览；
- JSON 场景布局和验证报告。

### 语言理解

当前有两条路径：

1. 无 LLM 时，通过关键词将中文需求路由到既有事件配方；
2. 配置 LLM 后，将中文需求解析为结构化 `SceneIntent`，再由确定性编译器生成场景。

LLM 不直接输出物体坐标、USD 路径或底层物理参数，以减少生成结果的不确定性。

### Web 与 CLI

系统提供：

- CLI 场景生成；
- Web UI；
- 场景增量修改；
- 多 seed 变体预览；
- LLM 状态与连接测试；
- USD 导出；
- Isaac Sim 打开接口。

### Agent 接口

提供 Gymnasium 风格的 `SceneFactoryEnv`。

当前包含：

- `SimulatorBackend` 协议；
- `DryRunBackend`；
- 场景 reset；
- 动作 step；
- observation、reward、terminated、truncated；
- 任务成功条件接口。

真实机器人动作空间和传感器由后续 Isaac backend 接入。

## 3. 当前验证状态

当前纯 Python 单元测试结果：

```text
18 tests passed
```

已覆盖：

- 多 seed 场景生成；
- 随机种子复现；
- 三个场景配方；
- 中文提示词路由；
- 批量生成与 manifest；
- SceneIntent 编译；
- LLM 结构化解析；
- LLM 缓存；
- LLM 降级路径；
- Web 场景生成；
- Web 场景修订；
- Agent facade；
- 任务判定；
- 资产记录隔离；
- USD 相关接口。

当前已确认的问题：

- LLM 配置已存在；
- 实际连接测试返回 `401 Invalid token`；
- 无 LLM 时的关键词降级路径仍然可用；
- Isaac Sim USD 导出和 PhysX 验收脚本已经实现；
- 尚未完成真实机器人 IK、导航和任务闭环验收。

## 4. 系统架构

```text
用户 / 任务描述
        |
        v
SceneIntent
        |
        v
IntentCompiler
        |
        v
SceneRecipe
        |
        v
LayoutSolver
        |
        v
CompiledScene
        |
        +--> Geometry Validation
        |
        +--> Top-down SVG Preview
        |
        +--> JSON Layout
        |
        +--> Isaac USD Exporter
                    |
                    v
              Isaac Sim / PhysX
                    |
                    v
            Robot Backend / RL / IL
```

核心模块：

- `scene_factory/intent.py`；
- `scene_factory/intent_compiler.py`；
- `scene_factory/layout.py`；
- `scene_factory/validation.py`；
- `scene_factory/factory.py`；
- `scene_factory/llm.py`；
- `scene_factory/asset_pipeline.py`；
- `scene_factory/exporters/isaac_usd.py`；
- `scene_factory/agent.py`；
- `scene_factory/webapp.py`。

## 5. 当前资产状态

当前资产注册表包含 20 个资产。

现状：

- 全部是程序化代理资产；
- 主要使用 `cube` 和 `cylinder`；
- 没有真实 USD `source_path`；
- 已配置类别、尺寸、质量、摩擦和支撑面；
- 尚未形成真实高保真资产库；
- 当前资产适合验证场景语义和布局，不适合高质量视觉或精细抓取训练。

当前资产状态流转：

```text
原始 USD
    -> inspect
    -> wrap
    -> quarantine
    -> PhysX 跌落测试
    -> 人工审核
    -> validated
    -> registry.jsonl
```

当前碰撞等级：

- `L0`：proxy box，适合管线测试；
- `L1`：凸包或多凸体碰撞，适合普通抓取和放置；
- `L2`：作者制作的低模碰撞体，适合接触密集型任务。

真实资产入库前必须记录：

- 来源；
- 许可证；
- 原始文件哈希；
- 实际尺寸；
- 质量；
- 摩擦参数；
- 视觉网格；
- 碰撞网格；
- 支撑面；
- 抓取区域；
- 关节和可操作部件；
- QA 报告。

## 6. 高保真资产建设计划

### 第一阶段：最小可用资产集

优先完成 12 到 16 个真实 USD 资产：

- mug；
- pot；
- pot lid；
- kitchen knife；
- backpack；
- shoe；
- keys；
- coffee table；
- kitchen counter。

每个资产必须完成：

```text
来源登记
    -> USD 检查
    -> Z-up / 米制标准化
    -> 视觉网格与碰撞网格分离
    -> authored 或凸分解碰撞
    -> PhysX 多姿态跌落测试
    -> 支撑面和抓取语义标注
    -> 人工审核
    -> registry 入库
```

### 第二阶段：可操作家具

加入：

- 抽屉；
- 柜门；
- 冰箱门；
- 锅盖；
- 杯把；
- 容器内部；
- 可开合和可旋转部件。

### 第三阶段：批量资产生产

实现：

- 自动资产检查；
- 自动碰撞体生成；
- 自动 QA 报告；
- 资产依赖打包；
- 资产版本管理；
- 批量准入命令；
- 资产质量分级；
- 资产许可证追踪。

## 7. NVIDIA 生态集成方向

SceneFactory 不替代 Isaac Sim，而是作为 Isaac Sim 上层的场景语义与任务编译层。

建议组合：

```text
SceneFactory
    -> 中文需求、任务语义、约束布局、SceneSpec

SAGE
    -> 复杂任务驱动的场景和对象生成

NuRec / Marble
    -> 真实房间重建和环境外观生成

Isaac Sim
    -> USD、PhysX、传感器和场景执行

Isaac Lab
    -> 机器人训练、并行环境和任务学习

OSMO
    -> 批量生成、仿真、数据采集和训练编排
```

相关项目：

- [NVIDIA Isaac Sim](https://developer.nvidia.com/isaac/sim)；
- [NVIDIA Isaac Lab](https://developer.nvidia.com/isaac/lab)；
- [NVIDIA SAGE](https://github.com/NVlabs/sage)；
- [SAGE-10k Dataset](https://huggingface.co/datasets/nvidia/SAGE-10k)；
- [Marble + Isaac Sim 官方流程](https://developer.nvidia.com/blog/simulate-robotic-environments-faster-with-nvidia-isaac-sim-and-world-labs-marble/)；
- [手机扫描重建 Isaac Sim 场景](https://developer.nvidia.com/blog/reconstruct-a-scene-in-nvidia-isaac-sim-using-only-a-smartphone/)。

### 适配原则

- SceneFactory 负责中文需求、任务语义和约束布局；
- SAGE 可作为复杂场景生成器；
- NuRec/Marble 可作为真实房间重建器；
- Isaac Sim 负责执行和物理验证；
- Isaac Lab 负责机器人训练；
- 所有外部生成结果仍必须经过 SceneFactory 的资产和物理验收。

## 8. 当前主要问题

1. 真实高保真 USD 资产尚未接入；
2. 当前资产碰撞主要是代理级别；
3. 尚未实现自动凸分解和复杂碰撞生成；
4. 资产记录中的摩擦参数需要进一步绑定到 Isaac PhysX 材质；
5. 尚未完成真实机器人动作空间和传感器后端；
6. 尚未完成导航可达性与 IK 可达性验证；
7. 尚未完成生产级任务 oracle；
8. LLM 当前因无效 token 无法正常调用；
9. SAGE、Isaac Lab 与当前 Isaac Sim 6.0.1 存在版本兼容性风险；
10. 当前工程目录没有 Git 元数据，版本追踪和资产追溯需要补齐。

## 9. 下一阶段路线

### P0：真实资产垂直切片

目标：

- 12 到 16 个真实 USD 资产；
- 完整的资产来源和许可证记录；
- L1 级碰撞体；
- PhysX 多姿态跌落测试；
- 在厨房和入户场景中批量验证。

### P1：机器人任务闭环

选择一个最小任务：

- Franka 抓取和放置厨房物体；或
- 移动机器人在入口区域导航并接近目标。

补齐：

- Isaac backend；
- 机器人动作空间；
- 传感器；
- IK/导航可达性；
- 成功条件；
- 任务 oracle；
- 仿真数据导出。

### P2：外部生成器接入

实现：

- `SAGE -> SceneSpec` 导入适配器；
- `NuRec/Marble -> 房间 USD` 导入适配器；
- 外部资产统一验收；
- 资产依赖打包；
- 批量 worker；
- 场景生成、仿真、训练的一体化调度。

## 10. 项目当前定位

SceneFactory 当前已经是一个可运行的仿真环境构建器 MVP。

它已经解决了：

- 场景语义结构化；
- 中文需求解析接口；
- 约束布局；
- 可复现生成；
- 批量输出；
- USD 导出；
- 初步物理验收；
- Web 和 Agent 接口。

下一阶段的核心不是继续增加更多简单配方，而是完成：

```text
真实资产
    + 精确碰撞
    + 机器人可达性
    + 任务闭环
    + 批量生成和训练
```

最终目标：

```text
自然语言任务
    -> 自动生成场景
    -> 自动配置资产和任务
    -> Isaac Sim 物理验证
    -> Isaac Lab 训练
    -> 批量生成数据
    -> 机器人策略评测
```
