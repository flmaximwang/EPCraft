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
| `gap` | 文字靠泳道那一侧的边缘与泳道边缘的水平间距 (pt) | 4 |
| `marker_lane` | marker 泳道是**左起第几条**参考线（1 起）；`0` = 第 1 条左边一条泳道宽处 | 1 |
| `marker_lane_x` | 直接给 marker 泳道的 x（给了就压过 `marker_lane`） | 无 |
| `lane_width` | 泳道宽 (pt)，只用于算泳道边缘；不给就按「胶图宽 ÷ 泳道数」 | 自动 |
| `lane_edge` | `inner` = 用泳道中心当边缘（不偏半个泳道宽） | 无 |
| `labels` | **从上到下**，每个条带一项：`{"text":..., "size":..., "color":..., "font":...}`（后三个可省） | — |

## 3. 泳道配置（`lanes.json`）

| 键 | 含义 | 默认 |
|---|---|---|
| `font` / `size` / `color` | 同上的默认值 | 16 / `#000000` |
| `gap` | 文字底边离**胶图顶边**的距离 (pt) | 0 |
| `align` | `center` / `left` / `right` | `center` |
| `labels` | **从左到右**，每条泳道一项；`text` 留 `""` = 这条泳道不写字 | — |

## 4. 对齐规则（marker 标签往哪边靠）

marker 泳道中心在**胶图左半边** ⇒ marker 标签 **右对齐**、右边缘贴泳道左边；
在**右半边** ⇒ 自动翻成**左对齐**、贴泳道右边。间距都是 `gap`。
这条规则是「标签写进图外面、不压条带」的实现方式；marker 泳道在中间时按半边判，要更细地控就改用
`marker_lane_x` 自己给一个 x。

## 5. 本机实例（2026-10-12 的实跑，路径是这台机器的）

工程与配置都在纯化记录的 `asset/` 目录下：

```
/Users/maxim/Documents/Lab/zsqlab_10_gammaPFD-Fiber_2026.06.25/Obsidian_zsqlab_10/Logs/2026/10/09/Purification-010/asset/
├── Probe1.1_2026.10.12.vectorcraft          # 标好参考线的工程
├── 10%-Tris-Tricine.json                    # → --markers（11 条分子量，250…10 kD）
├── Probe1.1_2026.10.12.json                 # → --lanes（8 条：M + Total/Sup/Soni/HP 各两条）
└── Probe1.1_2026.10.12_labeled.vectorcraft  # 输出
```

可复制命令：

```bash
cd "/Users/maxim/Documents/Lab/zsqlab_10_gammaPFD-Fiber_2026.06.25/Obsidian_zsqlab_10/Logs/2026/10/09/Purification-010/asset"
python3 label_gel.py Probe1.1_2026.10.12.vectorcraft \
        --markers "10%-Tris-Tricine.json" \
        --lanes   "Probe1.1_2026.10.12.json" \
        --clear-text-in "Layer 1" \
        --preview gel_labeled.png
```

`--clear-text-in "Layer 1"` 只在第一次（清掉手打的老标签）需要，之后不用带。
