# Spec: ep-craft — 裁切框写成「0° 矩形 + 旋转」，让检查器显示真实角度

Status: implemented 2026-10-31 · Scope: `skills/ep-craft` · Written 2026-10-31 · Builds on `specs/ep-craft-crop-artboard.md`

## 1. 问题

2026-10-31，`DGC_2026.10.31/IMG_1586`。用户在 Crop 画板上选中 `crop` 框，**检查器里的旋转是 0°，画布上的框却是斜的**。用户据此判断「这不是矩形，是个歪掉的 90° 四边形」，随后花了两轮才排除。

实测该文档（`fig.vectorcraft`，VectorCraft 0.19.0）：

| 检查项 | 结果 |
|---|---|
| 四个内角 | 均 90.000000°（余弦残差 ≤ 3e-16） |
| 两组对边 | 243.779528 pt / 177.165354 pt，各自相等 |
| 长宽比 `h/w` | 0.7267441860465117 —— 与预设 6.25/8.6 逐位相同 |
| `live` 字段 | `{shape:"rectangle", w, h, radii, xf:[0.998492, 0.054898, −0.054898, 0.998492, −388.56, 77.50]}` —— **没有 `rotation`** |
| `xf` 的正交性 | a²+b² = 1.000000（纯旋转，无剪切）；`atan2(b, a)` = **+3.147°**（y 向下，即屏幕上顺时针） |

所以形状确实是精确矩形，3.147° 正是胶自身的倾角，与 stage 1 打印的 `角度: 竖边拟合 -3.147°` 一致。**错的是表示方式，不是几何。**

成因在写入端：`vc_file.rect()`（`scripts/vc_file.py:281`）把 4 个角直接换算成 `w/h/xf`——它从模板剪切组深拷贝那条 `live.shape:"rectangle"` 路径，然后只 patch `w/h/xf` 和锚点，**从来没有把角度写成一个 app 能读的旋转**。app 读不到 `rotation`，检查器就报 0°。

**根因（2026-10-31 实测 0.19.0，见 §3）：** app 里的矩形角度存在**节点层**的 `bbox_angle` 上（`id / name / appearance / kind / bbox_angle`），既不在 `live` 里，也不从 `xf` 推。值为 `−atan2(b, a)`（= `vc_file.angle_of_xf(xf)`）；角度为 0 时 app 自己不写这个字段。skill 只写 `xf`，于是形状画对了、角度读不到——检查器 0°，选择框还被画成直的外接框，看起来像多了一个矩形。

### 后果

1. **用户被误导。** 「0° 却斜着」在人的眼里只有一个解释：这不是矩形。
2. **下一步是错的。** 最自然的动作是在检查器里把角度改成 0 —— 那会让导出胶面歪 3°（裁切时按框角度旋转，角度是语义的一部分，不是装饰）。
3. **app 的旋转语义全对不上。** 输入数值角度、绕中心转、对齐，没有一个作用在这个框上。

## 2. 决策

1. **裁切框按 app 的矩形语义写入**：先建 0° 的 `shape.rectangle`，再把旋转**作为旋转**写进去，使检查器显示真实角度、与画布一致。
2. **`vc_file.rect()` 不再以「深拷贝模板剪切组 + patch `xf`」作为输出真值。** 模板可以继续用作字段形状的参照，但角度必须是 app 认识的那个字段/那条命令产出的。
3. **几何不变。** 输出节点仍须是四角 TL TR BR BL、`live.shape == "rectangle"`、`live.w/h` = 两边长、`h/w` = 预设比例。改的只是表示方式；`fig.png` 应与改前逐像素相同。
4. **读回仍以锚点为准。** `rect_corners` / `crop.read_frame` 继续只读路径锚点，不依赖旋转字段——这样无论 app 用哪种表示存盘，裁切都读得回来。
5. **`render` 仍然不依赖 app。** `specs/ep-craft-vectorcraft.md` 决策 2：Python 直接写 `.vectorcraft`，app 只负责导出。所以首选**在写文件时就产出 app 认的字段**；只有在测量证明该字段无法由文件写入时才退到「render 打开文档后补一步旋转」。
6. **只改裁切框。** 泳道标签的旋转（90°）走的是 `text.create` + `object.rotate`，很可能有同一类问题，但那是另一件事：本 spec 只要求把它**量一遍并记进 `references/vectorcraft.md`**，不要求改。

## 3. 实测（VectorCraft 0.19.0，2026-10-31，控制口 7797）

