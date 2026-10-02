# 数据路线：质量审计与场景筛选

本阶段把生成结果变成**有质量边界、可筛选、可统计的数据候选集**。
它不采集机器人轨迹，不运行物理仿真，也不把离线场景称为训练就绪数据。
全部检查可以在无 GPU、无 NumPy、无模拟器、无网络的核心环境执行。

## 先生成，再筛选

```powershell
scene-factory batch --recipe living_room_recent_snacking --count 20 --seed-start 1000 --output outputs/data/snacking
scene-factory dataset validate outputs/data/snacking
scene-factory dataset reproduce outputs/data/snacking
scene-factory dataset audit outputs/data/snacking --minimum-level layout --deduplicate --output outputs/reports/snacking-quality.json
```

`audit` 不改写、不删除源数据。报告必须写到源数据集目录以外，避免污染原有
目录契约。完整但混有布局失败场景的数据集可以被筛选；缺少种子、未生成完成、
残留暂存文件、文件损坏、哈希不匹配、越界路径等情况则拒绝整个候选集。

- `validate`：原有完整性及全部场景的布局有效性检查，语义不变。
- `reproduce`：原有离线重建及语义指纹比较，语义不变。
- `audit`：先验证数据完整性，再按最低质量层级筛选，并统计失败原因和覆盖情况。
- `audit` 返回码 `0`：成功审计且至少有一个被选中的场景，**不代表所有源场景通过**。
- 返回码 `2`：源数据不能安全使用，或没有满足条件的场景；`1` 表示操作错误。

## 稳定生成与随机化配方

布局器在单个物体采样耗尽时，最多尝试 16 次完整布局。重试会重新采样已有的
非固定物体位置，并保留已选资产、回退资产和固定设施；每个物体每次最多采样
192 次。固定物体碰撞、资产缺失等明确错误仍立即报告，不无限重试。
第一次成功的布局继续使用原来的采样过程，已成功种子的布局与复现指纹保持一致。

重试时，带区域限制的物体会在该区域与支撑面可用范围的交集中采样，并继续
检查最终世界坐标、碰撞和支撑边界。这支持很小的起点区域及旋转支撑面。

| 用途 | 抓杯配方 | 抓取放置配方 |
| --- | --- | --- |
| 固定验收基准 | `kitchen_franka_mug_lift` | `kitchen_franka_mug_pick_place` |
| 随机化数据候选 | `kitchen_franka_mug_lift_data` | `kitchen_franka_mug_pick_place_data` |

数据配方保持同一资产、机器人底座、岛台、杯子方向和任务目标，只随机采样杯子
起点：世界坐标 X 为 0.56–0.64 米，Y 为 -0.08–0.02 米。放置任务的起点始终在
目标区域外。固定基准配方仍使用原固定位置，一般任务提示仍匹配原基准配方。

```powershell
scene-factory batch --recipe kitchen_franka_mug_lift_data --count 20 --seed-start 1000 --output outputs/data/mug-lift
scene-factory dataset audit outputs/data/mug-lift --deduplicate --output outputs/reports/mug-lift-quality.json
scene-factory dataset reproduce outputs/data/mug-lift
scene-factory batch --recipe kitchen_franka_mug_pick_place_data --count 20 --seed-start 1000 --output outputs/data/mug-placement
```

这一步增加的是起点位置覆盖。同 seed 可离线复现，但资产类别、机器人类型和
目标位置没有随机化。新起点仍需要逐场景验证物理稳定性、抓取与放置成功，质量报告
中的物理与任务层级继续为 `not_verified`。

## 三层质量，不混淆“通过”和“没测”

每个新场景有 `quality.json`，场景 ZIP 内也有 `scene/quality.json`。
网页显示质量边界并提供报告下载，Python 中可直接读取 `BuildResult.quality`。

