# Windows 下载安装

## 下载与使用

1. 从 [GitHub Releases](https://github.com/ykykk000009/enterprise-rag/releases/latest) 下载 `EnterpriseDocumentRAG-windows-x64-online.zip`。
2. 将 ZIP 完整解压到 E 盘，例如 `E:\EnterpriseDocumentRAG`；不要在压缩包内运行。
3. 双击 `EnterpriseDocumentRAG.exe`，等待浏览器自动打开。
4. 创建知识库，添加文档目录并扫描。
5. 关闭启动器窗口即可退出。

## 使用特点

- 适用于 Windows 10/11 x64；
- 无需安装 Python或申请大模型 API Key；
- 只监听本机 `127.0.0.1`；
- 数据库、索引和模型保存在程序目录的 `user-data`；
- 在线包约 351.4 MB，首次索引/问答会下载 BGE 和 Qwen，之后可离线运行；
- 扫描型 PDF 需要逐页 OCR，处理时间会明显长于文本型文档。

## 数据与升级

`user-data/data` 保存数据库和索引，`user-data/models` 保存模型，`user-data/launcher.log` 保存启动日志。升级前备份 `user-data`，替换程序文件时保留该目录。

## 常见问题

- **未知发布者**：当前版本未使用商业代码签名。请从本项目 Release 下载并核对 SHA-256。
- **浏览器未打开**：保持启动器运行，点击“打开应用”，或查看 `user-data/launcher.log`。
- **模型下载失败**：确认可访问 Hugging Face，关闭程序后重试；已下载文件不会重复下载。
- **卸载**：关闭程序后删除整个目录；需要保留数据时先备份 `user-data`。
