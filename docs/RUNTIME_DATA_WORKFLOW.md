# 候选场景的真实运行与证据

这一步把已完成的分层采样集合接到真实 Isaac Sim / PhysX。当前支持
Franka 抓杯和抓取放置两个任务，采用现有脚本控制器，顺序运行单个 worker。
不需要 LLM，也不需要启动 RGB-D 渲染。

## 使用

先验证五个初始布局、每个布局的两个任务：

```powershell
scene-factory dataset sample examples/stratified_dataset/plan.json --output outputs/stratified
scene-factory dataset sampling-validate outputs/stratified
scene-factory dataset run-isaac outputs/stratified `
  --output <ASCII_RUNTIME_DIR>/mug-pilot `
  --limit-layouts 5 `
  --asset-root <OFFICIAL_LOCAL_ASSETS>
```

确认流程和失败证据完整后，以新的输出目录运行全部二十个布局、四十个任务组合：

```powershell
scene-factory dataset run-isaac outputs/stratified `
  --output <ASCII_RUNTIME_DIR>/mug-acceptance `
  --limit-layouts 20 `
  --asset-root <OFFICIAL_LOCAL_ASSETS>
scene-factory dataset runtime-validate <ASCII_RUNTIME_DIR>/mug-acceptance
```

`--isaac-python` 可以指定已安装的 Isaac Python；未指定时复用现有环境探测。
Windows 的运行输出和官方资产入口须使用纯 ASCII 路径。把上例的占位符替换为
自己的运行目录和官方 Local Assets 根目录；路径含中文时可建立 ASCII 目录 Junction。
资产根目录下面应包含
`Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd`。

每个阶段默认最多等待 300 秒，可用 `--stage-timeout` 调整。重启中断的批次时
使用相同参数和 `--resume`；已完成的证据须通过离线重验才能跳过，来源、代码、
资产或参数变化应另建运行目录。

## 流程与判定

1. 校验原集合、选择索引和文件哈希，按初始配置配对抓杯与放杯任务。
2. 复制候选输入到新的运行目录，记录源场景身份及当前使用资产的元数据和文件哈希。
3. 在独立进程导出 USD，再启动独立 PhysX 进程记录静置轨迹。
4. 检查最后一个稳定窗口的线速度、角速度、位置漂移、实际支撑边界及物体底面。
5. 物理检查通过后，在稳定后的 USD 中启动官方 Franka 和现有控制器。
6. 记录逐步观测、动作、关节状态、接触、终止标记及失败原因，并离线重算任务判定。
7. 写入关联源场景的质量报告和批次索引；成功、失败和未验证分别统计。

默认物理门槛是 240 步静置、末尾 60 步的稳定窗口，步长 1/60 秒；线速度
不超过 0.01 m/s、角速度不超过 0.1 rad/s、位置漂移不超过 0.005 m、
支撑误差不超过 0.01 m。它们是这一小批刚体场景的验收配置，不是所有资产的
通用阈值。连续帧缺失、非有限状态、报告与输入不匹配、子进程异常退出都不能通过。

动态位姿和速度从有效 PhysX 句柄直接读取，角速度单位为 rad/s。USD 中的速度
属性仅用于诊断：刚体休眠后可能仍保留休眠前的旧值。测量接口显式关闭接触
传感器准备，且核对读取前后休眠属性没有变化；USD 包围盒对应的位姿也与实时
物理位姿逐帧核对。检查过程不通过调整休眠或放宽阈值制造稳定性通过。

任务成功需要真实接触和抬升证据，放置还需要释放与稳定证据。只有末态位姿或
进程退出码正常不足以证明成功。控制器失败是该控制器在该配置的结果，不能据此
证明所有策略都不可行。物理失败时任务保持未验证，不以干运行替代实际执行。

## 数据边界

原候选集合、清单和质量文件保持原样。新的运行目录保存场景快照、运行报告、
JSONL 轨迹、质量结果及中文预览，并通过原场景 ID 和文件哈希关联候选。
现有 `dataset audit` 仍按原候选的布局证据工作；新增的 `runtime-validate`
检查这次运行的证据和质量，不自动把旧候选或旧审计报告升级。

**证据完整**与**任务成功**是两个独立结论。完整记录的失败样本可以通过证据校验，
但其任务质量仍为失败。批次完成不表示四十个任务都成功，更不表示已成为训练集。

本阶段导出物理与机器人状态轨迹；RGB-D、视频、任意策略客户端、完整运动规划和
其他机器人不属于本步骤。新任务扩展应保持成功、失败和未运行的明确区分。

## 独立控制器对照

任务可在 `task.controller` 中指定严格校验的到达阶段、手臂驱动和夹持保持参数。
未指定时保留原控制行为。新的跟踪配方和[分层计划](../examples/franka_tracking/README.md)
复用原来的初始布局分布，独立设置手臂刚度及保持闭合余量；原配方的目标和门槛不变。
驱动参数设置后使用实际运行 API 读回，参数及读回结果保存在每帧运行摘要中。
