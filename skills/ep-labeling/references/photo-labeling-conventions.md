# 照片标注：配置、文档结构、分析、sync

权威设计在 `specs/ep-labeling-vectorcraft.md`；VectorCraft 文件格式与控制口命令（实测）在 `references/vectorcraft.md`。
本文写的是**现在代码实际怎么做**，数字来自 `tickets/ep-labeling-vectorcraft.md` 各张票的收尾记录。

## 1. 两个 JSON —— 字段表

`templates/` 里有整套示例。下面没列的键（`size` / `color` / `gap` / `font` / `align`、`y_px` / `snap` / `y_frac` /
`x_frac` / `marker_lane_x` / `lane_width` / `lane_edge`）**已不再读取**：字号、字体、颜色在 VectorCraft 里改；
条带与泳道位置在 Workbench 上拖线改（§5、§9）。

### markers.json（或 `data/markers.json` 预设库 + `--preset`）

| 键 | 含义 |
|---|---|
| `labels` | **从上到下**，每项一条标签：字符串，或 `{"text", "rows", "color", "angle"}`。`text` 里必须有分子量数字（`250 kD`、`21/20 kD`），它变成 Workbench 上的 `band <MW>` 线名。`rows: 2` = 这条标签盖两条带（两条 band 线、一个标签，见 special-cases §1）。`color` = 这条 marker 标签在 Figure 上的初始颜色（之后归用户）。`angle` = 这条标签的角度（度，逆时针，见 §8；省略用 `--marker-label-angle`） |
| `marker_lane` | 可选：梯子所在泳道（1 起）。追加到 `lanes.json` `marker_lanes` 候选的**末尾** |
| `_preset` / `_source` / `_说明` | 预设库里用来挑预设（`--preset` 的关键词在 `_preset` 里匹配，须恰好命中 1 个）与留出处 |

条带数之和（`rows` 求和）必须等于梯子泳道上叫出来的峰数，否则 render 拒绝并列出叫出来的 y（不硬配）。

### lanes.json

| 键 | 含义 |
|---|---|
| `labels` | **从左到右**，每条泳道一项：**字符串**，或 `{"text", "angle"}`（名字成为 Figure 上 `label lane n` 的初始文字；`angle` 只改这一条的角度）。`""` = 空白泳道：不画标签、MW 终端表不列。没列到的泳道（`labels` 比泳道少）也当 `""` |
| `marker_lanes` | **候选梯子泳道**（1 起，按优先顺序，如 `[1, 2, 3]`）。render 依次试：第一条叫出的峰数对不上标签（例如胶边损坏）就换下一条，直到配上。**标定只用配上的那一条**（不是多管加权）；之后 marker 泳道由 band 线落在哪条泳道里决定（§5） |

## 2. 胶体预设与成品尺寸

`data/gel_size.json` **必给**、**没有默认**。没有 `--gel-size` 时 `probe` / `render` 一律以
`gel preset required (--gel-size FILE)` 退出。

| 键 | 必给 | 作用 |
|---|---|---|
| `name` | ✓ | 预设名，日志与 `ep-meta` 里都记 |
| `width_cm` | ✓ | **定标**：胶面在每块画板上都是 `width_cm` 宽（pt = cm × 72/2.54），`fig.png` 里横跨 `width_cm/2.54 × dpi` 像素 |
| `height_cm` | ✓ | **定形**：胶面高 = `height_cm − trim_top_cm − trim_bottom_cm`，按 `anchor` 贴探测框。口径 = **孔底到胶下缘**（不含孔间的齿；`mini-gel` = 6.25） |
| `lane_count` | ✓ | **整块胶**的泳道数 = 种子梳子的齿数（裁过的照片用 `--gel-lanes N` 改） |
| `lane_pitch_cm` | ✓ | 孔距**先验**：well_grid 在它 ±20% 里搜间距；render 拿实测孔距与它核对（差 >10% 打 `[!] scale`）。`mini-gel` = **0.52**（2026-10-22 由 0.565 改来，见 special-cases §4） |
| `trim_top_cm` / `trim_bottom_cm` | | 从胶面上/下缘再裁掉的长度，默认 0；命令行 `--trim-top-cm` / `--trim-bottom-cm` 覆盖。别裁到孔（§6） |
| `anchor` | | `bottom`（默认）/ `top` / `center` |
| `_source` / `_说明` | | 这组数的来历 |

**不变式**：加载时校验 `lane_count × lane_pitch_cm ≈ width_cm`（容差 10%），不成立直接报错。
`mini-gel`：15 × 0.52 = 7.8 cm vs 8.6 cm（差 9.3%，在容差内 —— 15 个孔只占约 7.8 cm，两侧各有 0.4–0.6 cm 没有孔）。

`--gel-cm WxH` 只覆盖**已加载预设**里的宽高，不能单独用。

**照片裁过（只含 N 条泳道）→ `--gel-lanes N`**：定标改用 `N × lane_pitch_cm`，梳子也只有 N 齿。
不声明就会拿裁后的画面当整块 8.6 cm 胶，尺寸整体错。

**字号是 pt，与 dpi 无关**：Figure 上的文字按名义字号创建（默认 Arial 8 pt），`--dpi` 只改 `fig.png` 的像素数，
不改物理尺寸。用户在 VectorCraft 里改字号就是改 pt。

## 3. stage 1：胶面（老管线里唯一还在用的一级）

认胶：

