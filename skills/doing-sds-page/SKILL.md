---
name: doing-sds-page
description: 给 SDS-PAGE 胶图的 VectorCraft 工程自动标 marker 分子量与泳道名时用：GUI 里拖参考线标好位置（横线 = 条带高度、竖线 = 泳道中心），脚本按两个 JSON 写字号/颜色/对齐、分图层写、另存并默认导出裁掉白边的 PNG。也用于「已标好的胶图要改字号/配色/对齐」或「手打标签想批量清掉重来」或「先出一份 markers/lanes 配置模版」。不属于本 skill 的：跑胶与成像本身（→ 成像仪厂商软件）；把这次纯化写进 vault（→ ObsidianManagement 那批）；改画板尺寸（→ 就直接在 GUI 里改 artboard）。
---

# doing-sds-page — 给胶图自动贴 marker 尺寸与泳道名

一条流水线：**人在 GUI 里标位置（拖参考线）→ 脚本按配置写字 → 另存 + 导出裁掉白边的 PNG**。
位置由人判、字号与文字由 JSON 定 —— 两侧分开，改配色不用重新标、重标也不用重填表。

脚本是三个子命令（`scripts/label_gel.py`）：

| 子命令 | 干什么 |
|---|---|
| `label` | 读标记 → 写字 → 另存 `<名字>_labeled.vectorcraft` → **默认导出 `<名字>_labeled.png` 并裁掉四周白边** |
| `generate_template` | 生成 `markers.json` / `lanes.json` 模版（已有配置的别覆盖，默认拒绝） |
| `selftest` | 不连 VectorCraft，自检 PNG 解码/裁边/编码与泳道带宽算法 |

## When to Use

- 用户给一个画布上有胶图（一个 image 对象）的 `.vectorcraft` 工程，要求「在每个 marker 条带旁写分子量」「每条泳道上方写泳道名」「标签右对齐」「按这个配色/字号」。
- 已经在用这个脚本、这次只想改配置（换个 JSON、改字号、改颜色、换字体、改对齐、改 gap）后重跑。
- 用户手打的标签要清掉重来（`--clear-text-in`）。
- 手上还没有配置文件、要从模版起手（`generate_template`）。
- 交付时要一张成品图：导出 + 裁白边现在是默认行为（要纯工程文件加 `--no-preview`）。

不属于：从图片像素里认条带（本 skill 只认人在 GUI 里标的位置，不读图）；画板尺寸问题（那是 `Shift+O` artboard 工具的事，别在 `File → Document Setup` 里找）。

## Workflow

**Step 1 · 先问清四件事（缺一件就停下来问）**

位置怎么标（默认参考线）、两个配置 JSON 用哪两个文件、输出工程放哪、要不要那张导出图。
用户没说配置文件名时，用工程同目录的 `markers.json` / `lanes.json`。**别自己去猜配置文件名**——
配置驱动的东西猜错会静默写出标签全错的工程（本机实例里那两个文件叫 `10%-Tris-Tricine.json` 与
`Probe1.1_2026.10.12.json`，得显式传 `--markers` / `--lanes`）。

**Step 2 · 教用户在 GUI 里标位置**（只有人能判条带高度与泳道中心）

- `Cmd+R` 显示标尺 → 从**上方**标尺往下拖出**水平**参考线 = 一条 marker 条带的高度中心；从上到下对应 `markers` 的 `labels`。
- 从**左侧**标尺往右拖出**垂直**参考线 = 一条泳道的中心；从左到右对应 `lanes` 的 `labels`。
- **marker 泳道也要拖一条竖线**（不想在它上面写字就把 text 留 `""`）。
- 标完 `Cmd+S` 保存再跑脚本。
- 不想用参考线：在一个专用图层里画短线（竖长 = 泳道、横长 = 条带），跑时加 `--marks layer:<图层名>`。

**Step 3 · 写/核对两个 JSON**（字段表见 `references/labeling-conventions.md`）

没有现成配置文件时先出配置，再按这台胶的实际情况改：

```bash
python3 scripts/label_gel.py generate_template --list-presets             # 看内置 marker 预设
python3 scripts/label_gel.py generate_template --preset <预设名> --dir .  # marker 用内置预设
python3 scripts/label_gel.py generate_template --dir . --lanes 8          # 没有对应预设就用通用模版
```
它默认**不覆盖**已有文件（要覆盖得加 `--force`），写完会回读一遍确认是合法 JSON。
内置预设（`assets/markers/*.json`）是照**厂商页面**抄的条带表，出处与取用日期写在文件里的 `_source`；
预设只是起手模板 —— 出完要对着这台胶实际用的那管 marker 核一遍条带（本机就有一处出入，见 references §6）。

**Step 4 · 跑**

```bash
python3 scripts/label_gel.py label <工程.vectorcraft> \
        --markers <marker 配置.json> --lanes <泳道配置.json> \
        --out <输出.vectorcraft>
```

先加 `--dry-run` 看每条文字会落在哪、泳道带宽算成多少（它不改文件、不导出），确认数量与位置再真跑。
导出的 PNG 默认是 `<输出名>.png`（要换路径给 `--preview`，要**不导出**给 `--no-preview`）。
脚本会自己开一个 VectorCraft 实例（控制口 7981）干活、干完自动关，**不用重启用户正在用的那个**。

