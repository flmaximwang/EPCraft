# 照片标注：配置、文档结构、分析、sync

权威设计在 `specs/ep-craft-vectorcraft.md`；VectorCraft 文件格式与控制口命令（实测）在 `references/vectorcraft.md`。
本文写的是**现在代码实际怎么做**，数字来自 `tickets/ep-craft-vectorcraft.md` 各张票的收尾记录。

## 1. 两个 JSON —— 字段表

`src/ep_craft/templates/` 里有整套示例。下面没列的键（`size` / `color` / `gap` / `font` / `align`、`y_px` / `snap` / `y_frac` /
`x_frac` / `marker_lane_x` / `lane_width` / `lane_edge`）**已不再读取**：字号、字体、颜色在 VectorCraft 里改；
条带与泳道位置在 Workbench 上拖线改（§5、§9）。

### markers.json（或 `src/ep_craft/data/markers.json` 预设库 + `--preset`）

| 键 | 含义 |
|---|---|
| `labels` | **从上到下**，每项一条标签：字符串，或 `{"text", "rows", "color", "angle"}`。`text` 里必须有分子量数字（`250 kD`、`21/20 kD`），它变成 Workbench 上的 `band <MW>` 线名。`rows: 2` = 这条标签盖两条带（两条 band 线、一个标签，见 special-cases §1）。`color` = 这条 marker 标签在 Figure 上的初始颜色（之后归用户）。`angle` = 这条标签的角度（度，逆时针，见 §8；省略用 `--marker-label-angle`） |
| `marker_lane` | 可选：梯子所在泳道（1 起）。追加到 `lanes.json` `marker_lanes` 候选的**末尾** |
| `_preset` / `_source` / `_说明` | 预设库里用来挑预设（`--preset` 的关键词在 `_preset` 里匹配，须恰好命中 1 个）与留出处 |

条带数之和（`rows` 求和）= **MW 槽位数 N**：标签按从上到下的顺序展开成 MW 序列（每条标签占 `max(1, rows)` 个槽，
`250 kD` / `21/20 kD` 这类文字里的分子量数字按序填槽；`rows: 2` 但只写了一个数字时尾部那个槽不出线，记一条提示）。
render 在梯子道的剖面上取显著度**前 N 名**的局部极小（梯子道不看噪声门槛 —— 那里的噪声量的是条带自己，见 §7），
**第 k 条检出带直接拿第 k 个 MW 挂上去，落在检出位置上** —— 检出即标定，没有配对步骤，也不要求条数正好等于 N：
叫出 k 条就放 k 条线，k < N 时只挂前 k 个 MW，并把这条记进置信信息（§7「marker 道」、§10a）。
取出来的 N 名还要过两道置信闸门（断崖、下限，§7）：一条道过了就赢；全不过 → 用断崖比值最好的那条道，
照样出文档和 MW，只把整体标成**低置信**（§10a），消息里列出取出的 y 与没过的那道闸门。

### lanes.json

| 键 | 含义 |
|---|---|
| `labels` | **从左到右**，每条泳道一项：**字符串**，或 `{"text", "angle"}`（名字成为 Figure 上 `label lane n` 的初始文字；`angle` 只改这一条的角度）。`""` = 空白泳道：不画标签、MW 终端表不列。没列到的泳道（`labels` 比泳道少）也当 `""` |
| `marker_lanes` | **候选梯子泳道**（1 起，按优先顺序，如 `[1, 2, 3]`）。render 依次试：第一条的剖面过了置信闸门就赢（闸门不过，例如胶边损坏、道里只有纹理，就换下一条）；全不过则取断崖比值最好的那条道，照常出结果、整体标低置信（§10a）。**标定只用那一条**（不是多管加权）；之后 marker 泳道由 band 线落在哪条泳道里决定（§5） |

## 2. 胶体预设与成品尺寸

`src/ep_craft/data/gel_size.json` **必给**、**没有默认**。没有 `--gel-size` 时 `probe` / `render` 一律以
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
| `analysis` | Workbench | 锁定、不打印 | 泳道号 `tag n`（灰） | 否（每次 sync 重建） |
| `sample bands` | Workbench | 不锁、不打印 | 样品带的短刻度 `peak n.k`（绿，k = 道内自上而下的序号）；中点落在哪道就是哪道，名字不参与判断，用户画的线叫什么都可以 | **拖、加、删**（= 该道的 override，见 spec §7.5） |
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
`marker_confidence`（自动检出 marker 的置信信息，完整字段见 §10a）、"marker_lane"（最近一次被判定为 marker 的那条泳道号，`band` 线一条不剩时 sync 靠它来回读 `marker_lane`）、
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
| marker 条带线只剩 k 条（k = 0/1/2） | **不报错**：照常 sync，标定点如实记 0 条=空、1 条=单点、2 条=两点。0 / 1 条时样品 MW 一律估计不出（`mw_kda: null`，注「标定点不足 2 条…样品分子量无法估计」）；2 条时照常在这两点之间插值。文档 / Figure / band 表照出（低置信标记保持原样，sync 不新增也不清除，§10a）。若要补，把漏掉的 `band <MW>` 线画回去（命名带分子量） |
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

**marker 线**：在 marker 泳道的剖面上取**显著度前 N 名**的局部极小（N = 标签展开后的 MW 槽位数，spec ep-9ji），
它们的 y 就是 `band <MW>` 线，**第 k 条带按序拿第 k 个 MW** —— 检出即标定，不配对数。老 stage 3 的行在 5 张照片上一律偏下 0.13–0.5 mm，已不用。
**不用噪声门槛**：梯子道的噪声量的是条带自己（IMG_1586 第 3 道 σ = 17.2、门槛 4σ = 68.9，11 条带只过 1 条；
其余道 σ = 0.4–2.2），而按显著度排的前 N 名在这张胶上恰好就是 11 条带（第 11 名 7.4，第 12 名 1.01，断崖 7.3×）。
取到的 N 名要过两道**置信**闸门（§7）：第 N 名 ≥ 2 × 第 N+1 名、第 N 名 ≥ 1.5 灰度级。过闸门的第一条候选道赢；
全不过 → 取断崖比值最好的那条、整体标低置信（§10a），消息例如 `marker 泳道 1 未过标准：第 11 名显著度 0.26 < 下限 1.50 灰度级`，
并列出取出的 y。叫出的条数少于 N 也只是低置信，不是错误（`picked < need`）。
`probe --markers ...` 会把每条候选道的前 N 名与闸门打出来，低置信时想知道为什么可以跑它。

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
   - **以上是样品道**。marker 道不走这个门槛，见下面的「marker 道」一条。
   - 为什么不在 `ink` 上叫：滑动均值基线在浓带处下陷，浓带两旁成负瓣、两条浓带之间的平背景反成「鼓包」（合成 4 带梯子在 ink 上叫出 7 条，在原始灰度上正好 4 条）；
     浓带旁的淡带在 ink 上甚至是负的。闭运算基线试过也否了（平坦道上叫出假带）。
   - 为什么噪声要迭代剔除：相邻行差分法几乎处处掉到下限，把 1–2 级的纹理全当带；一次性 MAD 在密梯子上被条带撑大，11 条只叫出 8 条。
   - 实测：IMG_1496 梯子 11/11，淡样品带（泳道 5/6/13/14，SNR 5–11）叫得出，空道基本叫不出东西。
   - **marker 道（`profiles.pick_marker`，spec ep-9ji）**：带数由标签给（N = 标签展开后的 MW 槽位数），取显著度**前 N 名**，
     不用噪声门槛 —— 梯子道上噪声量的是条带本身（IMG_1586 第 3 道 σ 17.2、其余道 0.4–2.2；按 4σ 只会叫出 1 条，
     2σ 叫出 8 条、1σ 10 条，20 kD 显著度只有 7.4 永远过不了）。两条**置信**闸门代替它：第 N 名 ≥ **2 ×** 第 N+1 名
     （真梯子：IMG_1586 修好裁切框后 2.7×、旧裁剪 7.3×、合成 IMG_1496 3.8×；非梯子道 ≤ 1.7×）、
     第 N 名 ≥ **1.5 灰度级**（空道 0.14–0.54）。
     闸门只决定**置信度**：过了就用这条道；全不过就用断崖比值最好的那条道，照样出线、出 MW，并把整体标成
     **低置信**（`marker_confidence.ok = false`，`fig.mw.json` 的 `calibration.low_confidence: true`，§10a）。
     检出的第 k 条带（按 y 升序）按序拿第 k 个 MW，**落线即完成**；k < N 时只挂前 k 个（`picked < need`，照样低置信）。
