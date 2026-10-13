---
name: doing-sds-page-photo
description: 用一张含 SDS-PAGE 凝胶的**照片**（手机拍的、胶不占满画面）直接出标注好的 PNG 时用：脚本先自动找出胶面（比背景暗 / 蓝度两个判据）、按竖边倾角转正、裁切并留出标签白边，再按 marker 与泳道两个 JSON 在裁好的图上写字，输出 dpi 可调的 PNG（默认 300 dpi、胶面打印宽 8 cm）。两个 JSON 与隔壁 doing-sds-page 的 label_gel.py **同一套 schema**（marker_lane / marker_lane_x / lane_width / lane_edge / gap / align / labels 口径一致），照片路另外多 y_frac / x_frac 两个校准字段。也用于「标签位置/字号/颜色改一下重跑」「探测结果不对要出 debug 覆盖图核对」「换一张照片同一套配置复用」。不属于本 skill 的：在 VectorCraft 工程里人拖参考线标注（→ doing-sds-page）；跑胶与成像本身（→ 成像仪厂商软件）；从像素里判读条带是什么蛋白（本 skill 只按配置或剖面把已有文字放上去）。
---

# doing-sds-page-photo — 照片进、标注好的 PNG 出

一条流水线：**自动找胶 → 转正 → 裁切（留标签白边）→ 按两个 JSON 写字 → 出 PNG**。
与隔壁 `doing-sds-page` 的分工：那个是「人在 GUI 标位置、脚本在工程里写字」；
这个是「照片里位置由脚本探、人在配置里校准」，产物只有 PNG（没有工程文件）。
**两个 JSON 的字段与口径完全沿用 `label_gel.py`**（`lane_pitch` 也直接 import 它，不重写）。

脚本三个子命令（`scripts/gel_photo.py`，纯 stdlib + ImageMagick 7 的 `magick`）：

| 子命令 | 干什么 |
|---|---|
| `probe` | 只探测不写字：报胶框/角度/两个条带判据/泳道 x 与每条泳道的条带行；`--debug` 出一张覆盖图 |
| `render` | 出成品 PNG：裁切 + 转正 + 按胶体实际尺寸缩放 + dpi + 写字 |
| `doctor` | 环境自检：python / `magick`（含版本）/ 字体 / 预设目录 / 用户选的环境安装位置（`--env-root`）；缺什么说怎么补 |
| `selftest` | 不读用户文件：自检 PPM 解析、magick 旋转方向、连通域、平面背景、峰簇、旋转框裁切、dpi 与字号、补位算法、间距量法、合成胶泳道探测（10 项） |

## When to Use

- 用户给一张手机拍的胶图照片（胶在画面中间、四周是灯箱/桌面），要一张能放汇报或投稿的标注图。
- 用户要「按这个 marker 表把分子量标在条带旁、泳道名写在上方」，且**位置不想手标**。
- 已经跑过、这次只改配置（换 marker 预设、改字号/颜色/gap、改打印宽度或 dpi）后重跑。
- 探测结果不对劲，要先看一眼 `--debug` 覆盖图再决定改哪个参数。

不属于：读像素判读条带是什么蛋白、多少 kD；把标注写回 VectorCraft 工程（→ `doing-sds-page`）；
从胶图逆推实验结论（那是人的事）。

## 环境（先问用户装哪儿，再动手）

**依赖两样，都是系统级、不需要往别处装东西：**

| 依赖 | 用来干什么 | 怎么装 |
|---|---|---|
| `python3`（3.8+，只用 stdlib） | 跑本脚本 | 系统自带；无需 pip/第三方包 |
| ImageMagick 7 的 `magick` | 所有像素活（转正/裁切/缩放/写字/写 dpi）；贴图与判据全走它 | macOS `brew install imagemagick` · Windows `winget install ImageMagick.ImageMagick`（或 `choco install imagemagick`）· Linux `apt install imagemagick` / `dnf install ImageMagick` |

字体用系统自带的（macOS/Windows 的 Arial；Linux 上另给 `--font /usr/share/fonts/.../DejaVuSans.ttf`）。

**本 skill 自己的环境（虚拟环境 / 便携版 ImageMagick / 模型或缓存目录）装哪儿 —— 动手前先问用户，不要默认。**
用户没答就**停下来问**，把下面这几条作为推荐一起给出：

