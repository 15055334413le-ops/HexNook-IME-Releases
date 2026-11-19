# 心宿输入法 · 本地模型

本分支存放可选下载的本地模型（不进安装包），由 mirror-oss 工作流同步到阿里云 OSS 的 models/ 目录；
同样的文件也作为 Release `models-v1` 的附件提供。客户端先读 manifest.json，再按其中的文件名下载模型，
并用清单里的 SHA-256 与 *.sha256 校验。

- corspii-S.hxlm：小档
- corspii-M.hxlm：标准档