3. **标定（只有一种做法：两点间插值）**：`band <MW>` 线的 y ↔ MW。相邻两条 marker 带夹住的区间内，按 `log10(MW)`
   在**这两点之间**线性插值 —— marker 点上的值严格等于标签值，两点之间按这条胶自己的局部斜率走。**禁止全局拟合**：
   `log10(MW) = a + b·y` 最小二乘在 Tris-Tricine 胶上不成立（残差是系统的：250 kD 被压低约四成、10 kD 抬高约三成，
   R² 只有 0.95），而且这条直线在标记点之间也不经过真实值。标定里不存拟合系数、也不报 R²。
   **标定点少于 2 条也照样出报告**：0 条 → `calibration.points: []`；1 条 → 单点写进 `points`，但**不拿它做任何估计**
   （哪怕某条样品带恰好落在这一点的 y 上也不给 MW —— 一个点定不出斜率）。0 / 1 条时样品带 `mw_kda` 一律 `null`，
   报告 `notes` 写「标定点不足 2 条（k 条 band 线）：样品分子量无法估计」；文档里的低置信标记照旧（sync 不因此新加、
   也不会清掉，§10a）。2 条 → 正常按这两点之间插值（两点之外仍不外推，同下）。
4. **MW**：样品道每条叫出的带按标定换算；超出 marker 范围的记 `mw_kda: null` + `extrapolated: true`（终端表里写「超出 marker 范围」，不给外推数字）；标定点不足 2 条时同样 `mw_kda: null`，终端表里也不给数字，原因看报告注「标定点不足 2 条…」。
5. **marker 道（sync 不叫峰，spec ep-9ji D4）**：文档里 marker 道的带 = `band <MW>` 线本身，剖面只用来
   **吸附** —— 线 ±**0.06 cm** 内有局部极小就吸过去补 `prominence` / `width_cm`，`y_cm` = 极小位置、
   `line_y_cm` = 线、`snapped: true`；吸不上（线在平地上、或两条线抢同一个极小）→ 按线的位置量、`snapped: false`、
   报告 `notes` 里写一句。**线不动**：标定用线的 y，sync 不会为了吸附去挪线。每条带的 lane 上有
   `band_source: "lines"`（样品道是 `"called"`）。marker 道的 `noise` 照旧算、照旧写，但不参与任何判定。
   `snap_lines` 只量测，不参与**置信**判断 —— 低置信标记来自 render 时的自动检出（`ep-meta.marker_confidence`），
   拖线不算「认证」：线是用户（或 render）放的，剖面吸附不产生也不消除低置信标记。

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
 "params": {"base_half_cm": 0.3, "smooth_cm": 0.03, "min_sep_cm": 0.05, "k_noise": 4.0, "snap_cm": 0.06},
 "lanes": [{"lane": 1, "label": "M", "marker": true, "trimmed": false, "band_source": "lines",
            "left_cm": 0.31, "right_cm": 0.67, "noise": 0.8,
            "profile": {"y_cm": [...], "gray": [...], "baseline": [...], "ink": [...]},
            "bands": [{"y_cm": 2.1, "prominence": 4.1, "width_cm": 0.06, "mw_kda": 250.0,
                       "extrapolated": false, "line_y_cm": 2.11, "snapped": true, "strongest": true}]},
           {"lane": 2, "label": "S1", "marker": false, "band_source": "called", ...,
            "bands": [{"y_cm": 2.4, "prominence": 4.1, "snr": 5.2, "width_cm": 0.06,
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
ep-craft figure sync fig.vectorcraft [--port 7797] [--dry-run] [--dpi 300] [--reset-peaks [N ...]]
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
5. 全部操作在**一个**撤销组 `ep-craft sync` 里执行（⌘Z 一次撤回整个 sync，含重裁；中途出错整组回滚）。
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

改泳道名之类的请求**不需要重跑 render**。最省事是请用户在 Figure 上直接改字；要 agent 来改，**用 `edit`**（§9d），
下面的手写脚本只是它做的事的记录。控制口的 `text.setText`
（对象按**完整名字**找；`document.find` 是子串匹配，`label lane 1` 会命中 `label lane 10`–`15`，别用）：

```bash
PYTHONPATH=src python3 - fig.vectorcraft "label lane 2" "WT" <<'EOF'
import os, sys
from ep_craft import vc_control, vc_file
doc, name, text = os.path.abspath(sys.argv[1]), sys.argv[2], sys.argv[3]
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
ep-craft figure sync fig.vectorcraft
```

`text.setText` 保留第一段文字的样式（字体、字号、颜色不变）。要改样式也请用户在 app 里改，sync 不会覆盖。

**旋转**一条标签同样不用重跑 render：控制口 `object.rotate {ids, angle, origin}`（`angle` 逆时针、**相对**转；`origin` 给锚点，
点文字的锚点就是 `xf[4..5]`）。转完 `sync`：读到新角度 → 按墨迹框重新落位、只 `move`，角度记进 `ep-meta`。

```bash
PYTHONPATH=src python3 - fig.vectorcraft "label lane 3" 90 <<'EOF'
import os, sys
from ep_craft import vc_control, vc_file
c, _ = vc_control.connect(7797, launch=False)
docs = c.call("ui.inspect").get("documents") or []
c.execute("document.activate", {"index": next(i for i, d in enumerate(docs)
                                             if d.get("title") == os.path.basename(sys.argv[1]))})
n = next(x for x in vc_file.lay_children(vc_file.layer_by_name(c.document_json(), "labels"))
         if x.get("name") == sys.argv[2])
c.execute("object.rotate", {"ids": [n["id"]], "angle": float(sys.argv[3]),
                            "origin": [n["kind"]["xf"][4], n["kind"]["xf"][5]]})
EOF
ep-craft figure sync fig.vectorcraft
```

别用 `absolute:true` 去「设成 0°」：0.14 实测 `object.rotate {angle: 0, absolute: true}` 对已经是 90° 的文字是空操作；
要把转过的标签转回水平，就相对转 −90。

### 9d. `edit`：agent 替用户改（然后自动 sync）

`ep-craft workbench edit fig.vectorcraft [--keep 2-7 | --trim 1,8-15] [--marker-lane N] [--shift-labels K] [--labels a,b,…] [--label N=TEXT …] [--no-sync]`（`figure edit` 是同一条命令的别名）

`doc_edit.plan_edit` 读活文档（`workbench.from_document`，泳道号 = Workbench 上现在的泳道），生成与 `plan_sync` 同一套 op，
`sync_exec.apply_ops_live(label="ep-craft edit")` 一个撤销步落下，然后照常 `sync`（第二个撤销步）。

| 选项 | 做的事 |
|---|---|
| `--keep` / `--trim` | 清空 `trimmed` 层，每条要拿掉的泳道画一条 trim 短线（位置样式同 render）；集合没变 = 0 个操作 |
| `--marker-lane N` | `marker bands` 层每条线 `reshape` 到泳道 N（两端 = 泳道边 ± 0.15 cm，y = 原中点 y）：标定换到这条梯子 |
| `--shift-labels K` | 现在每道的名字整体右移 K 格（负 = 左移），先于下面两项 |
| `--labels` / `--label N=TEXT` | 有 `label lane n` 对象的 `text.setText`（样式与角度不动）；没有的写进 ep-meta `lane_names`，随后的 sync 按 `lane_angles` 新建 —— 新标签与其它标签同角度 |

标签对象的号是上一次 sync 的泳道号：按 `prev_centres_cm` 用 `match_lanes` 对到现在的泳道（与 plan_sync 一样）；
上次 sync 之后才新加的泳道没有对应 → 报「先 sync 一次」。越界、全部 trim → 报错，什么都不改。

## 10. 命令

公开入口是安装好的 `ep-craft` 控制台命令；不安装时从 repo 根用 `PYTHONPATH=src python3 -m ep_craft …`。没有 `scripts/` 目录，`skills/ep-craft/` 里只有文档。下面命令里的 `src/ep_craft/data/...` 相对 **repository 根**（在 repo 根目录执行）。

```bash
ep-craft doctor                             # python / magick / 字体 / VectorCraft 版本与控制口 / 数据文件
ep-craft selftest                           # 不读用户文件、不开 app；全绿即可（项数以当次输出为准）

# 只探测（不写文件、不开 app）：胶面、孔距/首道/孔深、每道叫出的带 y；给了 --markers 还打 marker 候选道的前 N 名与闸门
ep-craft crop probe photo.jpeg --gel-size src/ep_craft/data/gel_size.json [--gel-lanes N] [--rect x0,y0,x1,y1] \
        [--markers src/ep_craft/data/markers.json --preset 3 --lanes lanes.json]

# 出文档 + fig.png + fig.mw.json
ep-craft figure render photo.jpeg --out fig.vectorcraft \
        --markers src/ep_craft/data/markers.json --preset 3 --lanes lanes.json \
        --gel-size src/ep_craft/data/gel_size.json [--trim-lanes 1,2,14-15] [--dpi 300] [--profiles stack|strips] \
        [--label-angle 90] [--marker-label-angle 0] [--port 7797] [--no-mw]

# 用户在 VectorCraft 里改完之后
ep-craft figure sync fig.vectorcraft [--dry-run]

# agent 替用户改 trim / marker 泳道 / 泳道名（§9d），然后自动 sync
ep-craft workbench edit fig.vectorcraft [--keep 2-7] [--marker-lane 2] [--shift-labels 1] [--label 9=M]

# 控制口 / 只导出 Figure 画板 / Workbench 数据交换（§13）
ep-craft launch [fig.vectorcraft] [--port 7797]
ep-craft figure export fig.vectorcraft [--out fig.png] [--dpi 300] [--background white] [--port 7797]
ep-craft workbench export fig.vectorcraft [--out workbench.json]      # 省略 --out = stdout
ep-craft workbench import fig.vectorcraft --input workbench.json [--no-sync] [--dpi 300]

# densitometry chain (optional, §14): reads fig.mw.json only, writes sibling files, opens no app
ep-craft densitometry measure fig.mw.json [--flank-cm 0.15] [--min-ink 0.5] [--lanes 2-15] \
        [--include-marker] [--include-trimmed]
ep-craft densitometry curve --dens fig.dens.json --std "2=20,3=10,4=5" \
        [--baseline flank|global] [--fit linear|power] [--unit ug] [--drop 4]
ep-craft densitometry quantify --dens fig.dens.json --curve fig.curve.json [--load "9=2,10=2"]
ep-craft densitometry plot-bands --dens fig.dens.json [--baseline flank|global|both] [--cols 3]
ep-craft densitometry plot-curve --curve fig.curve.json [--quant fig.quant.json]
```

## 10a. marker 检出：按序直挂 + 置信标记（取代旧的补位流程）

render 的 marker 道检出**从不拒绝**：候选道逐条试，第一条过闸门的赢（同旧行为）；全不过 → 取断崖比值最好的那条道，
**照样出文档、照样出 band 线与 MW**，只是整体标成 **低置信**。检出与标定是一步：第 k 条检出带（y 升序）
直接拿标签展开后的第 k 个 MW，落在检出位置上；叫出 k 条就放 k 条线（k < N 时只挂前 k 个）。
没有配对、没有凑数（不再并线 / 插线凑够 N 条）、也不再有「等用户拖线」这一步。

- 终端：`[!] 低置信`（原因与 `picked/need` 一并打印），例如 `marker 泳道 1 未过标准：第 11 名显著度 0.26 < 下限 1.50 灰度级`。
- `ep-meta.marker_confidence = {"ok": bool, "picked": int, "need": int, "gate": null|"原因", "ratio": float|null, "nth": float|null, "next": float|null, "notes": []}`
  （替代旧版本里拆成两个键的标定标记与闸门信息）。`ok = false` 有三种来源：闸门没过、`picked < need`（条数少于 MW 槽位）、
  或 `notes` 里有劈峰提示；`gate` 记第一条原因（闸门原因 / 「条数与 MW 槽不符…」/ 劈峰提示原文），没有则 `null`。
  `ratio` / `nth` / `next` 是断崖诊断值（第 N 名与第 N+1 名的显著度及其比值）：终端打印用，也是候选道全不过闸门时
  「按断崖比值挑一条道」的依据 —— 它们不改变闸门本身的判定，但确实决定选哪条道。
- `fig.mw.json`：`calibration.low_confidence: true`（字段更名，语义不变），报告的**第一条注**写明「低置信：
  marker 道未过标准」之类的措辞 —— MW 照常给，只是不背书。
- **拖线纯是可选纠错**：用户在 Workbench 上把 `band <MW>` 线拖 / 删 / 改名，然后 `sync`，标定与 Figure 标签跟着更新。
  这不叫「认证」：sync 不清除也不新增低置信标记（它只沿用 render 时记下的 `marker_confidence`）。
- **停在一两口带也照样交付**：`band <MW>` 线只剩 0 / 1 / 2 条时 sync 不拒绝（旧规则「至少 3 条」已废）。标定点如实写：
  0 条 → `calibration.points: []`；1 条 → 单点（只是记下来，不拿它估任何 MW）；2 条 → 正常在这两点之间插值。0/1 条插不出值，
  样品带 `mw_kda: null`（终端表里不给数字）、报告注原话「标定点不足 2 条（k 条 band 线）：样品分子量无法估计」。
  文档里的低置信标记保持不变（sync 不新增也不清除）：render 时条数本来就不够（`picked < need`）的，`fig.mw.json` 里
  这条「无法估计」的注会排在低置信首注之后；render 时本来是正常的文档，删线只多出这条「无法估计」的注，不会忽然多一个
  低置信标记 —— 别把它说成新警告。交付时照实说「MW 估计不出」。
  给不出数就说给不出，**不补造标定、不插假线**。
- **线一条不剩也能读回 marker 道**：`marker_lane` 记在 `ep-meta` 里；`marker bands` 图层 0 条线时 sync 用它，
  不会因为「量不到线」就失败（`ep-meta` 里也没有才报错，提示重新 render 或补 `marker_lane`）。
- **劈峰只提示不代劳**（Q17，将错就错）：检出 ≥ 3 条时，若最小相邻间距 < 0.4 × 相邻间距中位数，
  `notes` 加一条 `识别条带偏多：y≈a 与 y≈b 疑似同一带劈成双峰，请微调`。不并峰、不拦截、不替用户改线；
  把这条提示原样转告用户即可。

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
| 老扁平 `export` 子命令、`fig.html` 编辑器、`fig.scene.json`、`fig.gel.png` | VectorCraft 里改；`ep-craft figure sync` 重新导出 `fig.png`。要单独导出 Figure 用 `ep-craft figure export DOC`（只出 PNG） |
| `--svg` | `fig.vectorcraft` 本身是矢量文档；要 SVG 在 VectorCraft 里 File → Export |
| `--no-editor` | 不再写 HTML 编辑器 |
| `--debug` 覆盖图 | Workbench 画板（`lanes` / `window` / `marker bands` / `analysis` 图层） |
| `--record` JSON | Workbench 上的线 + `ep-meta`；数值在 `fig.mw.json` |
| `--keep-lanes` | `--trim-lanes`（写要拿掉的），或在 `trimmed` 里给那条泳道画一条 trim 标记 |
| `--marker-policy` | 固定行为：marker 泳道被 trim 时只标定、不画标签 |
| `--pair-force`、`y_px` 手动标定、`--no-draft` | 配对与凑数都没有了：marker 带检出即挂 MW（按序），条数不符只降置信度（低置信标记见 §10a）。要纠正标定就在 Workbench 上拖 / 删 / 改名 `band <MW>` 线，然后 sync（纯可选） |
| `--mw-method interp` | 没有可选方法：MW 一律在相邻两条 marker 带之间插值，全局拟合被禁止 |
| `--margin-frac`、`--splice-gap-cm` | 页面规则自动推导；拼缝固定 0.15 cm |
| `--font`、`--outline` / `--no-outline` | 字体与样式在 VectorCraft 里改（默认 Arial） |

## 12. 脚本模块

运行时代码全在 repo 根的 `src/ep_craft/` 里（模块内用相对 import）；`skills/ep-craft/` 里只有文档，没有 `scripts/`。入口是安装后的 `ep-craft` 控制台命令（`pyproject.toml` 里 `ep-craft = ep_craft.cli:main`），或 `PYTHONPATH=src python3 -m ep_craft`。纯 stdlib，不需要第三方包：

| 模块 | 承担 |
|---|---|
| `src/ep_craft/magick_io.py` | magick 外呼、PPM 读取 |
| `src/ep_craft/gel_detect.py` | 认胶面：背景平面、暗度/蓝度判据、连通域、竖边倾角 |
| `src/ep_craft/gel_area.py` | stage 1：按预设定标定形、高度核对 |
| `src/ep_craft/geometry.py` | 旋转几何 |
| `src/ep_craft/crop.py` | Crop 画板：stage 1 框 ↔ 四角、预览 JPEG 与版式、读回框（`read_frame`）、`frame_delta`、按框重裁（`crop_face`） |
| `src/ep_craft/config.py` | markers / lanes / 胶体预设的读取与校验 |
| `src/ep_craft/lane_select.py` | `--trim-lanes` 语法、保留段与拼缝版式 |
| `src/ep_craft/text_layout.py` | 字体、文字量宽、标签的角度与墨迹框（`box_offsets` / `anchor_for`，页边距与落位都用它） |
| `src/ep_craft/well_grid.py` | 泳道种子：按加样孔定相的等距梳子（§6） |
| `src/ep_craft/profiles.py` | 剖面、叫带、标定、报告 v2（§7） |
| `src/ep_craft/mw_report.py` | MW 数学、标签 → MW 槽位展开、终端表与写盘 |
| `src/ep_craft/workbench.py` | Workbench 模型：建（`from_image`）、画、读回（`from_document`）、校验、泳道名匹配 |
| `src/ep_craft/figure.py` | Figure / Profiles 画板、整份文档（`build_document`）、sync 增量计划（`plan_sync`） |
| `src/ep_craft/doc_edit.py` | `edit` 的改动计划（`plan_edit`：trim、marker 泳道、泳道名），出与 `plan_sync` 同一套 op |
| `src/ep_craft/vc_file.py` | `.vectorcraft` 读写（内联 / blobs / gzip 三种编码；对象从 `src/ep_craft/templates/kinds.vectorcraft` 深拷贝） |
| `src/ep_craft/vc_control.py` | 控制口客户端：连接/启动、对话框、存盘轮询、导出核对、撤销组 |
| `src/ep_craft/sync_exec.py` | 把 `plan_sync` 的操作落到活文档上 |
| `src/ep_craft/env_check.py` | `doctor` |
| `src/ep_craft/canvas_cli.py` | 公开层级命令：`launch`、`figure export`、`workbench export` / `import` 的参数、路径保护与错误边界 |
| `src/ep_craft/workbench_io.py` | Workbench JSON 的导出 / 导入与整份校验（§13） |
| `src/ep_craft/densitometry.py` | densitometry math: `measure` (areas on both baselines), `parse_std` / `parse_lanes` / `parse_load`, `build_curve` (both fits), `format_curve`, `quantify` (area → mass through the stored fit), `format_quant`, JSON writing (§14) |
| `src/ep_craft/dens_plot.py` | the two PNGs: `plot_bands` / `plot_curve`. MVG layout only, no math (§14c) |
| `src/ep_craft/densitometry_cli.py` | argparse and handlers for `densitometry` (alias `dens`): flags, path guards, error boundary |
| `src/ep_craft/selftest.py`、`selftest_sync.py`、`selftest_crop.py`、`selftest_cli.py`、`selftest_workbench_io.py`、`selftest_densitometry.py`、`selftest_dens_plot.py`、`selftest_plot_curve_acceptance.py`、`selftest_quantify_acceptance.py` | `selftest` 的各套件（`selftest_sync` 用假控制口测 sync 执行器；`selftest_crop` 测裁切框几何与读回；`selftest_cli` 测命令树与分发；`selftest_workbench_io` 测 JSON 往返与校验）。The chain adds four suites: `selftest_densitometry` checks the analytic truth of areas, fits and quantification on a synthetic report; `selftest_dens_plot` checks the pixels of both PNGs; `selftest_plot_curve_acceptance` and `selftest_quantify_acceptance` are independent acceptance suites, for `plot-curve` (hand-written curve fixtures, geometry derived from `curve_layout`, horizontal area error bars, and no vertical error-bar ink) and for `quantify` (areas to masses through the stored fit, the out-of-range and non-positive-mass refusals, `--load`, lane shares, the terminal block, the CLI). |

## 13. Workbench 数据交换（`src/ep_craft/workbench_io.py`）

两个公开命令，读写的是**泳道数据**，不是 render 的配置（`lanes.json` / `markers.json`，§1），也不是 MW 报告（`fig.mw.json`，§7）：

```bash
ep-craft workbench export fig.vectorcraft [--out workbench.json]   # 省略 --out => JSON 写到 stdout
ep-craft workbench import fig.vectorcraft --input workbench.json [--no-sync] [--dpi 300] [--port 7797]
```

导出连控制口读**活文档**（没开就打开），未存盘的泳道名也会带上；所有几何量都是胶面 cm（原点 = 胶面左上角，与 §4/§5 同一口径）。导入先整份校验，再改活文档，全部操作落在**一个**撤销组 `ep-craft workbench import` 里；默认随后 sync（刷新 Figure / Profiles / `fig.png` / `fig.mw.json`），`--no-sync` 则只改文档、不存盘也不刷新。

### 13a. JSON 格式（`version: 1`）

```json
{
  "schema": "ep-craft-workbench-lanes",
  "version": 1,
  "doc": "/tmp/fig.vectorcraft",
  "gel": {"width_cm": 5.0, "height_cm": 3.5},
  "window": {"top_cm": 0.15, "bottom_cm": 3.395},
  "marker_lane": 1,
  "low_confidence": true,
  "lanes": [
    {"left_cm": 0.1, "right_cm": 0.5, "trimmed": false, "name": "M"},
    {"left_cm": 0.5, "right_cm": 0.9, "trimmed": false, "name": "WT"}
  ],
  "bands": [
    {"mw": 100, "y_cm": 0.8},
    {"mw": 50, "y_cm": 1.4},
    {"mw": 20, "y_cm": 2.2}
  ],
  "sample_bands": [{"lane": 2, "y_cm": 1.6}]
}
```

| 键 | 含义 |
|---|---|
| `schema` / `version` | 固定 `"ep-craft-workbench-lanes"` / `1`（只接受 v1，别的版本报错并点名 v1）。 |
| `doc` | 导出时那份文档的绝对路径；只作信息，导入**忽略**它（允许从别的胶/别的目录导入）。 |
| `gel.width_cm` / `gel.height_cm` | 胶面尺寸；导入时要与目标文档的胶面尺寸相差 ≤ 0.01 cm，否则报「export was taken from a different gel」。 |
| `window.top_cm` / `bottom_cm` | 所有泳道共用的积分窗；`top < bottom`，都在 `[0, gel.height_cm]` 内。 |
| `marker_lane` | 1 起，`1 … len(lanes)`。 |
| `low_confidence` | 低置信标记（字段更名，语义不变）。导出带来源值；导入只在来源为 `true` 时把目标标为低置信，来源为 `false` **不会清掉**目标已有的标记——导入永远不「认证」标定。 |
| `lanes[]` | 从左到右；相邻泳道共用边界（`lanes[i].left_cm == lanes[i-1].right_cm`，容差 1e-4）；`left/right_cm` 每道宽 ≥ 0.02 cm；至少一条未 trim。`trimmed` 必填布尔；`name` 可省略（省略 = 保留目标文档该道现有的名字），`null` = 占位的 `?`，`""` = 空白道。 |
| `bands[]` | marker 带线，自上而下；`mw > 0`，`y_cm` 严格递增、`mw` 严格递减，不能有重复 y。`mw` 就是 `band <MW>` 线上的数字（§5）。**0 / 1 / 2 条也合法**：标定点少时样品 MW 估计不出（0/1 条）或按两点插值（2 条），照常往返（§10a）。 |
| `sample_bands[]` | **可选**；导出只在文档有 `sample bands` 图层时出现。每条 `{lane, y_cm}`，`lane` 是 1 起的非 marker 泳道，重复的 y 会去重。省略 = 导入不动刻度；给了就按它重建刻度。 |

**不导出、导入时原样保留**：胶面图与它的 key、Crop 框、各画板、Figure / Profiles / 标签的几何、`ep-meta` 里的照片 / dpi / 预设等其它键。`peak_auto` 不导出；导入会按 `sample_bands` 相对文档的 `peak_auto` 重算 `peak_overrides`，让随后默认的 `sync` 复现导入的刻度。泳道名导出的是标签的当前文字（与 `ep-meta.lane_names`、`label lane n` 的 text 同一份）。

### 13b. 导入的校验与语义

校验在任何改动之前一次做完，任何一条不过就什么都不写（`RuntimeError` / `ValueError`，退出 2）：

- 顶层必须是 JSON 对象；`schema` / `version` 精确匹配 v1；必须有 `schema`、`version`、`gel`、`window`、`lanes`、`bands`、`marker_lane`。未知键忽略。
- 所有数字必须是有限的 JSON number（布尔、NaN、Infinity 都拒绝）。
- `gel` 尺寸 > 0，且与目标文档相差 ≤ 0.01 cm；`window` 见上；`lanes` 非空、几何连续、至少一条保留；`marker_lane` 在范围内；`bands` 见上（0/1/2 条也接受）；`sample_bands` 的泳道在范围内且不是 marker 道。
- 活文档本身必须读得通（`workbench.from_document`）；读不通就拒绝，绝不在坏文档上「半修」。
- 有 `sample_bands` **且与文档现有刻度不同**、而文档没有 `peak_auto`（无法把它换算成 override、默认 sync 复现不了）→ 拒绝，什么都不改；刻度没变则允许通过。

通过后：来源 `low_confidence: true` 时把目标也标成低置信；按导入的几何生成与 `plan_sync` 同一套 op；重写 `ep-meta.prev_centres_cm` / `lane_names`，使随后默认的 `sync` 不会把导入的泳道名改回去；把导入的 `sample_bands` 相对当前 `peak_auto` 换算成 `peak_overrides`，让默认 `sync` 复现这些刻度；然后按上面的撤销组落盘 / sync。导入一份未改过的导出是 0 个改动（`selftest_workbench_io` 覆盖）。

### 13c. 与其它文件的关系

- 泳道名 = 标签文字；改名字请改这里的 `name` 再导入，或走 `workbench edit --label n=…`（§9d），不要重跑 render。
- `bands[].mw` 必须与 `band <MW>` 线名里的数字同一套（§5）；MW 报告和 Figure 是导入 + sync 之后**派生**出来的，不写回这个文件。
- 泳道号是**保留 + trimmed 一起**从左往右数；`sample_bands` 不落在 marker 道。
- 它替代不了 `lanes.json` / `markers.json`：那两个是 render 的种子配置；这份 JSON 只记录 Workbench 当前长什么样。

## 14. Band densitometry

**SDS-PAGE only.** The chain assumes the standard lanes hold **protein standards of known mass**: every mass value comes from `--std`, and the areas are integrations of raw gray on the same gel. An agarose gel can still be sized (MW, §7), but it has no such loaded standards, so there is nothing to calibrate mass against — label it, do not quantify it.

This section states the **contract** of the chain as `specs/ep-craft-densitometry.md` defines it: the commands, the files, the JSON keys and the refusals. The whole chain `measure → curve → quantify` reads files only and never touches VectorCraft: the input is the `fig.mw.json` that `render` / `sync` already wrote (report v2, §7; `measure` also accepts a `.vectorcraft` path and resolves it to its report), and the outputs are sibling files with the same stem — `fig.vectorcraft`, `fig.png` and `fig.mw.json` are not changed by a single byte.

```
fig.mw.json ──measure──► fig.dens.json ──curve──► fig.curve.json ──quantify──► fig.quant.json
                              │                        │
                              └──► fig.bands.png       └──► fig.curve.png (sample points added with --quant)
```

Every step reads the previous step's file, so the chain can be inspected one step at a time and stopped wherever something looks wrong. Commands (short alias `ep-craft dens`):

```bash
ep-craft densitometry measure fig.mw.json                   # areas, both baselines
ep-craft densitometry curve --dens fig.dens.json --std "2=20,3=10,4=5"
ep-craft densitometry quantify --dens fig.dens.json --curve fig.curve.json [--load "9=2,10=2"]
ep-craft densitometry plot-bands --dens fig.dens.json [--baseline flank|global|both]
ep-craft densitometry plot-curve --curve fig.curve.json [--quant fig.quant.json]
```

Every command accepts `--out`; without one the output path follows the chain suffix: `fig.mw.json` → `fig.dens.json` → `fig.curve.json` → `fig.quant.json`, and the plots are `fig.bands.png` / `fig.curve.png`. An `--out` that names a directory, sits in a directory that does not exist, or points at an input this command reads is refused with an error and a non-zero exit (directories are never created, inputs are never overwritten).

### 14a. Two baselines: why flank is the default

`measure` computes two areas for **every band**:

- **global**: the top-hat baseline carried in report v2 (`profile.baseline`), half-window 0.30 cm. It treats the whole lane as background, so it dips underneath a dense band — which is exactly why `profiles.call_bands` looks for peaks on raw gray.
- **flank**: a baseline estimated from the rows immediately **either side** of the band, `--flank-cm 0.15` cm per side by default, median per side, with the two side points joined by a straight **ramp** (not a constant). Each end is clamped inside the midpoint to the neighbouring band; when a side has fewer than 2 usable rows, that side takes the global baseline value at the window edge and the band is tagged `flank_fallback_left` / `flank_fallback_right`.

The two baselines are **ordered**: the window is walked on the global baseline first, the flank ramp's two ends are taken **outside** the edges of that global window, and only then is the window walked again on the flank ramp (§14b). A global window that creeps into a neighbouring band or stops halfway across its own band therefore shifts where the flank rows are taken — which is why both shaded sets in `fig.bands.png` are worth a look: when the two areas differ a lot, the usual cause is not which baseline is right, but that the global window itself is wrong.

`curve --baseline flank` (the default) fits on the flank areas; `--baseline global` fits on the global ones. Flank is the default for a practical reason: gels often carry a gradient background and smile, and a dense band is exactly what the top-hat dips under most; together they make the global baseline underestimate the dense end of a titration — and standard lanes usually sit at that dense end. Both areas are written to `dens.json` (`area_global` / `area_flank`, at band level and at lane level), so switching baselines means re-running `curve`, not `measure`.

**Choosing a baseline from `fig.bands.png`** (§14c): the shading is the rows that were integrated and the dashed lines are the two baselines. If both dashed lines hug the background just outside the band and the shading does not creep into a neighbour, flank is sound; if the background is visibly sloped while the orange line is flat, the flank rows landed on the gradient — switch baselines or change `--flank-cm`.

### 14b. Windows, units, flags

**The window rule is walked once per band and per baseline:**

1. start at the band's `y_cm` and walk outward on `smooth(ink, 0.03 cm)` until the smoothed ink drops below `--min-ink` (default 0.5 gray levels);
2. the walk never crosses the **midpoint to the neighbouring band** (valley-to-valley), so two adjacent bands do not count each other twice;
3. the walk also stops at the first/last row of the profile (`clamped_top` / `clamped_bottom`), and at the neighbour midpoint it records `clamped_mid_up` / `clamped_mid_down`;
4. when the band is invisible (smoothed ink at the start row is already ≤ `--min-ink`) nothing is walked: a fixed window of `max(width_cm, 2·dy)` is opened and `invisible` is recorded.

The area is the **trapezoid integral** of ink over the window, with no half-cell edge approximation. The unit is `gray·cm`: `ink = baseline − gray`, `gray` runs 0–255 with darker = lower, so the area is "how much darker than the baseline, × how long". **It is a gray integration, not optical density** — there is no illumination reference, this tool does no OD, exposure or gamma conversion, so the premise that area is proportional to stain load is not corrected for; the standard curve and its residuals are what expose the deviation (§14d).

`clipped` and `flat_top` are **observations, not diagnoses**: the former means some row in the flank window has `gray ≤ 8` (near black), the latter that at least 3 rows sit within 1 gray level of the window minimum (a flat top). They say only what the pixel values in those rows are; whether the cause is too much load, overexposure or the image itself is something the standard curve cannot tell — check the photo before writing a conclusion. Likewise `peak_ink_flank` / `peak_ink_global` are the maximum ink inside their own window and are not `prominence`.

`measure` skips marker lanes and trimmed lanes by default (the ladder is not a sample, and trimmed lanes were deliberately left out of the Figure); `--lanes 2-15` replaces the whole selection and `--include-marker` / `--include-trimmed` extend the default one. Which lanes were actually measured is written into the text of `params.lanes` — `all lanes`, `all sample lanes, trimmed and marker excluded`, or a run list such as `lanes 1-5` / `lanes 5` / `lanes 2-4,9` — so the JSON says so itself.

**Known limits of the specified algorithm.** These numbers are measured on synthetic profiles and are the reason `fig.bands.png` has to be read before any mass is quoted:

- one Gaussian band (σ = 0.06 cm, amplitude 30) — the flank area lands about 3 % under the analytic area, the global area about 42 % under, because the top-hat baseline dips underneath a band of that width;
- two Gaussian bands 0.06 cm apart (both σ = 0.06 cm) — the midpoint split truncates each window, and each band keeps about two thirds of its analytic area (`clamped_mid_down` / `clamped_mid_up`).

Both are limits of the algorithm as specified, not bugs to work around: a window that is too tight loses area, and a dipping background takes more of it than a flank ramp does. What to look for in `fig.bands.png`: how far the shading runs past the band on each side, whether it stops at a neighbour midpoint, and how far the dashed baseline sits below the flat ends of the profile.

### 14c. Reading `fig.bands.png` / `fig.curve.png`

Both are standalone PNGs drawn by ImageMagick (MVG, ASCII text only, `ug` rather than `µg`), not VectorCraft artboards; `plot-*` is offline, pixel-testable and never modifies the document.

`plot-bands`: one panel per lane, `--cols` defaults to 3 and `--width` to 2400. A margin block on top carries the title (`ep-craft densitometry plot-bands  -  <report basename>`), a run-parameter line `baseline both  |  flank_cm 0.15  |  min_ink 0.5  |  6 lanes` and the legend `solid dark = gray profile  |  dashed blue = global baseline  |  dashed orange = flank ramp  |  shaded = area (gray*cm)`. In a panel the x axis is gray (`0 (dark) … 255`) and the y axis is `y_cm` (top = gel top). The dark solid line is `profile.gray`; the blue dashed line is the global baseline; the orange dashed lines are each band's flank ramp; the shading is the integration window under the active baseline, and **the area of the shading is the number**. A panel header reads `LANE n: label` (`LANE n` alone when the lane has no name), with lane totals `flank 23.4 | global 22.3 gray*cm` under `both` and `flank 23.4 gray*cm` under a single baseline; each band is labelled `b1 11.7 / 5.8 45.0 kDa` (flank / global area + MW; under a single baseline just that one area, so `b1 11.7 45.0 kDa`), a flag line reads `b1 flat top | b2 ! clipped, flat top`, and a tag line reads `b2 clamped_mid_down, flank_fallback_right`. `--baseline flank|global|both` (default `both`) decides which baseline the shading and totals use; with `both` the blue and orange windows are both drawn translucent, so a mismatch is visible at a glance.

Reading order: **windows first** — does the shading creep into a neighbour, stop halfway across the band, run to the first/last row of the profile; **baselines second** — do the dashed lines hug the background outside the band, does the orange line follow a sloped background; **numbers last** — two areas for the same band that differ a lot mean the baseline choice matters, so do not just pick the prettier one. `measure` turns these situations into sentences in its `notes` (e.g. `泳道 4 的带 1 窗口贴到相邻带中点`), matching the tag lines on the panels.

`plot-curve`: x = `volume` (area), y = `mass`. Standard points are filled circles (points removed by `--drop` are drawn hollow and labelled `dropped`), the fitted line runs solid across `range.volume_min … volume_max` and dashed through the 10 % extension on either side (it stops at the x axis rather than drawing a negative mass); underneath is a residual strip (`rel_pct` with a zero line and two dashed ±10 % guides). The strip's own scale widens when a residual is far outside ±10 % (`rel_hi`), and when the guides then sit too close together their two labels are replaced by a single `+/-10% band (dashed)` note above the strip — the rules never move, only the labelling. Top right is an annotation block with `fit` / `r2` / `n` (and how many points were dropped) / `baseline` / `unit`, the equation, and the `standard range`. The legend explains the markers in one line (filled circle for a standard point, hollow circle for a dropped point, and with `--quant` the sample markers — filled for in range, hollow for out of range), and it explains the bar once there instead of on every point. With `--quant` the samples are drawn as filled squares labelled `L<lane>` (`L5`), or `L<lane>.<band>` (`L8.2`) when that lane has several bands; an out-of-range sample is a hollow square sitting on the x axis, labelled `L<lane>.<band> out` (`L8.1 out`, or `L<lane> out` when there is no band index). When reading it, check how far each point sits from the line, whether a single point sits high or low in the residual strip, and whether `notes` mentions too few points or a large replicate CV — the fit is a black box that returns parameters, and the plot is the only place to audit it.

**Replicate bars.** A standard point that was measured more than once (`n > 1`) carries a bar drawn **along the area axis**, through the point, with small caps at both ends. Its half-length is the point's `sd` — the spread of the replicate **areas** in `gray·cm` (§14d), the same number `curve.json` stores — so the bar is read on the x axis (gray*cm) and never on the mass axis: it says how far apart the replicate measurements landed, not ±mass. A point with a single measurement (`n = 1`, `sd` `null`) gets no bar. The plot is the quick check that replicates exist and roughly how tight they are; for the exact numbers read `sd` / `cv_pct` in `curve.json` (§14d).

### 14d. Two fits, standard-lane grammar, replicates and drops

Standard lanes are given on the command line only (`--std`), never in a config file: `2=20,3=10,4=5` is `lane=mass`. Without an index the lane's **strongest band** (`strongest`) is used, `3:2=10` is the second band from the top (1-based), and `4:*=5` is the whole lane's total area. The mass unit is `--unit` (default `ug`); it is written to `curve.json`'s `unit` and does not change any number. The lanes named in `--std` must exist in the dens and must not be marker lanes; a lane given twice, a band index out of range and a non-positive mass each produce a one-line error and exit 2.

The two fits (`--fit`):

| `--fit` | Model | Parameters | Notes |
|---|---|---|---|
| `linear` (default) | a least-squares straight line between area and mass | `slope` / `intercept` / `r2` / `n` / `space: "linear"` | the usual choice; a bent calibration shows up in the residuals |
| `power` | one least-squares line on log10 of both area and mass (a power law) | `exponent` / `scale` / `r2` / `n` / `space: "log10"` | `r2` is in log space; requires every area and mass > 0 |

Parameters are stored to 4 decimals. The curve reads **area → mass**: linear `mass = intercept + slope · area`, power `mass = scale · area^exponent` (`scale = 10^c`). That is the direction `format_curve` prints, the direction `fit_params` is defined in, and the direction `quantify` evaluates (§14h); `points[].volume` is the area and `points[].mass` the mass throughout. `quantify` converts areas to masses with the **stored** parameters and never refits, so recomputing the same equation by hand reproduces what is in `quant.json`.

**Replicates**: standard lanes with the same mass collapse into one point (keeping `n`, `sd`, `cv_pct`, `lanes`, `entries`), and the fit works on points, not lanes. `sd` is the standard deviation of the **area** (the spread of areas between replicate lanes), not of mass; `cv_pct = sd / mean area × 100`. A `cv_pct > 15` goes into `notes`.

**`--drop`**: removes a standard lane from the fit (`--drop 4`, repeatable); its point stays in `points` with `used: false`, so both the plot and the JSON show it as dropped. `range`, `residuals` and `notes` use `used: true` points only. A lane named in `--drop` must be in `--std`.

**When the fit is refused**: `curve` requires ≥ 2 `used` points with at least two distinct areas; a linear slope or a power exponent ≤ 0 is refused outright with `标准点不单调：…` and a pointer to run `plot-bands` first. That error says "with the areas as measured and the mapping `--std` gives, mass does not grow with area" — it is a self-check of data against mapping and **not** a verdict about saturation or about how the gel was made; the next move is to go back to `fig.bands.png` and check windows, standard-lane order and flags, then either fix the mapping or drop a lane, instead of forcing a positive number.

### 14e. Out of range: no number

`quantify` compares every sample band's area against the curve's `range.volume_min … volume_max`:

- inside the interval: `mass` is computed from the stored `fit_params` (no refit) as `mass = intercept + slope · area` (linear) or `mass = scale · area^exponent` (power), `out_of_range: false`;
- outside it: `mass: null` + `out_of_range: true`. The band stays in the JSON with its `area` and other keys, but `mass` and `rel_pct` are `null`; the **whole lane's** `mass_total` / `conc` become `null` as well (so the other bands in that lane also get `rel_pct: null`), and `out_of_range_bands` lists which bands they were.

This follows the same rule as MW (§7: `mw_kda: null`, no extrapolation) — a plausible-looking number outside the curve is exactly the failure mode to avoid. The remedy depends on which side the area fell, and the terminal message and `notes` say so: below the range `多加点或浓缩后再跑` (load more or concentrate), above it `稀释或少点一点再跑` (dilute or load less) — bring the load inside the standard range in the right direction, or add a standard lane that brackets it and re-run `curve`. A band outside the range is never extrapolated either way.

**An in-range area can still produce no mass.** If the stored fit evaluates to zero or less for a band whose area does lie inside `range.volume_min … volume_max`, that band also gets `mass: null`, with `out_of_range: false`; the whole lane's `mass_total` / `conc` / `frac_pct_among_lanes` go `null` and one `note` names the lane, the band and the area and says the low-area end of the curve is not trustworthy. **The value the fit produced is never printed anywhere** — not in `quant.json`, not in the terminal block, not in the note as a diagnostic — so there is no zero or negative number to read, quote or mistake for a measurement. The fix is on the curve side: drop the lowest standard (`--drop`), or try `--fit power`, then re-run `curve`.

### 14f. Reading `quant.json`

`quantify --dens fig.dens.json --curve fig.curve.json [--load "9=2,10=2"]`: `--load` gives how many µL were loaded per lane (`lane=µL`) and affects `conc` only. Standard lanes (those appearing in `curve.points[].entries`) are skipped automatically and `notes` says how many were skipped. Lane order follows the dens.

The terminal block is a reading aid, not the data: a header line `[定量] 基线 flank · linear · ug（标曲 6–20 ug，3 点，r² 1.0000）；跳过 3 条标准道`, then one line per sample lane — `泳道  5 X  带1 45.0 kDa  面积 2.1  14.03 ug (100%) │ 合计 14.03 ug  浓度 7.015 ug/uL  占全胶 64%` — then the `注：` lines. A lane with several bands joins its band texts with ` · ` (`… 面积 4.5  14.869 ug · —  超出标曲范围（面积 1.75 < 1.78）：多加点或浓缩后再跑`). A band that gets no number either keeps its `带N … 面积 …` head and then prints `— 标曲给出的质量不为正：试 --drop 最低标准点或 --fit power`, or — when its area is outside the range — prints only `—  超出标曲范围（面积 0.003 < 0.871）：多加点或浓缩后再跑` with no band number and no MW (the `注：` line that follows names the lane and band); the comparison uses 3 significant digits, the same as the note. A rejected band also removes the lane's `│` tail, since `mass_total` is `null`. The header's standard range, point count and `r²` come from `curve.json` for display only — `quant.json` does not store them.

Precision: `quant.json` stores `mass` / `mass_total` / `conc` / `area` to 3 decimals and `rel_pct` / `frac_pct_among_lanes` to 2. The terminal table prints masses and concentrations with up to 3 decimals (trailing zeros dropped, so `0.522 ug` and `0.052 ug/uL` stay distinct) and prints shares as whole percent, so a percentage to quote should come from the JSON rather than from the table.

Every reported `mass` is the curve's stored fit evaluated on the band's area — `mass = intercept + slope · area`, or `mass = scale · area^exponent` for the power fit — read off the `area_<baseline>` column the curve was fitted on, so a reader can reproduce the number from `curve.json` alone.

Keep the three percentages apart:

- `rel_pct` (band level) = this band's mass ÷ **its own lane's** `mass_total` × 100 — "how much of this lane is this band";
- `frac_pct_among_lanes` (lane level) = this lane's `mass_total` ÷ the sum of known `mass_total`s × 100 — "how much of the gel is this lane";
- `conc` = `mass_total / load_ul`, numerically mg/mL (`ug` / µL).

They are three different quantities and must not be merged into one "purity": for "is this lane pure" read `rel_pct`, for "what fraction of the expression is the product" read `frac_pct_among_lanes`, for concentration read `conc`. Lanes whose `mass_total` is `null` do not enter the denominator of `frac_pct_among_lanes` (the denominator holds known totals only).

**Every measured lane that is not a standard lane counts as a sample.** `quantify` has no lane filter of its own: it takes whatever `measure` wrote, minus the standard lanes named in the curve, and puts all of it into `overall_mass` and the `frac_pct_among_lanes` denominator. `measure` in turn decides what is a marker by the report's `marker` flag, **not by the label** — a real report can carry a second ladder labelled `Marker` that is not the calibration lane, and it gets measured and quantified as if it were a sample. Before quantifying, leave out second ladders, blank lanes and anything else that is not a sample with `measure --lanes 9-12` (which replaces the default selection); otherwise those lanes inflate the gel total and shrink every real sample's lane share. `measure` records what it actually measured in `params.lanes`.

### 14g. JSON keys

**`fig.dens.json`** (`kind: "ep-craft-densitometry"`, `version: 1`):

| Key | Meaning |
|---|---|
| `version` / `kind` | fixed `1` / `"ep-craft-densitometry"` (checked on load by `curve` / `quantify`) |
| `units` | prose for `{y, gray, ink, area}`: `cm from gel-face top`, `0-255 (lower = darker)`, `baseline - gray`, `gray·cm (ink integrated over the band window)` |
| `source.report` | the path **as typed** on the command line (verbatim: a `fig.vectorcraft` argument is recorded as `.vectorcraft`, a `fig.mw.json` one as `.mw.json`); `""` for a report built in memory |
| `source.gel` / `source.window` / `source.marker_lane` | gel-face size, integration window and marker lane copied from the report, for traceability |
| `params.flank_cm` / `min_ink` / `smooth_cm` | the three values actually used (not the defaults) |
| `params.window` | fixed prose describing the window rule |
| `params.lanes` | which lanes were measured (`all lanes` / `all sample lanes, trimmed and marker excluded` / a run list such as `lanes 1-5`, `lanes 5`, `lanes 2-4,9`) |
| `lanes[]` | per lane: `lane`, `label`, `marker`, `trimmed`, `noise`; `profile` (`y_cm` / `gray` / `baseline_global`, value-for-value the report's, with no stored `ink` — recompute it for plots); `bands[]`; `area_global` / `area_flank` (sum of that lane's band areas) |
| `lanes[].bands[]` | `index` (1-based, top to bottom), `y_cm`, `mw_kda`, `strongest`, `peak_ink_global` / `peak_ink_flank`, `window_global` / `window_flank` (first and last `y_cm` of the closed interval), `area_global` / `area_flank`, `ramp`, `clipped`, `flat_top`, `notes` |
| `ramp` | `left` / `right` (each `[y_cm, gray]`), `source` (`flank` only when both sides are real flank rows, else `global`) |
| `notes` | top-level remarks (window clamped, flank fallback, invisible band, …), one sentence each (the messages are Chinese, like every CLI message) |

`bands[].notes` holds fixed ASCII tags, **never Chinese**: `invisible`, `clamped_top`, `clamped_bottom`, `clamped_mid_up`, `clamped_mid_down`, `flank_fallback_left`, `flank_fallback_right`; the Chinese sentences live in the top-level `notes`.

**`fig.curve.json`** (`kind: "ep-craft-densitometry-curve"`, `version: 1`):

| Key | Meaning |
|---|---|
| `unit` / `baseline` / `fit` | mass unit, the baseline that drove the curve, the fit model |
| `source.dens` / `source.std` / `source.drop` | input dens path, `--std` verbatim, list of dropped lane numbers |
| `points[]` | `mass`, `volume` (area), `n`, `sd` (standard deviation of the area, `null` when `n=1`), `cv_pct`, `lanes`, `entries`, `used` |
| `entries` | `[lane, band]` per replicate; `band` is a 1-based index, `"*"` the lane total, `null` the strongest band — traceable back to the exact lane and band |
| `fit_params` | `linear`: `slope` / `intercept` / `r2` / `n` / `space: "linear"`; `power`: `exponent` / `scale` / `r2` / `n` / `space: "log10"`; all to 4 decimals |
| `range` | `volume_min` / `volume_max` / `mass_min` / `mass_max`, over `used: true` points only |
| `residuals[]` | per used point: `mass` / `volume` / `predicted_mass` / `rel_pct` (`(predicted − actual) / actual × 100`) |
| `notes` | too few points (< 3), top-point residual < −10 %, replicate CV > 15 %, … |

**`fig.quant.json`** (`kind: "ep-craft-densitometry-quant"`, `version: 1`):

| Key | Meaning |
|---|---|
| `unit` / `baseline` / `fit` | copied from the curve, naming which parameter set was used |
| `source.dens` / `source.curve` / `source.load` | both input paths plus `--load` verbatim |
| `lanes[]` | `lane`, `label`, `load_ul` (`null` without `--load`), `bands[]`, `mass_total`, `conc`, `out_of_range`, `out_of_range_bands`, `frac_pct_among_lanes` |
| `lanes[].bands[]` | `index`, `y_cm`, `mw_kda`, `area` (the curve baseline's column), `mass`, `out_of_range`, `rel_pct` |
| `mass_total` / `conc` | the lane's known masses summed (`null` as soon as one band in the lane is out of range **or** its fitted mass is not positive, §14e) / `mass_total ÷ load_ul` (`null` without `--load` or when `mass_total` is `null`) |
| `out_of_range` / `out_of_range_bands` | whether any band in the lane is **out of range**, and the `index` of those bands; an empty list means all areas are inside (a band inside the range whose fitted mass came out ≤ 0 is not listed here — it has `mass: null` with `out_of_range: false`) |
| `overall_mass` | sum of every known `mass_total` |
| `notes` | the `已跳过 N 条标准道（泳道 …）` sentence, then one sentence per band that got no mass — `… 超出标曲范围（min–max）：多加点或浓缩后再跑；该道的合计与浓度不给数` or the non-positive-mass sentence (§14e) |

### 14h. Decisions and known limits

Three points call for an explicit record: one where behaviour follows a decision rather than the literal wording of the spec, one where that wording was corrected outright, and one where the specified algorithm has known accuracy limits.

1. **Direction of the fit.** The curve is **area → mass**: linear `mass = intercept + slope · area`, power `mass = scale · area^exponent` with `scale = 10^c`. `quantify` evaluates the stored `fit_params` in that direction (§14d, §14e). The spec's §4.6 wording ("inverse of the fit") does not line up with this, but the stored parameters, the equation `format_curve` prints and the acceptance item that recovers known masses within 1 % are all defined this way; the implementation follows them.
2. **Replicate error bars (resolved, ticket ep-bqv.7).** A standard point with replicates is drawn with a bar along the **area** axis, because the point's `sd` is the spread of replicate **areas** in `gray·cm` (§14d); nothing is drawn on the mass axis. The earlier spec §4.7 wording ("y-error bars") did not match what the stored number means; the spec has been corrected and now asks for the area-axis bar, so the plot, the plot legend and `curve.json` all say the same thing.
3. **Accuracy limits of the algorithm.** On synthetic profiles the flank area comes out about 3 % low and the global area about 42 % low for a single σ = 0.06 cm band; two bands 0.06 cm apart each keep about two thirds of their analytic area because the midpoint split truncates both windows (§14b). These are limits of the specified windowing and baselines, and the reason `fig.bands.png` is read before any mass is quoted.