| 平台 | 推荐 | 备选 |
|---|---|---|
| macOS | `/Applications/DoingSDSPage` | `$HOME/DoingSDSPage` |
| Windows | `C:\ProgramData\envs\DoingSDSPage` | `%USERPROFILE%\DoingSDSPage` |
| Linux | `$HOME/Applications/DoingSDSPage` | `$HOME/DoingSDSPage` |

拿到答复后：① 先 `ls` 确认那个目录是空/不存在，别覆盖已有内容；② 在那里建目录或 venv；
③ `doctor --env-root <位置>` 确认存在且可写；④ **把选定位置记在用户机器上，不要写进本 skill**：

```bash
mkdir -p ~/.config/doing-sds-page-photo
printf '{"env_root": "<用户选的位置>"}' > ~/.config/doing-sds-page-photo/env.json
```
之后任何一次 `doctor` 会直接从那儿读出来（来源会标在输出里），**不用再问一遍**；换机器或换人重新问一次。
读取优先级：`--env-root` > 环境变量 `GEL_PHOTO_ENV_ROOT` > `~/.config/doing-sds-page-photo/env.json`。
上表只是**推荐**，不是本机的值 —— skill 里不写任何一台机器的具体路径。

**自检（装完/换机器都先跑这两条）：**

```bash
python3 scripts/gel_photo.py doctor [--env-root <用户选的位置>]   # python / magick / 字体 / 预设目录 / 环境根
python3 scripts/gel_photo.py selftest                            # 算法自检，13 项
```
`doctor` 缺什么就直接说怎么补，退出码非 0 表示环境不齐；「环境根」那行会标出读的是哪个来源
（`--env-root` / `GEL_PHOTO_ENV_ROOT` / 本机配置文件）。

## Workflow

**Step 1 · 先跑 probe，再决定参数**（别一上来就 render）

```bash
python3 scripts/gel_photo.py probe <照片> --debug /tmp/gel_debug.png
```
输出里要核四样：`cross_cue`（暗度/蓝度两条判据的胶框是否一致 —— 一致才可信）、
`angle_vertical`（竖边倾角）、两个判据阈值（条带行严格 / 泳道宽松）、
以及泳道条数与「实测 / 补位」的拆分。`--debug` 那张覆盖图（红框=胶面、红线=实测泳道、
**橙色线=补位泳道**、蓝线=泳道带边、绿线=条带行）用眼睛过一遍：框没框住胶、
哪条线不落在泳道上、两条线之间是不是还看得出有泳道。

**Step 2 · 两个 JSON（schema 同 `label_gel.py`，字段表见 references）**

```bash
python3 scripts/gel_photo.py render --list-presets      # 列出现有 marker 预设
python3 scripts/gel_photo.py render <照片> --out out.png \
        --preset yasen-wj103-10-250kd-tristricine \
        --lanes lanes.json
```
- `markers.json`：`labels`（**从上到下**每条带一项）、`size`/`color`/`gap`(pt)/`font`、
  `marker_lane`（左起第几条泳道；**不给就自动选条带最多的那条**）、`marker_lane_x`、
  `lane_width`、`lane_edge`（`inner`）。
- `lanes.json`：`labels`（**从左到右**，字符串或 `{text,...}` 都行）、`align`/`size`/`color`/`gap`。
- 照片路额外两个校准字段（工程路没有）：`markers.y_frac`（每条带的 y，0=胶顶 1=胶底）、
  `lanes.x_frac`（每条泳道的 x，0=胶左 1=胶右）。

**Step 3 · render**

```bash
python3 scripts/gel_photo.py render <照片> --out out.png \
        --preset <名> --lanes lanes.json \
        --gel mini-gel --dpi 96 [--debug /tmp/dbg.png]
```
- **成品就是实际尺寸**：按胶体预设（`assets/gels/*.json`，默认 `mini-gel` = 宽 8.6 cm × 高 7.2 cm）用**宽度定标**，
  胶面在成品里横跨 `宽_cm/2.54×dpi` 个像素；`--dpi` 写进 PNG 的 pHYs（默认 **96**）。
  例：`--gel mini-gel --dpi 96` → 胶面 325×253 px（8.60×6.70 cm），成品 404×296 px（10.70×7.83 cm）；
  `--dpi 300` → 同一物理尺寸、胶面 1016×792 px。高度不按预设强改，按图的实际比例算，差 >10% 会警告。