- `dark`（默认）：拟合灯箱背景。取样取四边 8%–14% 的**环带**（跳过最外圈相框），两轮最小二乘拟一个平面，
  再判 `像素 < 平面 − max(6, 平面//20)`（`--ink-thr` 改阈值）。中性/银染的胶也成立。
- `chroma`：判 `B−R > 22`。蓝光灯箱下整块屏幕都蓝，所以是备选（`--cue chroma|union`）。
- 掩膜去毛刺，取最大且**不贴画面边**的连通域；裁过的照片找不到时退回「允许贴边取最大块」并打 `[i] 胶面贴着画面边`。
- 倾角 = 左右竖边在中段 20%–80% 行上的最小二乘拟合（`--angle` 强制、`--no-rotate` 不转）。

按预设定形（`gel_area.py`）：

1. 探测框与倾角（`--rect x0,y0,x1,y1` 跳过自动找胶，原图像素）。框上缘 = 第一条覆盖率 ≥90% 的行 = 孔底；
   左/右/下缘按 50% 覆盖率。
2. 定标：`px_per_cm = 探测宽度 / width_cm`（裁过的照片：`/ (N × lane_pitch_cm)`）。
3. 定形：高按预设，贴 `anchor` 那条边。
4. 核对：预设高 vs 探测框推出的高，差 >5% 打 `[!] height`（只警告、不改图）。

产物是一张**胶面图**（PNG，`--dpi`，宽 = `width_cm`），用 magick 旋转、裁切、缩放（`+profile "*"`，不用 `-strip`）。
**旋转后先 `+repage` 再 `-crop`**（2026-10-23 修）：之前没有，IM7 转完留下的虚拟画布偏移让实际裁到的地方比探测框
往右下错了 `((W1−W0)/2, (H1−H0)/2)` 原图 px（1° / 4032×3024 约 1 mm）。修之后转过的照片 render 出来比以前往左上挪这么多，
正好落在框上；之前 render 的文档内嵌图不变，直到用户挪了 Crop 框。
它以内嵌方式进 `fig.vectorcraft`，之后的泳道、条带、剖面全部只读这张图，不再读照片。

**这一级的框可以事后调**：render 把裁切框画在 `Crop` 画板的照片预览上（§4）。用户挪 / 转 / 按 Shift 等比缩放这个框，
sync 就从 `ep-meta.photo` 的**原图**按新框重裁胶面图、原地换像素（§9a）；Workbench 的线按胶面 cm 不动。
框的宽定标（px/cm），高按预设比例（`width_cm : height_cm − trims`），不读画出来的高。亮度/对比度、透视矫正仍不管。

## 4. 文档结构（`fig.vectorcraft`）

单位 pt，原点左上、y 向下；1 cm = 72/2.54 pt。四块画板（顺序固定）：

| # | 画板 | 内容 | 导出 |
|---|---|---|---|
| 0 | `Figure` | 胶面（按保留泳道剪切/拼接）+ 泳道名 + marker 标签，页边距按页面规则 | → `fig.png`（`document.export {artboard: 0, ppi: dpi}`） |
| 1 | `Workbench` | 整块胶面、所有泳道线、积分窗、marker 条带线、叫出来的带的刻度、泳道号 | 不导出 |
| 2 | `Profiles` | 每道一条 `ink` 曲线 | 不导出（用户可手动导） |
| 3 | `Crop` | 原照片预览 + 裁切框 | 从不导出 |

Figure 在 Workbench **下方**（变宽变高不会压到 Workbench），Profiles 在 Workbench 右边，Crop 在 Workbench **左边**
（`x = −(crop_w + BOARD_GAP)`、`y = 0`，Workbench 与 Profiles 怎么变都不挪它）。
Crop 的比例：pt / 原图像素 `s = width_cm·PT / 框宽px`，所以框画出来正好和 Workbench 上的胶面一样宽；画板 = 预览图外扩 0.3 cm。
非打印图层也会被 `document.export` 画出来，所以 Figure 画板上只放可打印的东西。

### 图层与对象名（名字 / 图层就是文档与分析之间的绑定）

| 图层 | 画板 | 锁 / 打印 | 对象 | 用户可改 |
|---|---|---|---|---|
| `gel` | 全部 | 锁定、打印 | Workbench：一张图 `gel`；Figure：每个保留段一个剪切组 `gel seg k` | 否 |
| `analysis` | Workbench | 锁定、不打印 | 泳道号 `tag n`（灰），叫出来的带的短刻度 `peak n`（绿） | 否（每次 sync 重建） |
| `window` | Workbench | 不锁、不打印 | 两条横线 `top`、`bottom`（橙），伸出胶边 0.15 cm 方便抓 | **拖** |
| `lanes` | Workbench | 不锁、不打印 | 竖线 `lane`（青），全窗高；是**分隔线**：相邻泳道共用一条，n 道 = n+1 条 | **拖、加、删** |
| `trimmed` | Workbench | 不锁、不打印 | trim 标记：胶顶上方横跨泳道的灰色短线 `trim`，中点落在哪道就拿掉哪道 | **画、删** |
| `marker bands` | Workbench | 不锁、不打印 | 横线 `band <MW>`（品红），伸出 marker 泳道 0.15 cm | **拖、删、改名** |
| `labels` | Figure | 不锁、打印 | `label lane n`、`label band <MW>`（并带：`label band 21/20`）、用户自加的任意文字 | **文字与样式**；位置归 agent |
| `profiles` | Profiles | 锁定、打印 | 曲线、坐标、刻度、泳道说明 | 否（每次 sync 重建） |
| `photo` | Crop | 锁定、不打印 | 一张图 `photo`：内嵌 JPEG 预览（长边 ≤ 2000 px，`xf = [s·k,0,0,s·k,X,Y]`，k = 原图px / 预览px） | 否 |
| `crop` | Crop | 不锁、不打印 | 矩形 `crop`（4 锚点闭合路径，`live.shape:"rectangle"`），绿 `(0, 0.75, 0.25)`、0.75 pt、无填充；锚点顺序 = 胶面的 TL TR BR BL（TL–TR = 孔那条边，转多少度都一样） | **挪、转、按 Shift 缩放** |
| `ep-meta` | Workbench 画板外 | 隐藏、锁定、不打印 | 一个文字对象 `ep-meta`：运行元数据 JSON | 否 |

