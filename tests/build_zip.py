"""把插件目录打成 AstrBot 能直接装的 zip。

两种布局：
  flat  —— 文件直接在压缩包根（main.py / metadata.yaml / core/…）
  dir   —— 顶层是一层目录（astrbot_plugin_menstrual_cycle/…）

AstrBot 4.29.x 的 `_resolve_archive_root_dir` 对扁平包会返回空串，
随后 `_finalize_extracted_archive` 直接早退，文件已经解在临时目录里，能装；
对带目录的包则要求解压后 target/<目录名> 存在。两种都合法，
面板上传失败时优先试 flat。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

SKIP_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "tests"}
SKIP_FILES = {".DS_Store", "Thumbs.db"}
SKIP_SUFFIX = {".pyc", ".pyo"}


def collect(root: Path) -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in SKIP_FILES or path.suffix in SKIP_SUFFIX:
            continue
        items.append((path, rel.as_posix()))
    return items


def build(root: Path, layout: str) -> Path:
    name = root.name
    out = root.parent / (f"{name}.zip" if layout == "flat" else f"{name}-withdir.zip")
    if out.exists():
        out.unlink()

    items = collect(root)
    if layout == "flat":
        # 让 metadata.yaml 排在最前：AstrBot 解析根目录时第一条就能判定
        items.sort(key=lambda item: (item[1] != "metadata.yaml", item[1]))

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, rel in items:
            arcname = rel if layout == "flat" else f"{name}/{rel}"
            info = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())

    return out


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    if not (root / "metadata.yaml").is_file():
        print(f"不是插件目录：{root}")
        return 1
    for layout in ("flat", "dir"):
        out = build(root, layout)
        with zipfile.ZipFile(out) as archive:
            names = archive.namelist()
            bad = [n for n in names if "\\" in n]
            has_dir_entry = any(n.endswith("/") for n in names)
            print(
                f"{out.name}: {len(names)} 条, {out.stat().st_size / 1024:.1f} KB, "
                f"反斜杠 {len(bad)} 条, 目录条目 {has_dir_entry}"
            )
            print(f"  首条 {names[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
