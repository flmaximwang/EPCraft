# 照片标注：字段表、探测机制、命令

## 1. 两个 JSON —— schema 与 `doing-sds-page` 的 `label_gel.py` 一致

字段名与口径完全沿用工程路那份（`skills/doing-sds-page/references/labeling-conventions.md`），
所以同一个配置文件两边都能跑：这次实测把工程路的 `Probe1.1_2026.10.12.json` 直接喂给照片路，正常出图。

### markers.json

| 键 | 含义 | 默认 |
|---|---|---|
| `size` | 字号（**成品像素 px**；工程路那边是画布 pt） | 16 |
| `color` | 默认颜色 | `#000000` |
| `gap` | 文字与**泳道带边缘**的水平间距 (px) | 4 |
| `marker_lane` | marker 是左起第几条**探到的**泳道（1 起）；`0` = 第 1 条左边一条泳道带处；**不给就自动选条带最多的那条** | 自动 |
| `marker_lane_x` | 直接给 marker 泳道的位置（<1 当占胶面宽度的比例，≥1 当输出像素） | 无 |
| `lane_width` | 泳道带宽手给值（同上单位规则）；不给就按相邻泳道中心距的中位数（`lane_pitch`） | 自动 |
| `lane_edge` | `inner` = 用泳道中心当边缘（不偏半个带宽） | 无 |
| `font` | 只有写**文件路径**（`.ttf/.ttc/.otf`）才生效；工程路的字体名（如 `Source Sans 3`）会被忽略并回退 Arial | Arial |
| `labels` | **从上到下**，每条带一项：字符串或 `{"text","size","color","font"}`（后三个可省） | — |
| `y_frac` | **照片路额外字段**：每条带的显式位置，0=胶面顶边、1=胶面底边（条带数对不上时用它校准） | 无 |

### lanes.json

| 键 | 含义 | 默认 |
|---|---|---|
| `size` / `color` | 同上（px） | 16 / `#000000` |
| `gap` | 文字**底边**离胶面顶边的距离 (px) | 0 |
| `align` | `center` / `left` / `right`，相对该泳道中心 | `center` |
| `labels` | **从左到右**，每条泳道一项（字符串或对象）；`text` 留 `""` 就不写 | — |
| `x_frac` | **照片路额外字段**：每条泳道的显式中心，0=胶面左、1=胶面右 | 无 |

对齐与左右判断的规则跟工程路一字不差：marker 泳道中心在**泳道区中线**左边 ⇒ 右对齐、
右边缘贴泳道带左边，右边则翻成左对齐；中线取 `(min+max)/2` 的泳道中心，不取整图中线。

## 1b. 成品尺寸怎么定（实际尺寸）

`assets/gels/*.json` 是胶体物理尺寸预设，文件名就是 `--gel` 的名字（默认 `mini-gel`：宽 8.6 × 高 7.2 cm）：

| 键 | 作用 |
|---|---|
| `width_cm` | **定标**：胶面在成品里横跨 `width_cm/2.54 × dpi` 个像素 |
| `height_cm` | 只用于**核对**（探测框比例推出来的高度与它差 >10% 会警告，不强改图） |

`--gel-cm 8.6x7.2` 可以直接给（宽x高，cm），压过预设。字号是绝对像素，所以
「同一物理尺寸 + 不同 dpi」不是等价图：96 dpi 的 16 px 字比 300 dpi 的 16 px 字**打印出来大 3 倍**。

## 2. 探测怎么做的（以及为什么这么选）

**a) 认胶 —— 两个判据**
- `dark`（默认）：拟合灯箱背景。取样带取四边 8%–14% 的**环带**（跳过最外圈相框），两轮最小二乘
  拟一个平面（每轮丢掉明显低于平面的样本），再判 `像素 < 平面 - max(6, 平面//20)`。
  对中性/银染的胶也成立（胶总比灯箱暗）。