线宽 0.5 pt。`ep-meta` 里存：skill 版本、照片路径、胶体预设、marker 预设、dpi、Profiles 版式、render 时的泳道名、
marker 标签表、**上一次的泳道中心**（给标签匹配用）、上一次的 band 线（给 sync 的改动摘要用）、种子信息、
`label_angle`（新建标签的默认角：`{"lane", "marker"}`）、`lane_angles`（每条泳道的标签角度，按泳道顺序）。
角度 0 不写这两个键（没有旋转的文档与旧版逐字节一致）。还有 `crop`（Crop 画板对应的裁切）：

```json
"crop": {"photo_size": [4032, 3024],                 // 自动定向后的原图像素
         "photo_sha1": "3f2a…",                       // 照片文件 sha1 的前 12 位十六进制
         "frame_px": [[x,y],[x,y],[x,y],[x,y]],       // TL TR BR BL，原图像素（自动定向后）
         "stage1": {"cue": "dark", "rect": null, "angle": null, "no_rotate": false,
                    "longest": 700, "ink_thr": null, "gel_lanes": null,
                    "trim_top_cm": 0.0, "trim_bottom_cm": 0.0}}  // render 时的参数，只作记录
```

不放进自定义 JSON 键（存盘就丢）也不放 `file.info`（会写进导出的 PNG），见 vectorcraft.md §4。

### 所有权（sync 的硬规则）

| 对象 | agent 写 | agent 从不改 |
|---|---|---|
| `label lane n` | 按**墨迹框**落位：框的底边离胶顶 4 pt、框的中线对准泳道中心（角度 90° 时字就竖在泳道上方，框宽 = 大写字高）；新泳道新建（文字 `?`，角度取 `lane_angles`/`label_angle`）；泳道没了就删；泳道增删时按**从左往右的序号**搬名字（第 i 格 ↔ 第 i 格，不看位置） | 文字、字体、字号、颜色、对齐、**角度**（sync 读到什么就用什么，从不改写） |
| `label band <MW>` | 框的竖直中心对准它的 band 线（挤的时候上下让开），内侧竖边离胶 4 pt；band 线删了就删 | 文字、字体、字号、颜色、**角度** |
| `labels` 里其它名字的文字 | — | 一切 |
| `gel seg k`、Figure / Profiles 画板大小 | 一切（由泳道线 + 保留泳道算） | — |
| `analysis`、`profiles` 层 | 一切 | — |
| Workbench 的线 | 只改颜色：`trimmed` 里的变灰、`lanes` 里的变青 | 位置、名字、条数 |
| `crop` 框 | 旧文档补画板时新建 | 位置、角度、大小（归用户） |
| `photo` 预览、`gel` 图的像素 | 框变了就重裁、原地换像素（`xf`、`id` 不变） | — |
| `ep-meta` | 每次改写 | — |

推论：用户拖了标签的位置，下一次 sync 会拉回锚点；**删掉** `label lane n` 对象，下一次 sync 会用旧名字重建它 ——
要让某道不显示名字，把它的文字清空，别删对象。

## 5. Workbench 的读法与校验

`sync` 读活文档（`document.json`，不需要先存盘），按**图层**绑定，不靠对象名（用户新画的线没名字也算）：

- `lanes` 层的竖线按中点 x 排序，**相邻两条之间就是一条泳道**（相邻泳道共用中间那条线），从左到右编号 1…n；
  要劈开一条泳道就在中间加一条线，要合并两条就删掉中间那条。
- `trimmed` 层里每条线的中点 x 落在哪条泳道里，那条泳道就不进 Figure；同一道放几条都算一次。删掉标记 = 恢复。
- `window` 层恰好两条横线 → 上 = `top`、下 = `bottom`。超出胶面只提示、按胶面裁。
- `marker bands` 层的横线名字必须是 `band <MW>`（数字可带小数）。
- marker 泳道 = **[L, R] 包含全部 band 线中点**的那一条（不存序号，增删泳道后照样找得到）。
- 线不必横平竖直：竖线取两端 x 的中点，横线取两端 y 的中点。
- 位置换算靠 Workbench 那张 `gel` 图的 `xf`；它被非等比缩放过 / 找不到 → 只能重新 render（`gel` 层本来是锁的）。

读不通时**什么都不改**，一次列出全部要改的地方（`Workbench 有 N 处要改（什么都没动）`）：

