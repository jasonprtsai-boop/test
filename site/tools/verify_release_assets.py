"""Verify the exact runtime and binary assets declared by release_manifest.json."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

SITE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SITE_ROOT.parent
if str(SITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SITE_ROOT))

from tmchess.release import sha256_file


def verify_manifest(project_root: Path, manifest_path: Path) -> list[str]:
    errors: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"無法讀取 release manifest：{exc}"]
    if manifest.get("schema_version") != 1:
        errors.append("release manifest schema_version 必須是 1。")
    expected_python = manifest.get("python_version")
    if platform.python_version() != expected_python:
        errors.append(
            f"Python 版本不符：目前 {platform.python_version()}，預期 {expected_python}。"
        )
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return errors + ["release manifest 沒有 files。"]
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            errors.append("release manifest 的 file 項目格式錯誤。")
            continue
        relative = item.get("path")
        if not isinstance(relative, str) or not relative or relative in seen:
            errors.append(f"release manifest 路徑缺失或重複：{relative!r}")
            continue
        seen.add(relative)
        target = (project_root / relative).resolve()
        try:
            target.relative_to(project_root.resolve())
        except ValueError:
            errors.append(f"release manifest 路徑逸出專案：{relative}")
            continue
        try:
            size = target.stat().st_size
        except OSError:
            errors.append(f"缺少發布檔案：{relative}")
            continue
        if size != item.get("bytes"):
            errors.append(f"檔案大小不符：{relative}（目前 {size}，預期 {item.get('bytes')}）")
        actual = sha256_file(target)
        expected = str(item.get("sha256", "")).lower()
        if actual != expected:
            errors.append(f"SHA256 不符：{relative}（目前 {actual}，預期 {expected}）")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    manifest_path = (args.manifest or project_root / "release_manifest.json").resolve()
    errors = verify_manifest(project_root, manifest_path)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"release assets verified: {len(manifest['files'])} files")
    if not manifest.get("public_release_ready", False):
        print("WARNING: manifest marks public release as blocked; see release_blockers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