**Step 5 · 回读，别只看脚本日志**

脚本自己会校验条数与图层（数量对不上直接非零退出、不会写一半）、导出后也会报「裁白边 1191×1684 → 695×742」，
但**回读是另一回事**：打开那张 PNG 看一眼对齐方向、标签离胶图的空隙、有没有压在泳道线上、颜色对不对；
必要时把输出的工程再开一次，数一遍三层各有什么（胶图 / `marker labels (auto)` / `lane labels (auto)`）。
图层布局是交付物的一部分 —— 三类东西分三层，用户才能单独显示/隐藏/导出。

## 检查点（CHECKPOINT）

| 🔴 触发 | 🛑 动作 |
|---|---|
| 要覆盖用户的原工程 | 🛑 STOP：默认是另存 `<名字>_labeled.vectorcraft`，原文件不动。用户明确要求就地覆盖才盖 |
| 要写 `markers.json` / `lanes.json` 但目录里已经有同名文件 | 🛑 STOP：那是用户的配置，`generate_template` 默认拒绝覆盖；要覆盖也得先问 |
| 用户手打的标签要清掉 | 🛑 STOP：`--clear-text-in <图层名>` 会删除那个图层里的**所有**文字对象（只删文字、不动图形）。删之前先列出要删哪几条、得到用户点头 |
| 位置不确定（参考线数量与标签数量对不上） | 🛑 STOP：脚本会报错退出，那是**判据**不是 bug。别改脚本放宽它 —— 回去数参考线 |

## 黑名单（别做的）

- 别用选择框缩放文字来「凑大小」：VectorCraft 的字号字段是**名义字号**，缩放写进变换矩阵，面板数字与实际大小从此不符，而且后面改字号会乘上加倍。这个脚本只写字号、不缩放。
- 别 `File → Document Setup` 里找页面尺寸（那里只有 units/出血/网格）。
- 别把 marker 标签写进胶图所在的图层：那层的显隐会连带标签。脚本按图层名分别写并校验，报错说同层就是名字取重了。
- 别在 `--dry-run` 之前就真跑整批：位置算错时真跑会产出一份「看起来正常」的错工程。
- **别用「胶图宽 ÷ 泳道数」当泳道带宽**（这个坑修过一次，2026-10-12）：胶图对象常常比泳道区宽（照片自带白边/背景），
  那样算出的带宽会大一倍以上，标签就飘得离胶图老远，**`gap` 看着像没生效**。带宽一律取**相邻泳道中心距的中位数**
  （`lane_pitch()`）；要手给就写 `lane_width`。左右半边的判断同理，按**泳道区**的中线，不按胶图对象的中线。
- 别把 `--no-preview` 当成默认：交付要图时就该导出并裁白边；只想拿工程文件才加它。

## 实测基线（实例快照，不是通用常量）

**2026-10-12 在用户工程 `Probe1.1_2026.10.12.vectorcraft` 上实跑**（11 横 + 8 竖参考线，配置是
`10%-Tris-Tricine.json` / `Probe1.1_2026.10.12.json`）：

- 读到的标记 = 8 条泳道 + 11 条条带，与两个 JSON 的 11 / 8 条标签对上（数量不符会当场报错，先 `--dry-run` 确认）。
- 胶图 515.4×354.3 pt，左边缘 x=-44.1、顶边 y=53.2 —— **胶图对象比泳道区宽得多**，正是带宽坑的现场。
- 泳道带宽 ≈36.6 pt（相邻泳道中心距的中位数；按胶图宽算会得到 64.4 pt，就是那个 bug）。
- marker 泳道 x≈190.7（泳道区中线 318.0 的左边）⇒ 带边 172.4 ⇒ 11 条分子量右对齐、锚点 x=168.4。
- **实测空隙 4.5 pt**（`gap: 4`；修 bug 前是 18.0 pt，同一个 `gap: 4`）—— 用逐列像素量出来的。
- 泳道名居中、底边 y=53.24；输出三图层：`Layer 1`（`clip group×1, image×1, rectangle×1`）、
  `marker labels (auto)`（type×11）、`lane labels (auto)`（type×8）。
- 工程另存 410,172 字节；导出 PNG 1191×1684 → **裁白边后 695×742 px**（scale 2，白底）；单条颜色覆盖生效
  （70 kD = `#8b6486`、21 kD = `#4d6c8b`）。
- `selftest` 10/10（含 5 种行滤波器的解码还原、透明底按 alpha 裁边、全白图报错、lane_pitch 三条）。

## Support files

| 文件 | 承担什么 |
|---|---|
| `scripts/label_gel.py` | 全部实现：控制口客户端 + 读标记 + 写字 + 校验 + 另存 + PNG 解码/裁边/编码，三个子命令 |
| `assets/markers/*.json` | 内置 marker 预设（文件名 = 预设名，`--preset` 用；每个文件里 `_source` 写出处与取用日期） |
| `references/labeling-conventions.md` | 标记约定、两个 JSON 的字段表、内置预设清单、本机实例（路径与可复制命令） |
| `references/vectorcraft-control-channel.md` | 控制口机制、图层/字号/导出的坑与它们为什么存在 |
| `test-prompts.json` | 触发与分界的测试题面 |
