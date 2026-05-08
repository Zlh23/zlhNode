# IndexOB (Object Index) 读取经验

## 结论：通过 Compositor Viewer Node 读取

**不能**用 `bpy.data.images.load()` 加载 EXR 文件来读取 IndexOB，因为：
1. EXR 是多层文件，`bpy.data.images.load()` 只加载 Combined 层的 R 通道（颜色值），不是 IndexOB
2. EXR 的 IndexOB 通道可能被归一化到 0~1 范围，或者有其他编码方式

**正确做法**：通过 Compositor 的 Viewer Node 读取

```
CompositorNodeRLayers.IndexOB -> CompositorNodeViewer.inputs[0]
```
渲染后读取 `bpy.data.images["Viewer Node"].pixels` 的 R 通道。

### 关键点

1. **输出名称**：在 Cycles 下 IndexOB 输出名称是 `"Object Index"`，不是 `"IndexOB"`
2. **无需解码**：Viewer Node 输出直接是 pass_index 的 float 值，`round(val)` 即可
3. **无边缘混合**：Cycles 的 IndexOB 是精确的整数，不会有 Eevee 的抗锯齿/SSS 边缘混合问题
4. **噪声极少**：1 sample 就够，只有极少量噪声像素（0.01% 阈值即可过滤）

### 完整步骤

```python
# 1. 启用 IndexOB pass
scene.view_layers[0].use_pass_object_index = True

# 2. 分配 pass_index
obj.pass_index = 1

# 3. 清空其他 mesh 的 pass_index（防止干扰）
for obj in bpy.data.objects:
    if obj.type == "MESH" and obj not in target_meshes:
        obj.pass_index = 0

# 4. 设置 Compositor
scene.render.use_compositing = True
tree = bpy.data.node_groups.new(name="...", type="CompositorNodeTree")
scene.compositing_node_group = tree

rl = tree.nodes.new(type="CompositorNodeRLayers")
viewer = tree.nodes.new(type="CompositorNodeViewer")
tree.links.new(rl.outputs["Object Index"], viewer.inputs[0])

# 5. 渲染
bpy.ops.render.render(write_still=True)

# 6. 读取 Viewer Node
img = bpy.data.images["Viewer Node"]
pix = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
indexob = pix[:, :, 0]  # R 通道 = pass_index

# 7. 解码：直接 round
decoded = np.round(indexob).astype(np.int32)
present = set(decoded[decoded > 0])
```

### 各引擎的 IndexOB 差异

| 引擎 | Compositor 输出名 | 存储格式 | 边缘混合 |
|------|------------------|---------|---------|
| Cycles | `"Object Index"` | 原始 float 值 | 无 |
| Eevee | `"IndexOB"` 或 `"Object Index"` | 可能有归一化 | 有（抗锯齿/SSS） |

### Python API 中 Render Result 的限制

- `bpy.data.images["Render Result"]` 在 `write_still=False` 时尺寸为 0x0，无法读取
- 只有通过 Compositor Viewer Node 才能在渲染后通过 Python 访问 IndexOB
