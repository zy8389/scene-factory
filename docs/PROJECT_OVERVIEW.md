# SceneFactory 项目说明

> 面向具身智能仿真工作流的确定性场景、任务和执行契约工具包
> 当前阶段：离线编译、MuJoCo 运行与 Web 三维预览可用；真实机器人闭环仍在推进
> 更新时间：2026-09-12

## 项目定位

SceneFactory 把自然语言需求、生活事件配方或外部 `SceneIntent` 编译为可复现的家庭机器人场景。它负责场景语义、资产选择、约束布局、任务描述和可审计输出；物理引擎和机器人控制通过独立后端接入。

```text
中文需求 / 生活事件 / 外部 SceneIntent
                 |
                 v
       确定性 SceneFactory 编译
                 |
       +---------+----------+-----------+
       v         v          v           v
 SceneSpec    数据集     交互计划    场景包
 JSON/SVG     清单       执行轨迹    MJCF/GLB
                 |
       +---------+----------+
       v                    v
 MuJoCo 代理碰撞运行    Isaac Sim USD / PhysX（可选）
```

相同输入和随机种子会产生相同的场景身份、布局与语义指纹。LLM 只负责将开放式语言转换成受模式约束的 `SceneIntent`，不会直接生成坐标、资产路径或物理参数。

## 当前能力

### 场景和数据

- 三个内置生活事件：`living_room_recent_snacking`、`living_room_returned_home` 和 `kitchen_after_cooking`；
- 配方、外部 JSON 和自然语言输入的确定性编译；
- 约束布局、支撑关系、几何校验、SVG 俯视预览与 JSON 输出；
- 多 seed 变体、批量生成、可移植清单、内容哈希和离线复现；
- 可选 Isaac USD 导出，以及默认生成的 MuJoCo MJCF；
- 自包含 `.scene.zip` 场景包，包含场景产物、使用到的本地 GLB、许可证字段和 SHA-256 清单。

### 交互和执行

- 版本化的可动部件元数据、`InteractionPlan` 和符号化状态回放；
- 独立的 `InteractionExecutor` 契约、干运行执行器、执行轨迹验证与一致性套件；
- Gymnasium 风格的 `SceneFactoryEnv`，默认可使用 MuJoCo 加载 MJCF 代理碰撞场景；
- 环境后端与交互执行器保持分离：MuJoCo 环境步进不等同于真实机器人完成语义交互。

### Web 和接口

- CLI、Web UI、REST API、LLM 状态检查与场景增量修改；
- 浏览器端 Three.js 预览：存在 GLB 时显示视觉模型，加载失败或没有模型时回退到包围盒代理；
- Web 页面可下载 SVG、MJCF、可选 USD 和可移植场景包；
- 对常见中文事件启用确定性关键词快速路径；未命中时可使用配置的结构化 LLM，LLM 不可用时会降级或报出原因。

## 资产状态

资产注册表目前有 29 条记录：22 条 `validated`、4 条 `ready` 和 3 条待处理或拒绝记录。仓库包含 12 个可随包分发的 GLB 源资产：YCB 物体资产与 8 个带 CC0 来源记录的家具、厨房视觉资产。

视觉资产、场景语义和碰撞几何有意分层：Web 预览使用 GLB；当前 MuJoCo 导出使用注册表尺寸生成的 primitive / 包围盒碰撞代理；Isaac USD 仍是可选高保真路径。因而，当前资产适合场景生成、可视化、物理管线冒烟测试和基础任务接口验证，但不构成精细接触、稳定抓取或可动家具操作的保证。

后续资产准入仍需保留来源、许可证、源文件哈希、尺度、质量、摩擦、视觉网格、碰撞网格、支撑面、抓取区域、关节语义与 QA 报告。

## 验证状态

当前本地完整测试集收集 251 项测试，覆盖确定性编译、外部输入、数据集、资产注册表、规划与执行契约、场景包、MJCF/MuJoCo 集成和 Web 资产访问。Ruff 与 Python 编译检查也在当前环境通过。

这些检查证明的是离线契约、打包输入和 MuJoCo 代理场景的可运行性。它们不替代真实机器人验收。

参考 Isaac Sim 6.0.1 环境已记录 P1-1 Franka mug-lift、P1-2 pick-and-place 和 P1-3 RGB-D 工作流的环境相关验收。P1-4A 真实可动资产绑定的只读检查通过；真实抽屉物理交互和真实机器人执行尚未通过本项目的发布验收门槛。

## 运行时边界

| 路径 | 依赖 | 当前用途 |
| --- | --- | --- |
| 纯 Python 编译 | 无第三方运行时依赖 | 场景、数据集、规划、干运行、SVG、MJCF 和场景包 |
| MuJoCo | `pip install ".[mujoco]"` | `SceneFactoryEnv` 默认物理环境、MJCF 加载和代理碰撞步进 |
| 浏览器预览 | 支持 WebGL 的浏览器 | Three.js + 本地 GLB 视觉预览 |
| Isaac Sim | 单独配置的 Isaac 环境与官方资产 | USD、PhysX、Franka、RGB-D 和资产相关验收 |

即使未安装 MuJoCo，仍可构建 MJCF 文件；只有创建默认 `SceneFactoryEnv` 后调用 `reset()` 时才会加载 MuJoCo。无需该运行时的集成可以显式传入 `DryRunBackend`。

## 近期工作重点

1. 将视觉资产的 primitive 碰撞代理升级为经过 QA 的凸包、多凸体或作者制作碰撞体；
2. 将资产摩擦、质量与材质参数系统化映射到 MuJoCo 和 Isaac PhysX；
3. 为 Franka 和可动家具完成真实接触、可达性、抓取保持和任务 oracle 验收；
4. 扩展真实资产与可操作家具，并为其生成可追溯的批量 QA 证据；
5. 在干净的非 editable 环境中构建、审计并安装 wheel/sdist，完成发布前验证。