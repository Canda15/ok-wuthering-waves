"""上游仓库不跟踪的自有代码目录。

凡是"我们和上游不一样"的部分都放这里,上游永远不会改动这个路径,因此
``git merge upstream/...`` 不会在这里产生冲突。约定见 SELFHOST.md。
"""

import os

# 不同 OCR 尺寸会积累 oneDNN 算子缓存；各启动入口先加载本包，须早于首次推理设置。
# 保留用户的显式容量，包括禁用值 0；旧 DNNL 别名也不能被新变量的默认值遮蔽。
if not os.environ.get('ONEDNN_PRIMITIVE_CACHE_CAPACITY') and not os.environ.get('DNNL_PRIMITIVE_CACHE_CAPACITY'):
    os.environ['ONEDNN_PRIMITIVE_CACHE_CAPACITY'] = '64'
