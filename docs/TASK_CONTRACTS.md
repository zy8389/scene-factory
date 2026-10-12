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

契约校验只检查语义动作是否属于领域、实体引用是否有效、参数是否为对象。具体的 IK、碰撞、接触稳定性和任务成功率仍由对应仿真后端验收。
