# Franka 跟踪与夹持保持对照

这个示例复用原分层采样的起点和配额，使用两个独立配方：

- `kitchen_franka_mug_lift_tracking_data`
- `kitchen_franka_mug_pick_place_tracking_data`

与原 `_data` 配方相比，物体、位姿采样、目标、相机和成功条件相同。
独立控制配置将手臂刚度设为 2000，夹持保持闭合余量设为 0.012 m；
预抓取位置门槛仍为 0.04 m，朝向门槛仍为 0.15 rad。
原配方与默认控制器继续使用刚度 1000 和余量 0.007 m。

```powershell
scene-factory dataset sample examples/franka_tracking/plan.json --output outputs/franka-tracking
scene-factory dataset sampling-validate outputs/franka-tracking
scene-factory dataset run-isaac outputs/franka-tracking --output <ASCII_RUNTIME_DIR>/franka-tracking --limit-layouts 5 --asset-root <OFFICIAL_LOCAL_ASSETS>
scene-factory dataset runtime-validate <ASCII_RUNTIME_DIR>/franka-tracking
```

先检查五个布局的十个任务组合，再以相同配置使用 `--limit-layouts 20 --resume`
扩到四十个组合。运行前后代码、输入和资产指纹必须匹配。

`task.controller` 支持严格校验的可选参数：

| 参数 | 默认值 | 用途 |
| --- | ---: | --- |
| `pre_grasp_position_tolerance_m` | 0.04 | 预抓取位置门槛 |
| `approach_position_tolerance_m` | 0.025 | 接近位置门槛 |
| `max_reach_steps` | 180 | 每个到达阶段的步数上限 |
| `grasp_hold_closing_margin_m` | 0.007 | 接触建立后保持闭合余量 |
| `arm_stiffness` | 1000 | 手臂位置驱动刚度 |
| `arm_damping` | 200 | 手臂位置驱动阻尼 |
| `arm_max_force` | 2000 | 手臂位置驱动最大力 |

设置进入场景快照并记录在每帧运行摘要中。驱动参数从实际 Isaac 运行接口读回，
与请求不符时拒绝运行验收。该示例只用于已验证的 Franka/YCB 杯子仿真切片，
其他物体或机器人应独立验证控制配置。