- 临时换尺寸：`--gel-cm 8.6x7.2`（宽x高，cm）压过预设。
- **`size`/`gap` 的单位是 pt（物理尺寸，dpi 无关，与 `label_gel.py` 同口径），默认 8 pt。**
  改 dpi 只改像素数，字与胶的相对大小不变（实测 8 pt：96 dpi 下 11 px≈3.4% 胶宽，300 dpi 下 33 px≈3.2% 胶宽）。
  参照一下这个默认值的来处：曾用「16 px」——96 dpi 时相当于 12 pt（太大、密梯子标签会撞），300 dpi 时只有 3.84 pt（太小），
  **8 pt 落在两者之间**（≈2.8 mm 高，占 8.6 cm 宽胶的 3.3%）。
- 成品**物理尺寸**在 96 与 300 dpi 下完全一致（实测都是胶面 8.60×6.70 cm、成品 9.46×7.73 cm），只有像素数不同。
- 位置类字段（`marker_lane_x` / `lane_width` / `x_frac` / `y_frac`）：**<1 当占胶面的比例，≥1 当输出像素**。
- marker 条带行来自**细栅格扫描**（默认按长边 1200 再审一遍行剖面，泳道仍用 `--longest`）：
  700 尺度上梯子相邻条带只差 4–8 px、会糊成一条，行位置就整体漂（实测 9 条 vs 真值 11 条）。
- 脚本按文字**真实包围盒**（`magick label:` 量的，不是估的）算四周留白：哪个方向露出去就补哪个方向；
  marker 泳道贴着胶左缘时左边会明显撑开（本次实测 1250×857 px）。
- 文字默认带细白边（`--no-outline` 关）：照片里胶顶到背景是渐变，纯黑字压在光晕上会糊。

**Step 4 · 回读成品图**（别只看日志）

打开 PNG 看三件事：① 分子量数字是否每个都对着一条带（数量对不上时脚本只按位置配前 N 个，会打印警告）；
② 泳道号是否落在浅色留白上、个数与位置对不对（对着 `--debug` 覆盖图数）；
③ dpi 是否真写进去（`magick identify -format "%x" out.png`，300 dpi 显示 118.11 像素/厘米，是同一个数）。
本次实测：`pHYs=(11811,11811,1)`。

**Step 3a · 挨得极近的一对带（21/20 kD 这种）：合成一条标签**

分子量接近的一对带在胶上只差 ~1.6 mm，而 8 pt 字高 2.8 mm —— **同一侧竖直方向放不下两行字**，
换 dpi 也没用（字号与间距同比例缩放）。脚本会把「厚 ≥1.6× 中位数」的行报出来（`fat_rows`），
看到这个提示就把配置里那一对写成一条：`labels: [..., "21/20 kD", ...]`（同时少一个 `y_px`）。
条目数与实测条带数一对上，配对自动正确。要两条都标，得把一个标签挪到梯子另一侧并拉引线。

**Step 3b · 条带数与配置不一致时：默认不出图，走手动标定**

自动探到的条带数与 `markers.labels` 条数不等时，脚本**拒绝出图**（`--pair-force` 才能硬配）。
因为「按位置硬配」会把分子量整体错位 —— 宁可不出图也不要出一张看起来正常的错图。手动标定三步：

1. `python3 scripts/gel_photo.py probe <照片> --debug debug.png`
   → 覆盖图左侧有 **y 标尺**（数字 = 分析尺度 y），绿线 = 自动探到的条带行，红/橙线 = 泳道。
2. 照着标尺把**每条带**的 y 抄下来（从上到下，与 `labels` 一一对应）写进 `markers.json` 的 `y_px`：
   ```json
   { "marker_lane": 8, "size": 8, "gap": 4,
     "y_px": [284.3, 291.1, 297.9, 308.0, 318.9, 327.0, 335.9, 347.4, 354.2, 367.7, 387.4],
     "labels": [ ... 11 个分子量 ... ] }
   ```
   默认**不吸附**（自动探到的行在密集条带上有 ~0.7 mm 系统性偏上，吸附会把你在图上读的准值拉偏）；
   想要「给个大概值就自动落到最近条带上」就写 `"snap": true`。
3. 再跑 render；`render --debug` 出的覆盖图会把**配置指定的行画成品红线**，一眼就能核对落点。

## 检查点（CHECKPOINT）

