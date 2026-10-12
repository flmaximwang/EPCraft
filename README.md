# AgentSkill-DoingSDSPage

「做 SDS-PAGE 胶图标注」这件事的 skill 包：**人在 GUI 里标位置、脚本按配置写字**。

一句话：**胶图的条带高度与泳道中心只有人能判**（脚本不读像素），所以位置由人在 VectorCraft 里拖参考线给出；
字号 / 文字 / 颜色 / 对齐由两个 JSON 定；脚本负责把文字写对位置、写进**单独的图层**、另存出来并回校验。
位置与样式分开之后，改配色不用重标、重标不用重填表。

## 来源

| 来源 | 内容 | 沉淀成 |
|---|---|---|
| 2026-10-12 在纯化记录 `Purification-010/asset/Probe1.1_2026.10.12.vectorcraft` 上的实跑（11 个 marker 条带 × 8 条泳道，手打 18 条标签 → 自动重排） | 参考线标记约定、marker 标签右/左对齐规则、三图层布局（胶图 / `marker labels (auto)` / `lane labels (auto)`）、条数与图层回校验 | [`skills/doing-sds-page/`](skills/doing-sds-page/) |
| VectorCraft 控制口（TCP JSON）的实测调用面 | `document.inspect` / `ui.inspect` / `engine.execute` / `guide.list` / `file.saveAs` / `file.exportAs` 的用法，以及四个坑（弹窗、`currentLayer` 不可靠、保存/导出是后台任务、字号是名义字号） | [`references/vectorcraft-control-channel.md`](skills/doing-sds-page/references/vectorcraft-control-channel.md) |

## 索引

| skill | 用途 | references |
|---|---|---|
| [doing-sds-page](skills/doing-sds-page/SKILL.md) | 在 VectorCraft 工程里给胶图自动标 marker 分子量与泳道名：标记约定、两个配置 JSON、对齐规则、图层布局与回校验 | `labeling-conventions.md` · `vectorcraft-control-channel.md` |

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
```

装完三件回读：`skills show doing-sds-page` 的 `deployed_to` 含 `hermes-default`、
`skills update` 报 `up_to_date`、部署副本 `diff -rq` 与 clone 一致（部署是软链到 `~/.skills-manager/skills/`）。

（也可以走 hermes 自己的 hub：`hermes skills install "flmaximwang/AgentSkill-DoingSDSPage/skills/doing-sds-page" -y`，
但本包默认只推 NAS。）

## remote

```bash
lab       ssh://wangfanlin1@10.10.74.242/volume1/homes/wangfanlin1/AgentSkill/AgentSkill-DoingSDSPage.git
tailscale ssh://wangfanlin1@100.107.237.60/volume1/homes/wangfanlin1/AgentSkill/AgentSkill-DoingSDSPage.git
```
