# 跨领域任务契约

`TaskGraph` 把机器人动作从具体场景中抽出来。每个动作包含动作名、执行者、主体、目标、参数、前置条件和效果；领域包决定动作是否可用，Scene IR 决定引用的实体是否存在。

```python
from scene_factory import TaskGraph

graph = TaskGraph.from_scene_ir(scene_ir, [
    {"action": "navigate", "actor": "robot_1", "target": "box_1"},
    {"action": "grasp", "actor": "robot_1", "subject": "box_1"},
    {"action": "place", "actor": "robot_1", "subject": "box_1"},
])
```

契约校验检查 IR 本身是否有效、v1 版本、领域一致性、动作集合、实体引用、有限 JSON 参数以及
前置条件/效果的字符串数组类型。前置条件和效果目前是语义标签，不进行逻辑求解或执行。

`TaskGraph` 当前表示有序动作列表，尚未接入 `InteractionExecutor` 或 Isaac 控制器。
仓库移动操作、碗和盒子操作不能因契约校验通过而视为可运行。
现有网页真实执行仍限制为 Franka + YCB 单杯；新增机器人或任务需要单独接入控制器、
碰撞/接触检查和任务结果判定，再记录真实物理证据。
