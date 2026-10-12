# 桌沿杯子与当前任务的优先级判断

先生成可复现的布局和任务情境，再向已配置的 LLM 提问。示例默认杯子距注册的
可用支撑面边缘 5 mm，设定正在向边缘移动；机器人正在整理遥控器，可以安全暂停。
杯子的运动是情境假设，未测量速度、未预测滚落时间，也未执行机器人动作。

```powershell
python -m scene_factory priority build --output outputs/task-priority --clearance-m 0.005
python -m scene_factory priority ask outputs/task-priority/priority_scenario.json
```

生成 `layout.json`、`preview.svg`、MJCF、场景 ZIP 和 `priority_scenario.json`。
AI 回答保存为 `priority_decision.json`，包括选择、理由、需要补充的信息与后续步骤。
模型可以选择先处理杯子、继续当前任务或请求更多信息。生成无需调用模型；询问
需要配置 `config/llm.json` 和密钥环境变量，接口失败时不会把规则答案伪装成 AI 回答。

网页左侧展开“复杂情境：桌沿杯子与当前任务”，填写当前任务、间隙、杯子运动
情境和暂停后果，点击生成。随后在预览下方点击“询问 AI 的优先级判断”。

可以生成对照情境，检查模型是否根据任务后果改变判断：

```powershell
# 安稳杯子：距支撑边缘 20 cm，静止。
python -m scene_factory priority build --output outputs/task-priority-safe --clearance-m 0.20 --motion-state stationary

# 当前任务涉及即时安全，不能立刻暂停。
python -m scene_factory priority build --output outputs/task-priority-critical --current-task "扶稳正在倾倒的热锅。" --urgency safety_critical --unsafe-to-pause --pause-consequence "立即松手会让热锅翻倒，可能烫伤旁边的人。"
python -m scene_factory priority ask outputs/task-priority-critical/priority_scenario.json
```

当前任务是提供给模型的结构化上下文；示例没有渲染热锅或复现它的物理状态。
间隙相对于注册的支撑面定义，其范围可能小于外观模型的完整桌面。静态靠边
不能证明杯子即将掉落。滚动、倾斜、真实时间余量和干预结果需要后续物理验证。
情境绑定布局哈希；修改布局后必须重新生成情境再询问，避免使用过期位置。
