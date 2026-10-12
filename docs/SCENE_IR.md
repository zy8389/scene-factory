# 通用 Scene IR

`SceneIR` 是独立的领域无关语义接口。它包含实体、关系、约束、任务和假设，不包含 USD、MuJoCo 或 Isaac 的渲染细节。当前主编译入口仍使用 `SceneIntent` / 配方；IR 尚未作为布局器或机器人执行器的直接输入。

现有 `SceneIntent` 可以通过 `SceneIR.from_intent()` 升级。`room_type` 会映射到领域包，例如 `kitchen` 映射到 `home`，`warehouse` 映射到 `warehouse`。之后领域包可以补充领域词汇、动作集合和约束规则，而不需要修改核心布局器。

```python
from scene_factory import SceneIR

ir = SceneIR.from_intent(intent)
ir.validate()
payload = ir.to_dict()
```

当前 IR 只负责语义和结构校验；真实布局、碰撞、物理和机器人可达性仍由现有后端分别验收。

`from_dict()`、`from_intent()`、`validate()` 与 `to_dict()` 检查 v1 版本、领域与场景类型一致性、
实体字段、正且有限的三维尺寸、关系/约束/任务引用，以及可序列化且有限的参数。
`room_bounds` 的尺寸须与场景尺寸一致，任务动作须属于当前领域。
未知约束类型与非空关系名称可保留为扩展语义；校验通过不代表布局器已经实现该约束或关系。
