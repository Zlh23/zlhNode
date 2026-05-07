# zlhNode（含 Web Bridge）

本目录为 ComfyUI 自定义节点包；包含 **Web Bridge Input / Output**、HTTP API 与静态页。

## 网页地址（扩展名 = 文件夹名 `zlhNode`）

`http://127.0.0.1:8188/extensions/zlhNode/index.html`

## 工作流文件（与 Comfy 内置目录一致）

`/bridge/workflows` 与 `/bridge/run` 读取的是 **Comfy 保存工作流的目录**：

`ComfyUI/user/default/workflows/*.json`

即在界面里保存的工作流文件；网页 / API 里选的名称是 **文件名去掉 `.json`**。

`/bridge/run` 接受 **`user/default/workflows/` 里保存的画布 JSON**（含 `nodes`/`links`）：服务端会用内置转换器转为 API prompt（思路来自 Seth Robinson 的 `workflow_converter`）。若图中含复杂子图或罕见节点，转换失败时会返回 `canvas_to_api_failed`；此时仍可改用 **Save (API Format)** 单独保存一份。

## session_key（每轮自动）

`/bridge/run` **每次**生成新的 `session_key`，并：`set_input`（值为 **`input` 字段的纯字符串**）/ `clear_output`、写入图中全部 **WebBridgeInput / WebBridgeOutput** 节点的 `inputs.session_key`。网页轮询使用响应里的 `session_key`。请求体里 `input` / `payload` 必须是 **JSON 字符串类型**（不是对象）；`Web Bridge Input` 节点输出同名文本，便于接 CLIP Text Encode 等。

**Web Bridge Input / Output** 节点上**不再显示** `session_key` 控件（已改为隐藏输入，仅由服务端注入）。

---

此前若使用过单独的 `comfyui_web_bridge` 文件夹，请删除该目录以免重复加载同名节点。
