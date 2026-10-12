# 标记约定与两个 JSON

## 1. 位置从哪来：两种标记方式

| 来源 | 写法 | 竖着的东西 | 横着的东西 | 顺序 |
|---|---|---|---|---|
| 参考线（默认） | `--marks guides` | 垂直参考线 = 一条泳道中心 | 水平参考线 = 一条 marker 条带高度 | 竖线按 x 从小到大、横线按 y 从小到大 |
| 图层里的短线 | `--marks layer:<图层名>` | 高 ≥ 宽的小图形取中心 x | 宽 > 高的小图形取中心 y | 同上 |

两种方式**都实测过**（同一个工程、同一批标签，逐条比对 x/y/对齐方式完全一致）。
参考线是首选：在 GUI 里拖一下就有，图层短线要多画一遍图形、还得记住哪根是哪种。

⚠️ **marker 泳道也要一条竖线**：脚本把竖线当泳道的完整清单，少一条就报错。

## 2. marker 配置（`markers.json`）

| 键 | 含义 | 默认 |
|---|---|---|
| `font` | 字体名，空 = 应用默认 | 无 |
| `size` | 所有 marker 标签的字号 (pt) | 16 |
| `color` | 默认颜色 | `#000000` |
| `gap` | 文字靠泳道那一侧的边缘与**泳道带边缘**的水平间距 (pt) | 4 |
| `marker_lane` | marker 泳道是**左起第几条**参考线（1 起）；`0` = 第 1 条左边一条泳道带处 | 1 |
| `marker_lane_x` | 直接给 marker 泳道的 x（给了就压过 `marker_lane`） | 无 |
| `lane_width` | 泳道带宽 (pt)，手给的值；**不给就按相邻泳道中心距的中位数自动算** | 自动 |
| `lane_edge` | `inner` = 用泳道中心当边缘（不偏半个带宽） | 无 |
| `labels` | **从上到下**，每个条带一项：`{"text":..., "size":..., "color":..., "font":...}`（后三个可省） | — |

**`gap` 是量到「带边」的，所以带宽怎么算直接决定它看起来有没有生效。** 带宽 = 相邻泳道中心距
（`lane_pitch()`，取中位数以抗一条拖歪的参考线）；**不要用「胶图宽 ÷ 泳道数」**——胶图对象常比泳道区宽
（照片自带白边），那样算会把带宽放大一倍以上，于是标签飘远、`gap` 像没生效。实测对照：同一个 `gap: 4`，
带宽按泳道距算 = 36.6 pt 时空隙 **4.5 pt**；按胶图宽算 = 64.4 pt 时空隙 **18.0 pt**。

## 3. 泳道配置（`lanes.json`）

| 键 | 含义 | 默认 |
|---|---|---|
| `font` / `size` / `color` | 同上的默认值 | 16 / `#000000` |
| `gap` | 文字底边离**胶图顶边**的距离 (pt) | 0 |
| `align` | `center` / `left` / `right` | `center` |
| `labels` | **从左到右**，每条泳道一项；`text` 留 `""` = 这条泳道不写字 | — |

## 4. 对齐规则（marker 标签往哪边靠）

marker 泳道中心在**泳道区的左半边** ⇒ marker 标签 **右对齐**、右边缘贴泳道带左边；
在**右半边** ⇒ 自动翻成**左对齐**、贴泳道带右边。间距都是 `gap`。

两边各有一处「拿什么当中线/边缘」的选择，都实测踩过：

- **中线按泳道区算**（`(min+max)/2` of 泳道参考线），**不按胶图对象的中线**：胶图对象比泳道区宽时，
  按它会把一个明确在左边的泳道误判成右半边，标签就翻到另一边去。
- **带边按带宽算**：`lane_edge: "inner"` 则取消这半个带宽、直接用泳道中心当边缘。
  marker 泳道不在最左/最右、想自己给位置时，用 `marker_lane_x`。

## 5. 本机实例（2026-10-12 的实跑，路径是这台机器的）

工程与配置都在纯化记录的 `asset/` 目录下：

```
/Users/maxim/Documents/Lab/zsqlab_10_gammaPFD-Fiber_2026.06.25/Obsidian_zsqlab_10/Logs/2026/10/09/Purification-010/asset/
├── Probe1.1_2026.10.12.vectorcraft          # 标好 11 横 + 8 竖参考线的工程
├── 10%-Tris-Tricine.json                    # → --markers（11 条分子量，250…10 kD）
├── Probe1.1_2026.10.12.json                 # → --lanes（8 条：M + Total/Sup/Soni/HP 各两条）
├── Probe1.1_2026.10.12_labeled.vectorcraft  # 输出（工程）
└── Probe1.1_2026.10.12_labeled.png          # 输出（导出图，已裁白边）
```

可复制命令（子命令名是 `label`）：

```bash
cd "/Users/maxim/Documents/Lab/zsqlab_10_gammaPFD-Fiber_2026.06.25/Obsidian_zsqlab_10/Logs/2026/10/09/Purification-010/asset"
python3 label_gel.py label Probe1.1_2026.10.12.vectorcraft \
        --markers "10%-Tris-Tricine.json" \
        --lanes   "Probe1.1_2026.10.12.json"
# 这一跑会产出 Probe1.1_2026.10.12_labeled.vectorcraft + 同名 .png（已裁掉四周白边）

# 第一次还要清掉手打的老标签时，加 --clear-text-in "Layer 1"（只删文字、不动胶图）
# 只想要工程、不要导出图：加 --no-preview
# 导出图换个路径：加 --preview /tmp/xxx.png
```

新建一套配置：`python3 label_gel.py generate_template --dir . --lanes 8`（已有同名文件时默认拒绝覆盖）。
自检（不用开 VectorCraft）：`python3 label_gel.py selftest`。