| 层级 | 本阶段的证据 | 可能状态 |
| --- | --- | --- |
| `layout` | 已记录的房间边界、包围几何重叠、支撑位置校验 | `passed` / `failed` |
| `physics` | 尚未运行碰撞体质量、静置及动态稳定性验证 | `not_verified` |
| `task` | 尚未运行机器人可达性、接触交互及任务成功验证 | `not_verified` |

这不是资产许可、真实网格碰撞精度或机器人能力的认证。`layout` 聚合已有
`validation.json`，不重新运行模拟器。最高可声明的层级为 `layout`，布局失败
时为 `none`。旧 `valid` 字段继续只表示原来的布局校验。

```powershell
scene-factory dataset audit outputs/data/snacking --minimum-level physics
scene-factory dataset audit outputs/data/snacking --minimum-level task
```

这两个严格门槛目前会得到空筛选结果及 `physics_not_verified` / `task_not_verified`，
而不是把“未运行”当作“通过”。手改 `quality.json` 或仅重新计算它的哈希不能
提升证据层级：读取时还会与源布局报告重新推导的结果比对。

## 如何看审计报告

报告版本为 `scene_factory.dataset_audit.v1`；单场景质量版本为
`scene_factory.quality.v1`。

- `dataset_id`、`manifest_sha256`：关联源数据集及本次清单快照。
- `generation`：生成状态、错误类型、预期与实际数量、缺失种子；未完成的批次不会放行已有前缀。
- `summary.layer_counts`：三个层级各有多少通过、失败、未验证场景。
- `summary.failure_counts`：按原始错误码统计，**同一场景同一错误只计一次**。
- `summary.failure_category_counts`：边界 `bounds`、重叠 `overlap`、支撑 `support`、
  未给出具体原因的布局失败 `layout`、未来未知检查 `other`。未知错误仍拒绝，原码保留。
- `summary.warning_counts`：警告单独统计，不自动变成失败。
- `summary.rejection_counts`：按筛选拒绝原因统计，包括质量不足和精确重复。
- `scenes`：逐场景质量、是否满足质量门槛、是否最终选中、拒绝原因、重复来源。
- `selected`：原始场景记录的筛选清单，保留种子、相对路径和哈希。
- `diversity.all` / `diversity.selected`：源集合及筛选后集合的场景数、精确布局数、
  资产种类数、房间/事件/配方分布；类别和资产计数是**物体实例数**，不是场景数。

`selected` 中的文件路径仍相对于**原数据集根目录**，不是报告所在目录。
它是选择索引，不是新的独立数据集，不能直接作为 `manifest.jsonl` 替换原清单；
它也不是轨迹或训练格式。使用前应确认源清单哈希并重新校验源文件。
源数据变化后需要重新审计，旧报告不会自动刷新。

## 精确重复与确定性

`--deduplicate` 可选开启，默认只统计而不去重。内容指纹忽略场景 ID、随机种子、
说明文字，按物体 ID 排序；其他布局字段（含任务、物体 ID、资产、位置和关系）保留。
只剔除这种定义下完全相同的布局，不做近似相似度、几何同构或语义多样性判断。

按清单的种子升序保留**第一个满足质量门槛**的场景。失败的重复样本不会挤掉
后续合格样本。筛选不删除源文件，也不更改原来的复现指纹。
相同的完整有效输入与筛选参数在不同目录得到相同报告；这不保证物理仿真跨版本或硬件一致。

## v1 兼容方式

旧数据没有 `quality.json` 时，审计直接从已有布局与校验文件推导质量。
新数据使用可选顶层 `quality_report: {path, sha256}` 描述附加文件，原清单
`files` / `sha256` 键集合及语义指纹定义不变；旧 v1 读取器可忽略该描述符。
新读取器验证描述符的路径、文件哈希、版本和证据一致性。

`dataset_id` 是原有生成调用身份，不是全内容哈希；请结合清单及文件哈希使用。
报告不证明外部输入本身可信，也不读取用户附加的任意“物理成功”声明。

