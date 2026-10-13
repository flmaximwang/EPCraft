# AgentSkill-DoingSDSPage

「做 SDS-PAGE 胶图标注」这件事的 skill 包，两条入口，共同点都是**位置与样式分开**：

1. **工程路**（`doing-sds-page`）：**人在 GUI 里标位置、脚本按配置写字**。
   胶图的条带高度与泳道中心只有人能判（脚本不读像素），所以位置由人在 VectorCraft 里拖参考线给出；
   字号 / 文字 / 颜色 / 对齐由两个 JSON 定；脚本把文字写对位置、写进**单独的图层**、另存工程，
   并**默认导出一张裁掉四周白边的 PNG**（纯 stdlib 像素检测）。改配色不用重标、重标不用重填表。
   子命令：`label`、`generate_template`、`selftest`。
2. **照片路**（`doing-sds-page-photo`）：**脚本探位置、人在配置里校准**。
   输入是一张手机拍的胶图（胶不占满画面），脚本自动找胶面 → 按竖边倾角转正 → 裁切并给标签留白 →
   按同形状的两个 JSON 写字 → 直接出 PNG（dpi 可调，默认 300 dpi / 胶面打印宽 8 cm）。
   位置可以用配置里的 `y_frac` / `x_frac` 校准，也能出 `--debug` 覆盖图用眼睛核。子命令：`probe`、`render`、`selftest`。

两条路共用 marker 预设（同一个 `assets/markers/*.json`，照片路按相对路径读工程路那份）。

## 来源

| 来源 | 内容 | 沉淀成 |
|---|---|---|
| 2026-10-12 在纯化记录 `Purification-010/asset/Probe1.1_2026.10.12.vectorcraft` 上的实跑（11 个 marker 条带 × 8 条泳道，手打 18 条标签 → 自动重排） | 参考线标记约定、marker 标签右/左对齐规则、三图层布局（胶图 / `marker labels (auto)` / `lane labels (auto)`）、条数与图层回校验 | [`skills/doing-sds-page/`](skills/doing-sds-page/) |
| VectorCraft 控制口（TCP JSON）的实测调用面 | `document.inspect` / `ui.inspect` / `engine.execute` / `guide.list` / `file.saveAs` / `file.exportAs` 的用法，以及四个坑（弹窗、`currentLayer` 不可靠、保存/导出是后台任务、字号是名义字号） | [`references/vectorcraft-control-channel.md`](skills/doing-sds-page/references/vectorcraft-control-channel.md) |
| 2026-10-12 用户报的两件事：导出要自动裁白边、marker 标签离胶图的空隙太宽且 `gap` 像没生效 | 导出 + 裁边做成 `label` 的默认步骤（纯 stdlib PNG 解码/裁边/编码，`selftest` 10 项）；泳道带宽从「胶图宽 ÷ 泳道数」改成相邻泳道中心距的中位数 —— 同一个 `gap: 4`，实测空隙 **18.0 pt → 4.5 pt** | [`skills/doing-sds-page/`](skills/doing-sds-page/) |
| 2026-10-13 在 `IMG_1464 (1).jpeg`（手机拍的胶）上的实跑：自动找胶/转正/裁切/写字全流程，以及踩到的坑（density 二次放大字号、`-strip` 吃 dpi、`-stroke` 吃黑字、整块胶当泳道、胶外局部背景、光晕压字） | 照片路这条 skill（探测判据、自适应留白、白边字、selftest 10 项） | [`skills/doing-sds-page-photo/`](skills/doing-sds-page-photo/) |
| 2026-10-13 用户报「标出的 3 与 4 之间实际有 4 条泳道、5 与 6 之间有 2 条」→ 只用一套阈值找泳道会把只剩一条淡带的泳道整条漏掉（15 条探成 9 条）；以及「标注 schema 参考 label_gel.py，不要重复造轮子」 | 泳道探测改成两套阈值（条带行严格 / 泳道宽松）＋宽 run 劈开＋行结构验伪＋按间距补位（标 interp）；两个 JSON 与 `label_gel.py` **同一套 schema**，`lane_pitch` 直接 import 复用；合成胶回归测试进 selftest | [`skills/doing-sds-page-photo/`](skills/doing-sds-page-photo/) |
| 雅酶（epizyme）WJ101 / WJ103 预染 Marker 的条带表（厂商产品页 + WJ10X 说明书，2026-10-12 取）与本机在用的 Tris-Tricine 配置 | 三个内置预设，**名字里带电泳体系**：`yasen-wj103-10-250kd-trisglycine`（11 条带 25/15 那一套）、`yasen-wj103-10-250kd-tristricine`（11 条带 21/14 那一套，来自本机配置）、`yasen-wj101-15-150kd-trisglycine`（9 条带）；`--preset` 直接出配置 | [`skills/doing-sds-page/assets/markers/`](skills/doing-sds-page/assets/markers/) |

## 索引

| skill | 用途 | references |
|---|---|---|
| [doing-sds-page](skills/doing-sds-page/SKILL.md) | 在 VectorCraft 工程里给胶图自动标 marker 分子量与泳道名：标记约定、两个配置 JSON（含内置 marker 预设与模版生成）、对齐规则、泳道带宽、图层布局、导出裁白边 | `labeling-conventions.md` · `vectorcraft-control-channel.md` |
| [doing-sds-page-photo](skills/doing-sds-page-photo/SKILL.md) | 照片进、标注好的 PNG 出：自动找胶面（暗度/蓝度两判据）、按竖边倾角转正、自适应留白裁切、按两个 JSON 写字、dpi 可调；泳道用宽松判据+行结构验伪、补位标 interp；schema 与 `label_gel.py` 同一套 | `photo-labeling-conventions.md` |