- `chroma`：判 `B-R > 22`。本机这张照片里染色胶 B-R ≈ +33…+47、背景 ≈ −6…−15；
  但**蓝光灯箱**下整块屏幕都蓝，所以它是备选不是默认。`probe` 会同时报两条判据的胶框，一致才可信。
- 掩膜先去毛刺，取最大且**不贴画面边**的连通域（灯箱相框、螺丝靠这条排掉），再按行/列覆盖率 50% 裁掉光晕毛边。
- 倾角由左右竖边在中段 20%–80% 行上的最小二乘拟合给出。

**b) 条带行 vs 泳道 —— 两套阈值，别混**
| | 阈值 | 用来干什么 |
|---|---|---|
| 严格 | 胶内中位数 − 3·1.4826·MAD（本次 147.7） | 找**条带行**（每条泳道自己的行剖面 → 峰簇中心） |
| 宽松 | 胶内中位数 − k·1.4826·MAD，k=1.5（本次 154.3） | 找**泳道**（只有一条淡带的泳道也要算进来） |

只用严格判据的后果（实测）：15 条泳道的胶探到 **9 条**，中间 4 条（只有一条淡带）整条漏掉。

**c) 泳道从列剖面到「几条」的四道工序**
1. 宽松判据下的列剖面 → 连续 run → 相邻中心 < 3 px 的合并。
2. **间距**量自「条带最密的那一行」（本次第 356 行，9 条带），不量整块胶的列剖面
   （梯子条带比泳道宽，列剖面会把相邻泳道糊在一起，量出的间距 14.4 vs 真值 18.0）。
3. 过近合并（< 0.7×间距，梯子中间有暗缝会被拆成两段的合回来）；过宽劈开（> 1.5×间距，按间距均分）。
4. **验伪**：候选泳道要有「一条 ≥2 点的严格判据行」（真条带），或宽松判据行剖面有峰且峰比中位数高 ≥2
   （淡带）；胶面自身的竖纹是均匀的，被这一条挡掉（实测竖纹会造出幽灵泳道）。
   最后两两实测泳道之间按间距补空位（差值对不上整数格就**不补**并报出来），补出来的标 `interp`。

## 3. 本机可复制命令（2026-10-13 实跑）

环境自检 / 本机环境位置：

```bash
# 环境自检（python / magick / 字体 / 预设目录 / 本机选定的环境根）
python3 scripts/gel_photo.py doctor
# 环境根从哪来（优先级）：--env-root > $GEL_PHOTO_ENV_ROOT > ~/.config/doing-sds-page-photo/env.json
# 选定的位置只记在**用户机器**上（那个 json 的 env_root），skill 里不留任何一台机器的具体路径；
# doctor 会把读到的值和来源打出来，换机器重新问一次即可
```

```bash
cd /Users/maxim/Documents/Experts/DoingSDSPage/skills/doing-sds-page-photo

# 1) 探测 + 覆盖图（红框=胶面、红线=实测泳道、橙线=补位、蓝线=带边、绿线=条带行）
python3 scripts/gel_photo.py probe "/Users/maxim/Downloads/IMG_1464 (1).jpeg" \
        --debug /Users/maxim/Downloads/gel_demo/IMG_1464_debug.png

# 2) 出成品（15 条泳道全部实测，0 条补位）
python3 scripts/gel_photo.py render "/Users/maxim/Downloads/IMG_1464 (1).jpeg" \
        --out /Users/maxim/Downloads/gel_demo/IMG_1464_labeled.png \
        --preset yasen-wj103-10-250kd-tristricine \
        --lanes /Users/maxim/Downloads/gel_demo/lanes.json --gel mini-gel --dpi 96
# → 404×296 px / 96 dpi（胶面 8.60×6.70 cm，成品 10.70×7.83 cm）/ pHYs=(3779,3779,1)
# 同一条命令换 --dpi 300 → 1145×893 px，物理尺寸不变，字相对变小、标签就不撞了

# 3) 直接复用工程路写好的配置（schema 同一套，实测可用）
python3 scripts/gel_photo.py render "/Users/maxim/Downloads/IMG_1464 (1).jpeg" --out /tmp/x.png \
        --preset yasen-wj103-10-250kd-tristricine \
        --lanes "/Users/maxim/Documents/Lab/zsqlab_10_gammaPFD-Fiber_2026.06.25/Obsidian_zsqlab_10/Logs/2026/10/09/Purification-010/asset/Probe1.1_2026.10.12.json"

# 自检（不读用户文件）
python3 scripts/gel_photo.py selftest        # 10/10（含合成胶泳道探测的回归测试）
```