| 报错（大意） | 意思 / 怎么改 |
|---|---|
| `lanes` 图层里只有 0 / 1 条竖线 | 至少两条才夹得出一条泳道 |
| `lanes` 图层里有横线 —— 泳道分隔线应是竖线 | 横线画错层了 |
| `trimmed` 图层里有竖线 | 按旧习惯把泳道线挪进了 `trimmed`；挪回 `lanes`，改在 `trimmed` 里画一条短横线落在那条泳道里 |
| `trimmed` 图层里的线中点不在任何泳道里 | trim 标记落在最外两条分隔线之外（或正好压在分隔线上）；拖进要拿掉的那条泳道 |
| 泳道 i 的两条边线几乎重合 / 整个在胶面外 | 两条分隔线叠在一起（多复制了一条）；删掉多的那条 |
| window 应当恰好有两条横线 / top 与 bottom 几乎重合（<0.05 cm）/ top 在 bottom 下面 | 积分窗被删了一条、多了一条或拖反了 |
| 一条线名叫 'xxx' —— 改名为「band <分子量>」 | band 线名不是 `band 35` 这种格式（含复制出来的无名线） |
| marker bands 图层里有竖线 | 竖线画错层 |
| 条带线的中点不在任何泳道里 | band 线被拖到了最外两条分隔线之外 |
| marker 条带线横跨了泳道 … | band 线分散在几条泳道里，应全在一条里 |
| 只剩 k 条，标定至少要 3 条 | 删多了 |
| band A 在 band B 下面，但分子量不更小 | 名字或位置弄反了（MW 必须随 y 向下单调减小） |
| 所有泳道都在 trimmed 里 | Figure 至少要留一条 |

只提示、不报错：图层里有非直线对象（忽略）；`ep-meta` 缺失或不是 JSON（标签匹配退回按顺序）。

**Crop 画板**（`crop.read_frame`）的报错并进同一张单子（表头不变，条目以 `Crop：` 开头），同样什么都不改。
框的四角取路径锚点，经 `photo` 图的 `xf` 换回预览像素，再按 `photo_size` 换回原图像素：

| 报错（大意） | 意思 / 怎么改 |
|---|---|
| `crop` 图层应当恰好一个 4 角闭合形状（找到 N 个） | 删掉多的 / 撤销删除 |
| 框不是矩形（某角偏 >0.5°，或对边差 >0.5%） | 撤销变形；只能挪、转、缩放 |
| 框的长宽比 `h/w` 与预设差 >1%（`… vs …`） | 按住 Shift 等比缩放 |
| `photo` 图不见了，或被非等比缩放 | 只能重新 render |

框有一部分出了照片**不算错**：`crop_face` 和 render 一样夹到画布里，打同样的 `[!] clipped` 警告
（所以 render 时探测框本来就贴边的文档，sync 照样是 0 个改动）。

## 6. 泳道种子：按加样孔定相的等距梳子（`well_grid.py`，V8）

为什么不用条带/墨量猜泳道：样品道常常是空的或极淡（DGC 照片上样品带只深 3–4 灰度级，梯子约 24），
老 stage 2 的列墨量拟合没东西可锁，在 IMG_1496 上整体偏了一道（梯子落进泳道 2）。加样孔则**每道都有**、不管上没上样。

- 胶面最上面 **0.12 cm** 一条里，每列的「深度」= 顶条中位灰度 − 该列最暗值（孔底是一道细的深色 U）。
- 梳子 `centre_k = c0 + k·pitch`（k = 0…lane_count−1），间距在预设 **±20%** 内以 0.002 cm 步长穷举、相位以 1 px 步长穷举。
  打分 = 各孔窗口（宽 **0.75·pitch**）内平均深度 − 窗口外平均深度（窗外含两侧没有孔的边距，所以整排错一道会被罚）。
- 种子泳道宽 = **0.7·pitch**（用户在 IMG_1499 上手放的宽/距 = 0.71）。
- 最优分（孔深）< **20 灰度级** = 看不见孔 → 退回「预设间距、整排居中」的等距梳子，日志写 `看不清孔，预设间距居中`。
- 实测（5 张 DGC 照片 IMG_1496–1500）：间距 0.510–0.522 cm，孔深 41–87 灰度级（无孔处 0.9–9.9）；
  与用户在 IMG_1499 上手放的 15 道相比，中心平均差 **0.015 cm**、最大 **0.042 cm**（手放本身抖 ±0.03 cm）。
- render 打印 `[i] 泳道种子：按加样孔定相，15 道，孔距 0.516 cm，首道中心 … cm（孔深 … 灰度级）`，并拿孔距与预设核对。

**marker 线**：在 marker 泳道的剖面上叫带（§7），叫出来的峰 y 就是 `band <MW>` 线的位置，按 `labels`（含 `rows`）从上到下配 MW。
老 stage 3 的行在 5 张照片上一律偏下 0.13–0.5 mm，已不用。峰数 ≠ 标签需要的行数 → 换下一条候选 marker 泳道；
全部失败 → 拒绝，消息里列出叫出的 y（cm），例如 `marker 泳道 1 的剖面上叫出 10 条带（y = [...] cm），标签需要 11 行`。

**默认积分窗**：top = **0.15 cm**（让开孔底 U 形，否则每条剖面开头一根大尖峰），bottom = 胶面高 × **0.97**（让开胶底渐变）。

## 7. 剖面、条带、标定（`profiles.py`；render 与 sync 同一份代码）

1. **剖面**：积分窗内每一像素行，取 x ∈ [L, R] 的平均灰度（像素中心落在区间里的列）→ `gray`。
   基线 = 半窗 **0.30 cm** 的滑动均值（top-hat），`ink = baseline − gray`（越暗越大）。参数一律用 cm，与分辨率无关。