## 安装

用 **skills-manager**（本包在 NAS 上，无 GitHub 镜像）：

```bash
# 装法用 adopt（install 对这一层的包只认到 skills/ 这一级，会把整层 skills/ 拷进安装副本；
# adopt 能显式给出 skill 在仓库里的子路径）
~/.skills-manager/bin/skills-manager-cli skills adopt \
  --git-url "ssh://wangfanlin1@100.107.237.60/volume1/homes/wangfanlin1/AgentSkill/AgentSkill-DoingSDSPage.git" \
  --git-subpath skills/doing-sds-page \
  ~/Documents/AgentSkill/AgentSkill-DoingSDSPage/skills/doing-sds-page
~/.skills-manager/bin/skills-manager-cli skills update doing-sds-page      # 记下远端 revision
~/.skills-manager/bin/skills-manager-cli skills deploy --agent hermes-default doing-sds-page

# 照片路那条，同样用 adopt 指出它在仓库里的子路径
~/.skills-manager/bin/skills-manager-cli skills adopt \
  --git-url "ssh://***@100.107.237.60/volume1/homes/wangfanlin1/AgentSkill/AgentSkill-DoingSDSPage.git" \
  --git-subpath skills/doing-sds-page-photo \
  ~/Documents/AgentSkill/AgentSkill-DoingSDSPage/skills/doing-sds-page-photo
~/.skills-manager/bin/skills-manager-cli skills update doing-sds-page-photo
~/.skills-manager/bin/skills-manager-cli skills deploy --agent hermes-default doing-sds-page-photo
```
两条 skill 装在同一下级目录时，照片路才读得到工程路那份 marker 预设（`--preset` 要用它）；
单独装照片路就得用 `--markers` 指自己的 JSON。

装完**跑起来的环境**（系统 `python3` + ImageMagick 7）与「本 skill 环境装哪儿」这件事，
照片路在 SKILL.md 的「环境」一节里（含各平台推荐位置与 `doctor` 自检命令）——
那段是要先问用户再动手的，不默认。

装完三件回读：`skills show doing-sds-page` 的 `deployed_to` 含 `hermes-default`、
`skills update` 报 `up_to_date`、部署副本 `diff -rq` 与 clone 一致（部署是软链到 `~/.skills-manager/skills/`）。

remote 只有 `lab`（LAN 地址）与 `tailscale`（同一 NAS 仓库的第二条路），没有公开托管的 `origin` ——
hub 那套三段式标识符（`owner/repo/skills/<名>`）取不到本包，装法就是上面 skills-manager 这一条。

## remote

```bash
lab       ssh://***@10.10.74.242/volume1/homes/wangfanlin1/AgentSkill/AgentSkill-DoingSDSPage.git
tailscale ssh://***@100.107.237.60/volume1/homes/wangfanlin1/AgentSkill/AgentSkill-DoingSDSPage.git
```

## 参考过的代码仓库（与许可）

本仓库自己的代码与文档按 **[MIT](LICENSE)**（`LICENSE`，2026）。写这两条 skill 时读过或用过的外部东西，
按性质分三类逐条列出 —— 别把它们混成一句「参考了 XX」：

| 仓库 / 依赖 | 许可 | 性质 |
|---|---|---|
| [ImageMagick](https://github.com/ImageMagick/ImageMagick)（本机 7.1.1-44） | ImageMagick License（与 Apache-2.0 兼容） | **运行时依赖**：照片路所有像素活（转正/裁切/缩放/写字/写 dpi/读 PPM）都调它的 `magick` 命令；本仓库**不包含**它的代码或二进制，装它另按它自己的许可 |
| [mattaq31/GelGenie](https://github.com/mattaq31/GelGenie)（Aquilina et al., *Nat Commun* **16**, 4087, 2025） | Apache-2.0 | **只读参考**：读了论文、README 与包结构，确认「凝胶 AI 模型只做**条带**像素分割，**泳道**是规则算法从 band map 推出来的」——据此决定照片路不引模型、自己写规则链路。**未复制其代码**（若以后真要嵌它的 U-Net 权重，得按 Apache-2.0 保留署名并单独说明） |
| [weenke/gel-analysis](https://github.com/weenke/gel-analysis) | MIT | **只读参考**：GelGenie 之上做泳道分组、smiling 校正，以及它的实测坑（ladder 泳道不参与跨泳道行分组）。**未复制其代码** |
| 本 pack 自己的 [`skills/doing-sds-page`](skills/doing-sds-page/)（`label_gel.py`） | 同本仓库 MIT | **代码复用**：照片路按相对路径 import 它的 `lane_pitch()`（不重写）；两个 JSON 的 schema 与对齐口径也照它对齐，所以同一份配置文件两边都能跑 |
| 雅酶（epizyme）WJ101 / WJ103 预染 Marker 条带表 | 厂商产品页 / 说明书 | **数据**：三个内置预设照厂商表抄写，出处与取用日期写在各 JSON 的 `_source` 字段里 |

照片路**评估过但没有采用**的路线：[GelGenie](https://github.com/mattaq31/GelGenie) / YOLO11-Seg
（*Anal. Chim. Acta* 2026, doi:10.1016/j.aca.2026.345293）这类深度学习条带分割模型 ——
它们回答的是「条带在哪」，而贴标签要的是「泳道在哪」；引模型/权重会把这条 skill 变成要装环境的路线，
而现在的选择是 `python3` + ImageMagick（都是系统级、`doctor` 一条命令可验）。