常用开关：`--cue chroma|union`、`--ink-thr`（改泳道判据阈值）、`--longest`（分析缩略图长边，默认 700）、
`--rect x0,y0,x1,y1`（跳过自动找胶）、`--angle` / `--no-rotate`、`--margin-frac`、
`--no-outline`、`--font <ttf>`、`--debug`。

## 4. 坑的来龙去脉（都实测过）

| 现象 | 原因 | 处置 |
|---|---|---|
| 15 条泳道只探到 9 条 | 泳道也用严格判据（只有一条淡带的泳道整条漏） | 泳道换宽松判据（k=1.5），外加行结构验伪 |
| 补位补出一堆 7 px 宽的假泳道（34 条） | 间距量自整块胶列剖面（偏小 14.4），宽 run 又没劈开 | 间距改量自「最密的一行」；宽 run 按间距均分 |
| 幽灵泳道（该处什么都没有） | 胶面竖纹在宽松阈值下亮起来 | 行结构验伪：真条带是峰，竖纹是平的 |
| 只探到一条「泳道」，宽 = 整块胶 | 用「比灯箱暗」找条带 → 整块胶都是命中 | 认胶用灯箱平面，找条带用胶内中位数 |
| 梯子的分子量标签上下糊在一起 | 固定 16 px 字号 + 96 dpi：相邻条带只差 ~11 px | 提 dpi（192–300）、把 `markers.size` 降到 ~10 px、或只标关键几条带 |
| 字大得离谱，9 pt 占满半张图 | magick 看到 density=300，把 `-pointsize` 又乘了 300/72 | 写字前 `-density 72`，dpi 留到最后写 |
| PNG 没有 dpi，`identify` 显示 72 | `-strip` 把之后设的 `-density` 一起丢了（IM7） | 用 `+profile "*"`，别用 `-strip` |
| 黑字被吃成白字 | `-stroke` 是画在字**上面**的 | 分两批：先整批白底、再整批黑字（0.07 em） |
| 分子量整体错位一位 | 梯子实际条带数 ≠ 预设条带数，脚本只能按位置配前 N 个 | 换对预设 / 给 `y_frac`；别默认脚本配对了 |
| 泳道号被胶顶光晕压住 | 胶顶到背景是渐变，掩膜边界 ≠ 视觉边界 | 留白按整字高算 + 文字细白边 |

## 5. 与隔壁 skill 的分工

| | `doing-sds-page` | `doing-sds-page-photo` |
|---|---|---|
| 输入 | `.vectorcraft` 工程（胶图是一个 image 对象） | 照片文件（JPEG/PNG…） |
| 位置 | 人在 GUI 拖参考线/画短线 | 脚本按像素探，配置可校准（`y_frac`/`x_frac`） |
| 产物 | 另存工程 + 裁白边 PNG | PNG（dpi 可调） |
| 依赖 | VectorCraft 控制口 | ImageMagick 7（`magick`） |
| schema | `markers.json` / `lanes.json` | **同一套**（+ `y_frac`/`x_frac` 两个照片路额外字段） |
| 共用 | `lane_pitch` 等逻辑在 `label_gel.py` 里，照片路直接 import 复用（`_sibling()` 按相对路径找） | |

两条路做的是同一件事的两端：**位置与样式分开**（位置能校准、样式不重标）。