2. **叫带**：在**轻平滑（半窗 0.03 cm）后的原始灰度**上找局部变暗。
   - **局部显著度** = 比两侧 **0.15 cm** 内各自最浅点中**较深**的那个还深多少。
   - 门槛 = **4 × 该道背景噪声**；噪声 = 平滑 ink 的 MAD × 1.4826，**迭代剔除**偏离中位数 3σ 的行（条带本身和它两旁的负瓣）最多 5 轮，下限 0.15 灰度级。
   - **没有绝对墨量下限**（老 `min_loose_ink=6` 会把只深 3–4 级的淡道整条丢掉）。
   - 两峰最小间隔 0.05 cm（并带 21/20 kD 约隔 0.12 cm）；剖面两端各留一段不取峰（基线在端点被截断）。
   - 每条带报 `y_cm`、`prominence`、`snr`、`width_cm`（半高宽）、`strongest`（该道显著度最大的那条）。
   - 为什么不在 `ink` 上叫：滑动均值基线在浓带处下陷，浓带两旁成负瓣、两条浓带之间的平背景反成「鼓包」（合成 4 带梯子在 ink 上叫出 7 条，在原始灰度上正好 4 条）；
     浓带旁的淡带在 ink 上甚至是负的。闭运算基线试过也否了（平坦道上叫出假带）。
   - 为什么噪声要迭代剔除：相邻行差分法几乎处处掉到下限，把 1–2 级的纹理全当带；一次性 MAD 在密梯子上被条带撑大，11 条只叫出 8 条。
   - 实测：IMG_1496 梯子 11/11，淡样品带（泳道 5/6/13/14，SNR 5–11）叫得出，空道基本叫不出东西。
3. **标定（只有一种做法：两点间插值）**：`band <MW>` 线的 y ↔ MW。相邻两条 marker 带夹住的区间内，按 `log10(MW)`
   在**这两点之间**线性插值 —— marker 点上的值严格等于标签值，两点之间按这条胶自己的局部斜率走。**禁止全局拟合**：
   `log10(MW) = a + b·y` 最小二乘在 Tris-Tricine 胶上不成立（残差是系统的：250 kD 被压低约四成、10 kD 抬高约三成，
   R² 只有 0.95），而且这条直线在标记点之间也不经过真实值。标定里不存拟合系数、也不报 R²。
4. **MW**：每条叫出的带按标定换算；超出 marker 范围的记 `mw_kda: null` + `extrapolated: true`（终端表里写「超出 marker 范围」，不给外推数字）。

终端表：标定一行；每条**有名字、不是 marker、没被 trim** 的泳道一行，`[主]` = 显著度最大的带。
「浓」= 局部显著度（灰度级），不是积分量；预染 marker 的表观分子量与样品有系统偏差，汇报时别说成真实分子量。

### `fig.mw.json` v2

```json
{"version": 2,
 "units": {"x": "cm from gel-face left", "y": "cm from gel-face top",
           "gray": "0-255 (lower = darker)", "ink": "baseline - gray (higher = more stain)"},
 "gel": {"width_cm": 8.6, "height_cm": 6.25, "px_per_cm": 118.11},
 "window": {"top_cm": 0.15, "bottom_cm": 6.06},
 "calibration": {"method": "interp", "marker_lane": 1, "points": [[0.42, 250], ...]},
 "params": {"base_half_cm": 0.3, "smooth_cm": 0.03, "min_sep_cm": 0.05, "k_noise": 4.0},
 "lanes": [{"lane": 1, "label": "M", "marker": true, "trimmed": false,
            "left_cm": 0.31, "right_cm": 0.67, "noise": 0.8,
            "profile": {"y_cm": [...], "gray": [...], "baseline": [...], "ink": [...]},
            "bands": [{"y_cm": 2.1, "prominence": 4.1, "snr": 5.2, "width_cm": 0.06,
                       "mw_kda": 23.5, "extrapolated": false, "strongest": true}]}],
 "notes": ["泳道 16 还没有名字"]}
```

- 坐标一律是胶面 cm（原点 = 胶面左上角），**没有照片像素**。剖面每个胶面图像素行一个样本（300 dpi 下 15 道约 300 KB）。
- `label` = 文档里当前的标签文字（sync 后就是用户改过的名字）；`null` = 还没名字（`?`）；`""` = 空白道。
- trimmed 泳道照样有剖面和条带（`"trimmed": true`）。`--no-mw` 不写这个文件。

## 8. Figure 与 Profiles 版式（`figure.py`）

**Figure**：

- **保留段**：没 trim 的泳道里，连续的成一段；段边界 = 与相邻（被 trim 的）泳道中心的**中点**，两端到胶边。
  段与段之间留 **0.15 cm 白缝**（拼缝披露；没有额外的线）。不拉伸、不上下位移、不调色。
- **泳道名**：`label lane n` 用**原泳道号**（留下的第 8 道还是 `label lane 8`），Arial 8 pt；
  **按墨迹框摆**：框的底边在胶顶上方 4 pt、框的中线对准泳道中心。角度 0° 时就是「水平居中、基线离胶顶 4 pt」；
  角度 90° 时框宽 = 大写字高，字从胶顶上方 4 pt 起往上竖排。
- **marker 标签**：Arial 8 pt；**按墨迹框摆**：框的竖直中心对准 band 线，框的内侧竖边离胶边 4 pt（marker 泳道中心在
  Figure 胶面左半 → 标签在胶左侧、右对齐，否则在右侧、左对齐）。角度 0° 时与旧版逐点一致。
  挨得近的（250/150/100、40/35）用一维最小位移排字**上下让开、保持顺序**，不叠字（让开量按框高算，竖排时框高 = 字长）。
  marker 泳道被 trim 时不画 marker 标签（只标定）。
