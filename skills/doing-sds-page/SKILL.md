---
name: doing-sds-page
description: 给 SDS-PAGE 胶图的 VectorCraft 工程自动标 marker 分子量与泳道名时用：GUI 里拖参考线标好位置（横线 = 条带高度、竖线 = 泳道中心），脚本按两个 JSON 写字号/颜色/对齐并分图层输出。也用于「已标好的胶图要改字号/配色/对齐」或「手打标签想批量清掉重来」。不属于本 skill 的：跑胶与成像本身（→ 成像仪厂商软件）；把这次纯化写进 vault（→ ObsidianManagement 那批）；改画板尺寸（→ Sketch 之外就直接在 GUI 里改 artboard）。
---

# doing-sds-page — 给胶图自动贴 marker 尺寸与泳道名

一条流水线：**人在 GUI 里标位置（拖参考线）→ 脚本按配置写字 → 回读换来的工程/预览图**。
位置由人判、字号与文字由 JSON 定 —— 两侧分开，改配色不用重新标、重标也不用重填表。

## When to Use

- 用户给一个画布上有胶图（一个 image 对象）的 `.vectorcraft` 工程，要求「在每个 marker 条带旁写分子量」「每条泳道上方写泳道名」「标签右对齐」「按这个配色/字号」。
- 已经在用这个脚本、这次只想改配置（换个 JSON、改字号、改颜色、换字体、改对齐）后重跑。
- 用户手打的标签要清掉重来（`--clear-text-in`）。

不属于：从图片像素里认条带（本 skill 只认人在 GUI 里标的位置，不读图）；画板尺寸问题（那是 `Shift+O` artboard 工具的事，别在 `File → Document Setup` 里找）。

## Workflow

**Step 1 · 先问清四件事（缺一件就停下来问）**

位置怎么标（默认参考线）、两个配置 JSON 用哪两个文件、输出工程放哪、要不要预览 PNG。
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

**Step 4 · 跑**

```bash
python3 scripts/label_gel.py <工程.vectorcraft> \
        --markers <marker 配置.json> --lanes <泳道配置.json> \
        --out <输出.vectorcraft> --preview <预览.png>
```

先加 `--dry-run` 看每条文字会落在哪（它不改文件、不需要最终确认），确认数量与位置再真跑。
脚本会自己开一个 VectorCraft 实例（控制口 7981）干活、干完自动关，**不用重启用户正在用的那个**。

**Step 5 · 回读，别只看脚本日志**

脚本自己会校验条数与图层（数量对不上直接非零退出、不会写一半），但**回读是另一回事**：
打开 `--preview` 出的 PNG 看一眼对齐方向、有没有标签压在泳道线上、颜色对不对；
必要时把输出的工程再开一次，数一遍三层各有什么（胶图 / `marker labels (auto)` / `lane labels (auto)`）。
图层布局是交付物的一部分 —— 三类东西分三层，用户才能单独显示/隐藏/导出。

## 检查点（CHECKPOINT）

| 🔴 触发 | 🛑 动作 |
|---|---|
| 要覆盖用户的原工程 | 🛑 STOP：默认是另存 `<名字>_labeled.vectorcraft`，原文件不动。用户明确要求就地覆盖才盖 |
| 用户手打的标签要清掉 | 🛑 STOP：`--clear-text-in <图层名>` 会删除那个图层里的**所有**文字对象（只删文字、不动图形）。删之前先列出要删哪几条、得到用户点头 |
| 位置不确定（参考线数量与标签数量对不上） | 🛑 STOP：脚本会报错退出，那是**判据**不是 bug。别改脚本放宽它 —— 回去数参考线 |

## 黑名单（别做的）

- 别用选择框缩放文字来「凑大小」：VectorCraft 的字号字段是**名义字号**，缩放写进变换矩阵，面板数字与实际大小从此不符，而且后面改字号会乘上加倍。这个脚本只写字号、不缩放。
- 别 `File → Document Setup` 里找页面尺寸（那里只有 units/出血/网格）。
- 别把 marker 标签写进胶图所在的图层：那层的显隐会连带标签。脚本按图层名分别写并校验，报错说同层就是名字取重了。
- 别在 `--dry-run` 之前就真跑整批：位置算错时真跑会产出一份「看起来正常」的错工程。

## 实测基线（实例快照，不是通用常量）

2026-10-12 在用户工程 `Probe1.1_2026.10.12.vectorcraft` 上实跑（11 个 marker × 8 条泳道）：

- 胶图对象 360.68 pt 宽、左边缘 x=121.18；8 条泳道按均分算出中心 x = 143.72 … 459.32（`marker_lane: 1`）。
- 输出三图层：`Layer 1`（胶图 image×1）、`marker labels (auto)`（11 条 type）、`lane labels (auto)`（8 条 type）。
- 单条颜色覆盖生效（70 kD = `#8b6486`、21 kD = `#4d6c8b`，其余 `#000000`）。
- 参考线模式与图层短线模式逐条比对 x/y/对齐方式一致。

## Support files

| 文件 | 承担什么 |
|---|---|
| `scripts/label_gel.py` | 全部实现：控制口客户端 + 读标记 + 写字 + 校验 + 另存/预览 |
| `references/labeling-conventions.md` | 标记约定、两个 JSON 的字段表、本机实例（路径与可复制命令） |
| `references/vectorcraft-control-channel.md` | 控制口机制、图层/字号的坑与它们为什么存在 |
| `test-prompts.json` | 触发与分界的测试题面 |
