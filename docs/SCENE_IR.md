# 通用 Scene IR

`SceneIR` 是自然语言需求进入确定性场景编译器前的领域无关中间表示。它包含实体、关系、约束、任务和假设，不包含 USD、MuJoCo 或 Isaac 的渲染细节。

现有 `SceneIntent` 可以通过 `SceneIR.from_intent()` 升级。`room_type` 会映射到领域包，例如 `kitchen` 映射到 `home`，`warehouse` 映射到 `warehouse`。之后领域包可以补充领域词汇、动作集合和约束规则，而不需要修改核心布局器。

```python
from scene_factory import SceneIR

ir = SceneIR.from_intent(intent)
ir.validate()
payload = ir.to_dict()
```

当前 IR 只负责语义和结构校验；真实布局、碰撞、物理和机器人可达性仍由现有后端分别验收。
