# VectorCraft 控制口：协议、用到的命令、以及四个坑

脚本不靠 GUI 自动化，走的是 VectorCraft 自带的**控制通道**（TCP 上的行式 JSON）。

## 协议

- 起实例：`VectorCraft --control <端口>`（脚本默认 7981，`--port` 可改）。已经在监听的实例会直接被复用，
  脚本只在「没人监听」时才自己起一个，且**只关自己起的那个**。
- 一次调用 = 一行请求 `{"id":N,"method":"…","params":{…}}` + 一行回包
  `{"ok":true,"result":…}`；失败是 `{"ok":false,"error":"…"}`（脚本把它变成异常）。
- 超时给足：开工程、保存、导出都是同步等待的，脚本客户端设 120 s。

## 用到的 method

| method | 用途 |
|---|---|
| `document.inspect {depth,childLimit}` | 图层/对象树、`selectionBounds`、`currentLayer` |
| `ui.inspect` | 窗口层状态，含当前弹窗 `ui.dialog` |
| `ui.dialog.cancel` | 关掉打开工程时弹的对话框 |
| `engine.execute {command,params}` | 菜单/工具底层命令，脚本用的都在这一层 |
| `guide.list` / `guide.add {vertical,pos}` | 读/加参考线（参考线就是标记本身） |
| `file.open` / `file.saveAs` / `file.exportAs` | 开、另存、导出 PNG |
| `app.quit` | 收工时关掉自己起的实例 |

`engine.execute` 里用到的命令：`text.create {x,y,text,size,color,font}`、`text.setStyle {id,justify}`、
`object.move {dx,dy}`、`select.set {ids}`、`edit.clear`、`layer.new {name,top}`、`layer.setCurrent {id}`、
`layer.highlight {ids,mode}`、`window.panel {panel:"layers"}`。

## 四个坑（都是实测踩到的）

1. **打开工程可能弹对话框**：图是内嵌的但链接文件不在时，VectorCraft 会弹 `missingLinks`，不关掉后续调用全卡住。
   脚本开完文件先 `ui.inspect` 看 `ui.dialog`，有就 `ui.dialog.cancel`（最多 6 次）。
   `file.open` 的返回里带 `missingLinks` 数量，可以据此提示用户（不影响本流程）。

2. **`currentLayer` 刚打开文档时不可靠**：新图形落进的是 `currentLayer`，但文档刚打开时图层行状态还没建好，
   `layer.setCurrent` 可能不生效 —— 结果文字全落到 `Layer 1`（胶图那层）去。
   脚本的做法：开图层面板 → 最多 10 次（`setCurrent` → 校验 → 不行就 `layer.highlight` 再校验）→ 仍不对就报错退出。
   **别把这段重试删掉**，它修的是真出现过的错层。

3. **保存与导出是后台任务**：`file.saveAs` / `file.exportAs` 立刻返回，文件要过一会儿才落地。
   脚本轮询到「文件存在且大小 > 0」才报成功，超时（60 s）就非零退出。只看调用是否成功会写出「成功」却找不到文件。

4. **字号是名义字号**：用选择框拖角点缩放文字时，缩放写进的是对象的变换矩阵，run 里的 `size` 一个字都不动 ——
   于是面板显示 16、实际渲染 32。`Object → Transform → Reset Bounding Box` **不修这个**（在缩放过的文字上实测返回
   `changed: 0`）。所以本脚本**只写字号、从不缩放**：面板数字与实际大小永远一致。
   （顺带：Character 面板的 hScale/vScale 是同类乘数，区别只是它把倍数写在百分比字段里。）

## 胶图与坐标

- 「胶图」= 文档里**面积最大的可见 image 对象**（脚本按 `width*height` 取最大，`--marks layer:` 与对齐判断都用它）。
- 参考线与对象坐标同一套（文档坐标系，原点在左下），所以参考线的 `pos` 可以直接当条带高度 / 泳道中心用。
- 预览导出：`file.exportAs {path, format:"png", scale:2, background:…}`；默认白底，`--preview-background` 可换
  `transparent` / `black` / `#rrggbb`。
