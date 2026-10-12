# 操作契约

场景资产的 `grasp_region` 描述规划器可以尝试的局部区域，不等同于机器人已经在 Isaac Sim 中抓取成功。当前契约字段为：

- `type`：区域语义，例如 `rim_grasp`、`edge_grasp`、`side_grasp`；
- `center`、`size`：以资产局部坐标表示的有界区域，单位为米；
- `approach_axis`：推荐接近方向；
- `allowed_actions`：当前允许的动作集合，通常为 `grasp`、`lift`、`place`。

厨房/餐厅程序化资产 `kd_bowl_01`、`kd_plate_01`、`kd_bottle_01` 和 `kd_box_01` 已注册这些契约。网页 `/api/assets` 和场景生成结果的 `assets` 字段会原样返回 `grasp_region`，并提供 `manipulation.graspable`、`manipulation.validated` 和 `manipulation.allowed_actions` 摘要。

运行离线检查：

```powershell
python tools/validate_manipulation_contracts.py `
  --output outputs/manipulation_contracts.json
```

检查通过只表示元数据完整、碰撞资产已就绪。它不能替代 Franka 的可达性、闭合力、接触稳定性和放置验收；这些仍需单独的仿真任务运行。
