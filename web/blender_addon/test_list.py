import os
print("CWD:", os.getcwd())
root = r'\\wsl.localhost\Ubuntu\home\zlh-linux\ComfyUI\custom_nodes\zlhNode\web\blender_addon'
for name in os.listdir(root):
    print(name, os.path.getsize(os.path.join(root, name)))