背景：`references/vectorcraft.md` 的结论原是在 0.14 上测的，`doctor` 在警告版本变化。这个版本变化正是要点——0.14 上矩形的角度只在 `xf` 里；0.19 把它搬到了节点层的 `bbox_angle`。

| # | 写法 | 0.19.0 上的结果 |
|---|---|---|
| A（旧） | 深拷贝模板剪切组的矩形路径，patch `w/h/xf`/锚点 | 文件里没有 `bbox_angle`；`document.inspect` 不给 `rotation`；检查器 0°、选择框为直立外接框 |
| B | `shape.rectangle {x, y, width, height, rotation:−3.147}` | 节点带 `bbox_angle:3.147`，`xf` 角度 −3.147；`inspect` 报 `rotation:3.147` |
| C | `shape.rectangle` 建 0° → `object.rotate {angle:3.147}` | 同一节点形状：`bbox_angle:3.147`，`xf` 角度 −3.147 |
| D | 手写文件里补 `bbox_angle` | app 读它（`inspect` 报 `rotation`）；存盘、重开不丢（两轮 save/reopen，四角漂移 0.000000000 pt） |

口径确认（怎么知道 −3.147 才是我们这张框的值）：给 `crop` 框（`xf` 角度 +3.147）分别写 `bbox_angle` ±3.147，再让 app 做 `object.rotate {absolute:true, angle:0}`。写 −3.147 时框正好变成横平竖直（`xf` 角度 0.000000），写 +3.147 时落到 6.294——两倍的角。所以 **`bbox_angle = angle_of_xf(xf)`**。

结论：不需要 B/C 那两条例行命令。直接按 D 写文件即可，`render` 仍然不依赖 app（决策 5 成立）。

## 4. 合同

| 项 | 要求 |
|---|---|
| `vc_file.rect(doc, name, corners, rgb, width)` | 入参、返回节点名不变；输出仍是 `live.shape:"rectangle"`、四角 TL TR BR BL、`w/h` 正确；新增：旋转按 §3 选定的方式写入，**app 存盘后角度不丢** |
| 角度口径 | 与现有 `xf` 一致：`θ = atan2(b, a)`（y 向下、顺时针为正）。写 `shape.rectangle.rotation` 用顺时针值，写 `object.rotate` 传相反数 |
| `vc_file.rect()` 的输出 | 新增节点层 `bbox_angle = angle_of_xf(live.xf)`；角度为 0 时不写（与 app 一致）；模板里残留的先删再算 |
| `rect_corners` / `crop.read_frame` | 不变，只读锚点 |
| `sync` 的矩形校验 | 不变（某角偏 >0.5°、对边差 >0.5% → 报错、什么都不改） |
| 幂等 | render 完立刻 `sync` = 0 ops |
| 旧文档 | 不要求迁移；旧文档里的 `crop` 框照样能读、能拖、能 sync。若用户想让它也显示角度，重新 render 即可 |

## 5. 验收

1. 用 `DGC_2026.10.31/IMG_1586` 重渲染，在 Crop 画板选中 `crop`：**检查器角度与画布斜度一致**（约 3.1°，符号按 app 口径），框的形状、位置、大小与改前完全相同。
2. `fig.png` 与改前逐像素相同（几何没动，只是换了表示）。
3. 在 app 里存盘、关掉、重新打开该文件，角度读数不变。
4. `sync` 立刻跑 = 0 ops。
5. `selftest` 新增一条：写出的矩形节点里，角度可由 app 字段读出，且值等于 `atan2` 算出的角；`rect_corners` 读回的四个角与输入一致（容差 1e-6）。

## 6. 不在范围

- 胶面裁切框**位置与大小**的自动检测（那是另一件事：`--rect`，或用户在 Crop 画板上拖）。
- 标签的旋转表示（决策 6：只测量、记录，不改代码）。
- 把裁切框改成非矩形/自由四边形——矩形是硬要求，`sync` 继续拒收变形框。

## 7. 顺带要记的实测事实（`references/vectorcraft.md`）

- 0.19.0 与 0.14.x 在 `live` 矩形、`rotation`、`object.rotate` 上的一致/差异（§3 的测量表原样录入）。
- 一句话结论：「0.19 的检查器角度读节点上的 `bbox_angle`（= −atan2(b,a)，0 时省略）；skill 写的 `crop` 框以前只有 `xf`，所以显示 0°、选择框是直的外接框」。