- **角度**：度，逆时针；90 = 从下往上读，−90 = 从上往下读（与 matplotlib 的 `rotation` 同向）。角度写在点文字的
  `xf` 矩阵里（绕锚点转，见 vectorcraft.md §1），框用同一个矩阵算，所以任意角度都能正确落位、量页边。
- **页面规则**：每边留白 = `max(0.43 cm, 文字框越界 + 0.17 cm)`，按**当前**标签的文字、字号与角度量
  （名字改长或转成竖排，页就跟着变宽/变高）。

**Profiles**（`render --profiles stack|strips`，默认 `stack`；版式记在 `ep-meta`，sync 沿用）：

- `stack`：每道一行横向曲线，横轴 = 积分窗内的 y，上方标 MW 刻度；每行右侧注满幅灰度级。
- `strips`：每道一条竖向曲线，纵轴与胶同比例（1 cm = 1 cm），按泳道顺序并排，左边一列 MW 刻度。
- 曲线画 `ink`；满幅 = max(曲线最高点, 本胶最强样品道的 1/4, 10 × 该道噪声)（空道不会被放大成像有带）；marker 道按自己的最高点画。
  叫出的带有标记，marker 道带标定点。

## 9. `sync`

```bash
python3 scripts/gel_photo.py sync fig.vectorcraft [--port 7797] [--dry-run] [--dpi 300]
```

### 9a. 做什么

1. 连上控制口上的 VectorCraft（没有就带 `--control` 启动），文档没开就打开，激活它。
2. 读 `document.json` → Workbench 模型（§5）和 Crop 框（`crop.read_frame`）。读不通 → 打印全部要改的地方，**什么都不动**。
3. **框变了**（`frame_delta` 不是 `None`：有角挪了 >0.5 原图像素）：`ep-meta.photo` 必须还在、`photo_sha1` 对得上，
   否则报 `照片 <path> 找不到 / 换过了 —— 把原照片放回去，或重新 render`（什么都不动）。用 `crop_face` 按 meta 的 dpi
   从原图重裁 → 后面分析的就是这张新胶面图（像素尺寸不变：width_cm、height_cm、dpi 都没变）。
4. 在胶面图上重算剖面、条带、标定（§7）→ `plan_sync` 算出最少的操作。重裁过就在最前面加一个 `relink` 操作
   （Workbench 的 `gel` 图 + 每个 Figure `gel seg k` 里的图）：写临时 PNG → `links.relink {ids, path, preserve:"transforms"}`
   → `links.embed {ids}`，临时文件在撤销组结束后删掉；`ep-meta.crop.frame_px` 改成新框。
5. 全部操作在**一个**撤销组 `ep-labeling sync` 里执行（⌘Z 一次撤回整个 sync，含重裁；中途出错整组回滚）。
6. 存盘（轮询到文件比请求新且能解析），按 `ep-meta` 里的 dpi 重新导出 `fig.png`，重写 `fig.mw.json`（泳道名取当前标签文字）。
7. 打印改动摘要，例如 `泳道移动：3 +0.15 cm`、`trimmed（不进 Figure）：[14]`、`marker 线：band 35 删除`、
   `裁切框：转 +0.40°，移 +0.05/−0.10 cm，宽 ×1.020（重新裁了胶面图）`（cm = 旧比例下的胶面 cm；框没动就没有这行），
   以及 `已更新：artboard 1, delete …, create …, move …`；有 `?` 泳道时打 `[?] 泳道 [n] 还没有名字`。

`--dry-run` 只打印摘要和操作列表，不动文档。`--dpi` 只在 `ep-meta` 没记 dpi 时用。

### 9b. 会变什么、永远不变什么

- 会变：泳道标签的位置（跟泳道中心）、marker 标签的位置（跟 band 线）、新泳道的 `?` 标签、消失的泳道/带的标签被删、
  Figure 的胶段与拼缝、Figure 与 Profiles 画板大小、`analysis` 与 `profiles` 层、trimmed 线的颜色、`ep-meta`、`fig.png`、`fig.mw.json`。
- 永远不变：标签的文字、字体、字号、颜色、**角度**（用户在 app 里转过的，sync 读到后照它重新落位，只把角度记进 `ep-meta`）；
  `labels` 层里用户自加的文字；Workbench 线的位置与名字（重裁只换线下面的像素，胶面 cm 尺寸不变，线不会掉出胶面）；
  `gel` 图的位置与 `xf`（框没动时像素也不动）；`crop` 框。
- **幂等**：Workbench 与框都没改、标签已在位时，再跑一次是 0 个操作（`文档已经是最新的（0 个改动）`），只重新存盘和导出。
  重裁过的 sync 之后再跑一次也是 0 个（框 = meta 里的 `frame_px`）。
- **旧文档（没有 Crop 画板）第一次 sync**：`ep-meta.photo` 不在了 → `[i] 旧文档没有 Crop 画板（照片 <path> 不在了）；要调裁切就重新 render`，
  其余照常。照片在 → 用默认参数重跑 stage 1、框挪 `crop.legacy_page_shift`（旧文档的图是修错位之前裁的，§3）、按 meta dpi 裁出来，与内嵌胶面图比（像素尺寸相同且平均 |Δ灰度| ≤ 2.0 级）：
  对不上 → 同一条提示加 `（当时的裁切参数没记下来）`，照常；对上 → 同一个撤销组里补上 Crop 画板、`photo` / `crop` 图层、预览与框，
  写 `ep-meta.crop`，摘要 `已补上 Crop 画板`。之后再 sync 是 0 个操作。
  sync 没有 `--gel-size`：重跑用的预设由胶面 cm（取到 0.01）拼出来，trim 为 0、anchor = bottom。所以当时用了 trim、
  `anchor top/center` 或 `--rect` / `--angle` / `--cue` 的旧文档一般对不上，只会得到那条提示（宁可不补，也不补一个错的框）。
