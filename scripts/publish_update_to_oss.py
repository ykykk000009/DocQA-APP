"""Publish a verified Windows update package and atomically refresh OSS latest.json.

Credentials are read from environment variables only.  The script uploads versioned
objects first and replaces ``latest.json`` last, so clients never see a version whose
installation package is not yet available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _public_url(base_url: str, object_key: str) -> str:
    return f"{base_url.rstrip('/')}/{quote(object_key)}"


def _asset(path: Path, *, key: str, public_base_url: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"安装包不存在：{path}")
    return {
        "name": path.name,
        "url": _public_url(public_base_url, key),
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "object_key": key,
    }


def _read_notes(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"Release Notes 不存在：{path}")
    return path.read_text(encoding="utf-8-sig").strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="例如 https://oss-cn-hangzhou.aliyuncs.com")
    parser.add_argument("--bucket", required=True, help="OSS Bucket 名称")
    parser.add_argument("--public-base-url", required=True, help="公开访问根地址，例如 https://download.example.com")
    parser.add_argument("--prefix", default="docqa", help="OSS 对象前缀，默认 docqa")
    parser.add_argument("--version", required=True, help="三段式版本号，例如 0.3.7")
    parser.add_argument("--standard-package", type=Path, required=True)
    parser.add_argument("--offline-package", type=Path)
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument("--release-url", default="", help="可选的发布说明页面链接")
    parser.add_argument("--access-key-id", default=os.environ.get("OSS_ACCESS_KEY_ID", ""))
    parser.add_argument("--access-key-secret", default=os.environ.get("OSS_ACCESS_KEY_SECRET", ""))
    parser.add_argument("--security-token", default=os.environ.get("OSS_SECURITY_TOKEN", ""))
    parser.add_argument("--dry-run", action="store_true", help="只打印待发布的 latest.json")
    return parser


def main() -> int:
    args = _parser().parse_args()
    prefix = args.prefix.strip("/")
    version_prefix = f"{prefix}/releases/v{args.version}" if prefix else f"releases/v{args.version}"
    manifest_key = f"{prefix}/latest.json" if prefix else "latest.json"
    assets = [
        _asset(
            args.standard_package,
            key=f"{version_prefix}/{args.standard_package.name}",
            public_base_url=args.public_base_url,
        )
    ]
    if args.offline_package is not None:
        assets.append(
            _asset(
                args.offline_package,
                key=f"{version_prefix}/{args.offline_package.name}",
                public_base_url=args.public_base_url,
            )
        )
    manifest = {
        "schema_version": 1,
        "version": args.version,
        "tag_name": f"v{args.version}",
        "published_at": datetime.now(UTC).isoformat(),
        "notes": _read_notes(args.notes_file),
        "release_url": args.release_url,
        "assets": [
            {key: value for key, value in asset.items() if key != "object_key"}
            for asset in assets
        ],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if args.dry_run:
        print(manifest_bytes.decode("utf-8"))
        return 0
    if not args.access_key_id or not args.access_key_secret:
        raise SystemExit(
            "缺少 OSS_ACCESS_KEY_ID 或 OSS_ACCESS_KEY_SECRET；请使用环境变量提供凭据。"
        )
    try:
        import oss2
    except ImportError as exc:
        raise SystemExit("缺少 oss2。请先运行：python -m pip install 'oss2>=2.19,<3.0'") from exc

    auth: Any
    if args.security_token:
        auth = oss2.StsAuth(args.access_key_id, args.access_key_secret, args.security_token)
    else:
        auth = oss2.Auth(args.access_key_id, args.access_key_secret)
    bucket = oss2.Bucket(auth, args.endpoint, args.bucket)
    asset_headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    package_paths = {args.standard_package.name: args.standard_package}
    if args.offline_package is not None:
        package_paths[args.offline_package.name] = args.offline_package
    for asset in assets:
        content_type = mimetypes.guess_type(asset["name"])[0] or "application/octet-stream"
        headers = {**asset_headers, "Content-Type": content_type}
        bucket.put_object_from_file(
            asset["object_key"], str(package_paths[asset["name"]]), headers=headers
        )
        print(f"uploaded {asset['object_key']}")

    # This final object replacement is atomic in OSS and deliberately has no cache.
    bucket.put_object(
        manifest_key,
        manifest_bytes,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-cache, no-store",
        },
    )
    print(f"published {_public_url(args.public_base_url, manifest_key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