| 触发 | 动作 |
|---|---|
| 要建/写「本 skill 的环境」目录（venv、便携版 ImageMagick、缓存…） | 先问用户装哪个位置（推荐见上表），别默认替用户选；建之前先确认那儿是空的；定下来**记在本机配置文件里**，别写进 skill |
| 本机已记过位置（`doctor` 的「环境根」有值） | 直接用那个，不要再问；只有换成没记过的机器才重新问 |
| `doctor` 报 `magick : 缺` | 停下装 ImageMagick 7，别绕行（没有它本 skill 一行都跑不了） |
| 探测到的条带数与配置的分子量数不一致 | 脚本打印警告并按位置配前 N 个（占位）。要正式出图就停下来校准：给 `markers.y_frac`、换对预设，或调 `--ink-thr` / `--longest` 后重探 |
| `lanes.labels` 条数 > 探到的泳道数 | 脚本报错退出（那是判据不是 bug）：先用覆盖图数清那张胶有几条泳道，缺的用 `lanes.x_frac` 显式给位置 |
| 覆盖图里出现橙色（补位）泳道 | 那是**估计值**（该处一条带都没显色，没有像素证据）。要精确就必须给 `x_frac` |
| 两条判据（dark / chroma）给出的胶框差很多 | 先看覆盖图，必要时 `--cue chroma` 或直接 `--rect x0,y0,x1,y1` 跳过自动找胶 |
| 要覆盖已有的输出 PNG | 脚本直接覆盖同名文件（不像隔壁 skill 会另存）。输出名自己起清楚 |

## 黑名单（踩过的）

- **别用整块胶的列剖面量泳道间距**：梯子的条带比一条泳道还宽，列剖面会把相邻泳道糊成一片，
  量出来的间距偏小（实测 14.4 px vs 真值 18.0），后面按它补位就会多补泳道。
  间距一律量自**条带最密的那一行**（那一行里各条带是彼此分开的）。
- **别只用一套条带阈值**：找条带行要严格（中位数 − 3·MAD），找泳道要宽松（− 1.5·MAD）。
  实测：只用严格判据，15 条泳道的胶只探到 9 条（一条只有一条淡带的泳道整条漏掉）。
- **宽松判据的噪声要靠「行结构」挡**：胶面自身的竖纹在宽松阈值下也会亮起来 → 幽灵泳道。
  判据是「有一条 ≥2 点的行」或「行剖面有峰且峰比中位数高 ≥2」（竖纹是均匀的，没有峰）。
- **宽得离谱的 run 要先劈开**：右半边一排单条带在宽松阈值下糊成宽 34 px 的 run（真值 2 条、间距 17），
  不劈开后面的补位会把这片再细分成 7 px 一条（实测 34 条泳道）。
- **别拿「整块胶比背景暗」去找条带**：胶体整块暗于灯箱，那样会得到「一条宽 = 整块胶的巨型泳道」。
  认胶用灯箱背景（环带亮样本拟的平面），找条带用胶内中位数。
- **别在设了 dpi 的图上直接写字**：magick 会把 `-pointsize` 再乘一次 `density/72`，9 pt 变成 ~156 px（实测大 4 倍）。
  先 `-density 72` 写字，dpi 元数据留到最后写。
- **别用 `-strip`**：IM7 里它会把之后设的 `-density` 一起丢掉，PNG 出来没有 pHYs。要去元数据用 `+profile "*"`。
- **别用 px 当字号单位**：px 是绝对像素，改 dpi 就改字与胶的相对大小（实测 16 px 在 96 dpi 下是 12 pt、
  在 300 dpi 下只有 3.84 pt）。用 pt（本 skill 现在的口径），dpi 只改像素数。
- **条带数对不上时别硬配**：脚本默认拒绝出图就是这个原因；要出就走上面的手动标定（`y_px`）。
  硬配（`--pair-force`）出来的图看起来正常、其实分子量整体错位 —— 这是本 skill 出过的最坏的一种错。
- **密梯子标不完不是 bug**：3 mm 的实际条带间距在 96 dpi 只有 ~11 px，任何可读字号都放不下；
  提 dpi、减小 `markers.size`、或只标关键几条带。
- **别用「描边 + 填充」一次写完带白边的字**：`-stroke` 画在字上面，宽一点就把黑字吃成白字。
  分两批：先整批白底、再整批黑字。
- 旋正只按竖边拟合的倾角（本次 0.174°），没做透视矫正 —— 斜着拍的梯形胶会歪，那时用 `--angle` 手动压。