## 分层配额与覆盖报告

`scene_factory.sampling_plan.v1` 按配方和起点区域定义候选池与合格配额。
示例 [plan.json](../examples/stratified_dataset/plan.json) 将抓杯、抓取放置各分为左右两组，
每组生成 40 个候选，选出 10 个不重复的布局合格场景，共生成 160 个、选出 40 个。

```powershell
scene-factory dataset sample examples/stratified_dataset/plan.json --output outputs/stratified
scene-factory dataset sampling-validate outputs/stratified
scene-factory dataset sampling-reproduce outputs/stratified
scene-factory dataset sample examples/stratified_dataset/plan.json --output outputs/stratified --resume
```

计划字段：`minimum_level` 指定最低质量层级（默认 `layout`）；`strata` 是有顺序的分组列表。
每组的 `id` 是跨平台安全目录名，`recipe` 指定原有配方，`quota` 是最终合格数量，
`candidate_count` 是有限的候选预算，`seed_start` 指定首个种子。可选 `filter` 使用
`object_id` 和世界坐标 `region_xy` 过滤物体中心，不修改配方或原候选布局。
区域采用左闭右开边界，同一配方各组的种子区间必须不重叠。

按计划顺序、组内种子升序选择，复用原质量门槛和精确内容指纹，并在组间去重。
任何一组配额不足时，整体标记未完成，`selected` 为空；候选及每组缺口仍保留。
不按候选通过率替换配额，也不无限补采。修改配额、预算或区域时使用新输出目录。

`collection.json` 的版本为 `scene_factory.sampling_collection.v1`。主要内容包括：

- `sources`：规范化配方和资产注册记录的语义哈希，恢复时检查它们是否变化；不代表资产二进制或物理验收证明。
- `summary`：候选总数、总配额、能满足条件的暂选数量 `qualified_count`、实际放行数量 `selected_count`。
- `strata`：每组候选、质量通过、区域匹配、去重、可用数量、配额是否满足及拒绝原因。
- `strata[].position_bounds_xy`：该组暂选起点的实际最小/最大坐标，用于检查覆盖，不能解释为机器人工作范围。
- `coverage`：已放行样本的配方、房间、事件分布与资产实例数。未完成集合不发布部分覆盖。
- `selected`：每条包含分组、子数据集位置、内容指纹和原始场景记录；文件路径相对子数据集，不是集合根目录。

子目录 `datasets/<分组 id>/` 继续使用原数据集 v1 格式。`sampling-validate` 重新审计
候选文件、哈希、门槛、区域与配额，并检查存储的选择索引与推导结果一致；不重写文件。
`sampling-reproduce` 在集合验证通过后，复用原复现流程重建所有候选，检查语义指纹。
它不是物理回放；如果候选池中保留了被拒绝的布局失败样本，原复现检查仍可能失败。
复现使用当前默认资源库；定制配方或注册表需在匹配的 `SCENE_FACTORY_HOME` 环境下运行。

生成中断可用原计划 `--resume` 恢复。恢复前拒绝损坏候选、不同计划或变化的配方/注册记录；
已完成的候选批次不重写，未完成批次复用原恢复机制。数据集合目录不是原单配方数据集，
请使用 `sampling-validate` 检查集合；原 `dataset validate` 仍可用于其各个子数据集。
采样结构见 [sampling_plan.schema.json](../schemas/sampling_plan.schema.json)，跨字段配额、区间和有限数值由运行时进一步检查。

## 后续数据路线

1. 根据覆盖报告增加房间、任务和资产的采样分组，再以实际任务结果评估难度。
2. 接入实际运行且绑定场景指纹的物理与任务证据，建立严格筛选入口。
3. 复用已有 `EpisodeRecorder` 和轨迹加载/回放协议，采集可回放、带失败标签的小批示教。
4. 验证任务与轨迹后再做训练/验证/测试划分及防泄漏检查，不把当前场景索引冒充训练集。