- **旧文档第一次 sync（V9 起）**：页顶留白改按**大写字高**算（旧版按字号），整张 Figure 会上移约 2.3 pt（0.08 cm）、
  页高相应变短；标签相对胶面的位置不变。一次之后稳定，再 sync 还是 0 个操作。
- **泳道增删**：名字**按序号搬** —— 第 i 格（从左往右数）永远用上一次第 i 格的名字，不看中心位置、不做容差配对。
  所以拖线、调宽窄、把最外侧线拖到胶边都不改名；删掉一条分隔线时它右边的名字整体前移一格，多出来的最后一格是 `?`。
  （旧版按「半个孔距容差就近配」，拖边这种半个孔距的常见编辑会被判成新泳道 —— Issue #1，已弃用。）
- **band 线**：删掉 = 去掉这个标定点、它的 Figure 标签也删；新增一条 `band <MW>` = 多一个标定点（但没有对应的 Figure 标签）；
  改名 = 改标定值：旧 MW 的 Figure 标签随之删掉，新值自动得到一条默认标签 `"<MW> kD"`（名字 `label band <MW>`，文字可以再改）；新画的 `band <MW>` 线同样。
- 实测（V7，IMG_1496）：拖泳道 3、把泳道 14 挪进 `trimmed`、删 `band 35`、泳道 2 改名 WT 并染红 → 一次 sync 约 9 s、一个撤销步；
  WT 保持文字和红色，标签 3 重新居中，泳道 14 和 35 kD 的标签被删，Figure 拆成两段；再跑一次 0 个改动。

### 9c. 只改文字：经控制口改标签（然后 sync）

改泳道名之类的请求**不需要重跑 render**。最省事是请用户在 Figure 上直接改字；要 agent 来改，就用控制口的 `text.setText`
（对象按**完整名字**找；`document.find` 是子串匹配，`label lane 1` 会命中 `label lane 10`–`15`，别用）：

```bash
python3 - <skill>/scripts fig.vectorcraft "label lane 2" "WT" <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
import vc_control, vc_file
doc, name, text = os.path.abspath(sys.argv[2]), sys.argv[3], sys.argv[4]
c, _ = vc_control.connect(7797, launch=False)
docs = c.call("ui.inspect").get("documents") or []
c.execute("document.activate", {"index": next(i for i, d in enumerate(docs) if d.get("title") == os.path.basename(doc))})
lab = vc_file.layer_by_name(c.document_json(), "labels")
node = next((n for n in vc_file.lay_children(lab) if n.get("name") == name), None)
if node:
    c.execute("text.setText", {"id": node["id"], "text": text})
else:   # 空白道（lanes.json 里是 ""）没有标签对象：在 labels 层新建一个并命名，sync 会把它放到位
    c.execute("layer.setCurrent", {"id": lab["id"]})
    c.execute("select.set", {"ids": []})
    r = c.execute("text.create", {"x": 0, "y": 0, "text": text, "size": 8, "font": "Arial", "justify": "center"})
    c.execute("object.setProps", {"id": r["id"], "name": name})
EOF
python3 <skill>/scripts/gel_photo.py sync fig.vectorcraft
```

`text.setText` 保留第一段文字的样式（字体、字号、颜色不变）。要改样式也请用户在 app 里改，sync 不会覆盖。

**旋转**一条标签同样不用重跑 render：控制口 `object.rotate {ids, angle, origin}`（`angle` 逆时针、**相对**转；`origin` 给锚点，
点文字的锚点就是 `xf[4..5]`）。转完 `sync`：读到新角度 → 按墨迹框重新落位、只 `move`，角度记进 `ep-meta`。

```bash
python3 - <skill>/scripts fig.vectorcraft "label lane 3" 90 <<'EOF'
import os, sys
sys.path.insert(0, sys.argv[1])
import vc_control, vc_file
c, _ = vc_control.connect(7797, launch=False)
docs = c.call("ui.inspect").get("documents") or []
c.execute("document.activate", {"index": next(i for i, d in enumerate(docs)
                                             if d.get("title") == os.path.basename(sys.argv[2]))})
n = next(x for x in vc_file.lay_children(vc_file.layer_by_name(c.document_json(), "labels"))
         if x.get("name") == sys.argv[3])
c.execute("object.rotate", {"ids": [n["id"]], "angle": float(sys.argv[4]),
                            "origin": [n["kind"]["xf"][4], n["kind"]["xf"][5]]})
EOF
python3 <skill>/scripts/gel_photo.py sync fig.vectorcraft
```

别用 `absolute:true` 去「设成 0°」：0.14 实测 `object.rotate {angle: 0, absolute: true}` 对已经是 90° 的文字是空操作；
要把转过的标签转回水平，就相对转 −90。

## 10. 命令

