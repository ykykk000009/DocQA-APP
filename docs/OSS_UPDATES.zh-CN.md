# 阿里云 OSS 更新发布

应用将优先读取 `latest.json`；无法读取时才回退到 GitHub Release。这样受网络限制、
不能访问 GitHub 的用户仍可完成“检查更新 → 下载 → 校验 → 安装”。

## 一次性准备

1. 创建 OSS Bucket，并为下载域名配置 HTTPS（推荐自定义域名）。
2. 允许客户端匿名读取 `docqa/latest.json` 和 `docqa/releases/*`；不要将写权限公开。
3. 为发布人员创建仅能写入该前缀的 RAM 用户或 STS 角色。凭据只通过环境变量使用，绝不写入仓库或 `latest.json`。
4. 设定公开清单地址，例如：`https://download.example.com/docqa/latest.json`。

## 构建与发布

先将 OSS 清单地址写入构建命令。它会被写入包内的 `version.json`，安装后的客户端会自动使用它：

```powershell
$env:UPDATE_MANIFEST_URL = "https://download.example.com/docqa/latest.json"
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\packaging\windows\build_windows.ps1 `
  -OnlineModelAssetsPath .\.online-assets `
  -UpdateManifestUrl $env:UPDATE_MANIFEST_URL
```

构建会生成同一份标准 ZIP 和 `-offline.zip` 兼容别名，以及各自的 `.sha256` 文件。发布前先安装 OSS SDK，并以环境变量配置凭据：

```powershell
python -m pip install ".[release]"
$env:OSS_ACCESS_KEY_ID = "<RAM 或 STS AccessKeyId>"
$env:OSS_ACCESS_KEY_SECRET = "<RAM 或 STS AccessKeySecret>"
# 使用临时凭据时额外设置：$env:OSS_SECURITY_TOKEN = "<token>"

python .\scripts\publish_update_to_oss.py `
  --endpoint "https://oss-cn-hangzhou.aliyuncs.com" `
  --bucket "<bucket-name>" `
  --public-base-url "https://download.example.com" `
  --prefix "docqa" `
  --version "0.3.7" `
  --standard-package .\dist\windows\DocQA-v0.3.7-win-x64.zip `
  --offline-package .\dist\windows\DocQA-v0.3.7-win-x64-offline.zip `
  --notes-file .\docs\RELEASE_NOTES_v0.3.7.md
```

脚本先上传固定版本路径的 ZIP；所有文件成功后才写入 `docqa/latest.json`。ZIP 使用一年不可变缓存，
`latest.json` 使用 `no-cache, no-store`，因此客户端每次检查都会读取最新版本。

## latest.json 格式

```json
{
  "schema_version": 1,
  "version": "0.3.7",
  "tag_name": "v0.3.7",
  "published_at": "2026-07-27T00:00:00+00:00",
  "notes": "完整更新内容",
  "assets": [
    {
      "name": "DocQA-v0.3.7-win-x64.zip",
      "url": "https://download.example.com/docqa/releases/v0.3.7/DocQA-v0.3.7-win-x64.zip",
      "sha256": "<64 位十六进制哈希>",
      "size": 123456
    }
  ]
}
```

可先添加 `--dry-run` 查看将写入的清单，不会访问 OSS。