## 实测基线（2026-10-13，一张手机照片，不是通用常量）

输入 `/Users/maxim/Downloads/IMG_1464 (1).jpeg`（3024×4032，EXIF orientation=1），分析缩略图 700 px：

- 胶框：两条判据一致（dark `(115,207,395,443)` / chroma `(116,208,395,442)`，分析尺度）→ 原图
  `x 668–2252, y 1313–2546`，占画面 16%。倾角 **0.174°**（竖边拟合，129 点/边）。
- 判据：胶内中位数 161、MAD 3 → 条带行阈值 **147.7**（严格）· 泳道阈值 **154.3**（宽松）。
- 间距 **18.0 px**（量自第 356 行 = 条带最密的一行）；探到 **15 条**泳道，**全部实测、0 条补位**
  （修之前：只用严格判据 + 无补位 → 9 条，比真值少 6 条）。
- 用户核的两处：**第 3 条与第 4 条（梯子）之间 4 条、第 5 条与第 6 条之间 2 条** —— 修后与真值一致。
- 成品（实际尺寸路线，`--gel mini-gel`）：胶面 **8.60×6.70 cm**、成品 **9.46×7.73 cm**（含标签留白），
  96 dpi → **358×292 px**、300 dpi → **1117×913 px** —— **物理尺寸两者一致**，只有像素数不同（8 pt 字：11 px vs 33 px）。
- marker 条带行：细栅格（1200）扫中间那把梯子得 **11 条**（与 WJ103 预设 11 条一致）；700 尺度只有 9 条。
  残余误差：自动行比条带视觉中心偏上 ~3–4 分析 px（≈0.7 mm）—— 要更准就 `y_px` 手动标定。
- 手动标定实测：`markers.json` 里给 11 个 `y_px`（对着 debug 图 y 标尺读）→ 标签落到条带上，
  debug 覆盖图的品红线（配置行）与照片上的条带重合。
- 单次跑 ≈10 s（纯 stdlib，全 CPU）。
- 已知偏差：该梯子实测 9–12 条带、预设 11 条 → 脚本按位置配前 N 个（占位），颜色配对会整体错位一位。
  这是**配置问题不是脚本问题**：用对 marker 预设或给 `markers.y_frac`。
- `selftest` 10/10，其中第 10 项是**合成胶回归测试**：5 条泳道（第 3 条只有一条很淡的带）+ 一道竖纹，
  要求正好探出 5 条、位置误差 ≤3 px、竖纹不算泳道 —— 这条就是本次修的 bug 的看护测试。

## Support files

| 文件 | 承担什么 |
|---|---|
| `scripts/gel_photo.py` | 全部实现：magick 外呼、PPM 读像素、认胶（平面背景+蓝度）、泳道探测与补位、转正/裁切/留白、按实际尺寸定标、写字、PNG dpi |
| `assets/gels/*.json` | 胶体物理尺寸预设（`mini-gel` 宽 8.6 × 高 7.2 cm；文件名就是 `--gel` 的名字，`_source` 写出处） |
| `markers.json` 的 `y_px` | **手动标定**用的绝对行位置（分析尺度 y，照 `probe --debug` 图的 y 标尺读）；`snap: true` 才吸附到自动行 |

竖直对齐口径照隔壁 `label_gel.py`：marker 标签框**中心**对条带行（那边 `center_y`）、
泳道名框**底边**在胶顶之上 `gap`（那边 `bottom_y`）。注意 `magick -annotate` 的 y 是文字框
**顶边**，所以上面两个都要减掉框高（`text_extent` 量的真实框高），不能拿字号乘个系数凑。
| `references/photo-labeling-conventions.md` | 两个 JSON 的字段表（同 `label_gel.py`）、探测机制与阈值、本机可复制命令、七个坑的来龙去脉 |
| `test-prompts.json` | 触发与分界的测试题面 |

依赖与安装位置：见上面「环境」一节（`python3` + ImageMagick 7；本机实测 python 3.9.6 / ImageMagick 7.1.1-44）。
`python3 scripts/gel_photo.py doctor` 一条命令就能把这几项验完。
marker 预设与本 skill 复用隔壁 `label_gel.py` 的 `lane_pitch`：按
`<本 skill>/…` → `<同 pack 的 doing-sds-page>/…` 顺序找；**只单独装本 skill 时找不到**，
那时用 `--markers` 指自己的 JSON（`lane_pitch` 会走本文件里的等价实现）。