```bash
cd <本 skill 目录>
python3 scripts/gel_photo.py doctor          # python / magick / 字体 / VectorCraft 版本与控制口 / 数据文件
python3 scripts/gel_photo.py selftest        # 不读用户文件、不开 app；全绿即可（项数以当次输出为准）

# 只探测（不写文件、不开 app）：胶面、孔距/首道/孔深、每道叫出的带 y
python3 scripts/gel_photo.py probe photo.jpeg --gel-size data/gel_size.json [--gel-lanes N] [--rect x0,y0,x1,y1]

# 出文档 + fig.png + fig.mw.json
python3 scripts/gel_photo.py render photo.jpeg --out fig.vectorcraft \
        --markers data/markers.json --preset 3 --lanes lanes.json \
        --gel-size data/gel_size.json [--trim-lanes 1,2,14-15] [--dpi 300] [--profiles stack|strips] \
        [--label-angle 90] [--marker-label-angle 0] [--port 7797] [--no-mw]

# 用户在 VectorCraft 里改完之后
python3 scripts/gel_photo.py sync fig.vectorcraft [--dry-run]
```

render 的其它开关（全是 stage 1；出图后也可以在 Crop 画板上调框再 sync）：`--gel-cm`、`--gel-lanes`、`--trim-top-cm` / `--trim-bottom-cm`、`--angle` / `--no-rotate`、
`--longest`（分析缩略图长边，默认 700）、`--cue dark|chroma|union`、`--ink-thr`、`--rect`。
标签角度：`--label-angle`（泳道名默认值）、`--marker-label-angle`（marker 标签默认值），或 `lanes.json` / `markers.json`
里每项的 `angle`（度，逆时针；90 = 从下往上读）。sync 不需要角度参数：它从文档读。
`--out` 给 `.png` 也行（取同名 stem）。已有的 `fig.vectorcraft` 先备份成 `fig.vectorcraft.bak`；
它在 VectorCraft 里开着且有没存的改动时 render 拒绝（免得覆盖用户的修改）。

## 11. 已移除 → 用什么代替

命令行上的旧子命令/开关都会报 `… 已经去掉了：…` 并给出替代，不会被静默忽略；配置文件里的旧键（`y_px` 等，§1）则只是不再读取。

| 已移除 | 代替 |
|---|---|
| `export` 子命令、`fig.html` 编辑器、`fig.scene.json`、`fig.gel.png` | VectorCraft 里改；`sync` 重新导出 `fig.png` |
| `--svg` | `fig.vectorcraft` 本身是矢量文档；要 SVG 在 VectorCraft 里 File → Export |
| `--no-editor` | 不再写 HTML 编辑器 |
| `--debug` 覆盖图 | Workbench 画板（`lanes` / `window` / `marker bands` / `analysis` 图层） |
| `--record` JSON | Workbench 上的线 + `ep-meta`；数值在 `fig.mw.json` |
| `--keep-lanes` | `--trim-lanes`（写要拿掉的），或在 `trimmed` 里给那条泳道画一条 trim 标记 |
| `--marker-policy` | 固定行为：marker 泳道被 trim 时只标定、不画标签 |
| `--pair-force`、`y_px` 手动标定 | 在 Workbench 上拖 / 删 / 改名 `band <MW>` 线，然后 sync |
| `--mw-method interp` | 没有可选方法：MW 一律在相邻两条 marker 带之间插值，全局拟合被禁止 |
| `--margin-frac`、`--splice-gap-cm` | 页面规则自动推导；拼缝固定 0.15 cm |
| `--font`、`--outline` / `--no-outline` | 字体与样式在 VectorCraft 里改（默认 Arial） |

## 12. 脚本模块

`scripts/gel_photo.py` 只放 CLI 与编排（`probe` / `render` / `sync`）；其余同目录、纯 stdlib：

| 模块 | 承担 |
|---|---|
| `magick_io.py` | magick 外呼、PPM 读取 |
| `gel_detect.py` | 认胶面：背景平面、暗度/蓝度判据、连通域、竖边倾角 |
| `gel_area.py` | stage 1：按预设定标定形、高度核对 |
| `geometry.py` | 旋转几何 |
| `crop.py` | Crop 画板：stage 1 框 ↔ 四角、预览 JPEG 与版式、读回框（`read_frame`）、`frame_delta`、按框重裁（`crop_face`） |
| `config.py` | markers / lanes / 胶体预设的读取与校验 |
| `lane_select.py` | `--trim-lanes` 语法、保留段与拼缝版式 |
| `text_layout.py` | 字体、文字量宽、标签的角度与墨迹框（`box_offsets` / `anchor_for`，页边距与落位都用它） |
| `well_grid.py` | 泳道种子：按加样孔定相的等距梳子（§6） |
| `profiles.py` | 剖面、叫带、标定、报告 v2（§7） |
| `mw_report.py` | MW 数学、marker 配对、终端表与写盘 |
| `workbench.py` | Workbench 模型：建（`from_image`）、画、读回（`from_document`）、校验、泳道名匹配 |
| `figure.py` | Figure / Profiles 画板、整份文档（`build_document`）、sync 增量计划（`plan_sync`） |
| `vc_file.py` | `.vectorcraft` 读写（内联 / blobs / gzip 三种编码；对象从 `templates/kinds.vectorcraft` 深拷贝） |
| `vc_control.py` | 控制口客户端：连接/启动、对话框、存盘轮询、导出核对、撤销组 |
| `sync_exec.py` | 把 `plan_sync` 的操作落到活文档上 |
| `env_check.py` | `doctor` |
| `selftest.py`、`selftest_sync.py`、`selftest_crop.py` | `selftest`（`selftest_sync` 用假控制口测 sync 执行器；`selftest_crop` 测裁切框几何与读回） |
