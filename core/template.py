"""补丁模板:保存/加载"补丁 + 复制规则 + 网格(边缘线/分界线)"为模板文件。

模板与工程文件格式分离(工程要求完整字段 video_path/duration 等,模板
只要三样);补丁保存时 anchor_grid 归一化为 "project"(模板语义 = 全局,
源工程补丁可能锚定段)。模板文件存软件目录(项目根)/templates/。
"""
from __future__ import annotations

import json
import os

from .grid import GridLayout
from .project import CopyRule, Patch

FORMAT = "fenshenfu_template"
VERSION = 2

# 模板迁移注册表(同工程文件思路)
def _migrate_template_v1_to_v2(data: dict) -> dict:
    out = dict(data)
    out["version"] = 2
    return out


_TEMPLATE_MIGRATIONS: dict[tuple[int, int], object] = {
    (1, 2): _migrate_template_v1_to_v2,
}


def migrate_template_dict(data: dict) -> dict:
    fmt = data.get("format")
    version = data.get("version")
    if fmt != FORMAT or not isinstance(version, int):
        raise ValueError("不兼容的模板文件")
    while version < VERSION:
        step = _TEMPLATE_MIGRATIONS.get((version, version + 1))
        if step is None:
            raise ValueError("不兼容的模板文件")
        data = step(data)
        version += 1
    if version != VERSION:
        raise ValueError("不兼容的模板文件")
    return data

# 软件目录(项目根)= 本文件(core/)的上一级;模板文件存 软件目录/templates/
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates")


def _template_dir() -> str:
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    return TEMPLATE_DIR


def save_template(grid: GridLayout, patches: list[Patch],
                  copy_rules: list[CopyRule], path: str) -> None:
    """保存模板:网格 + 补丁 + 复制规则(补丁/复制 anchor 归一化为 project)。"""
    data = {
        "format": FORMAT,
        "version": VERSION,
        "grid": grid.to_dict(),
        "patches": [_patch_template_dict(p) for p in patches],
        "copy_rules": [_copy_template_dict(r) for r in copy_rules],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _patch_template_dict(p: Patch) -> dict:
    d = p.to_dict()
    d["anchor_grid"] = "project"   # 模板 = 全局语义(段锚定补丁归一化)
    return d


def _copy_template_dict(r: CopyRule) -> dict:
    d = r.to_dict()
    d["anchor_grid"] = "project"   # 模板 = 全局语义(段锚定复制规则归一化)
    return d


def load_template(path: str) -> dict:
    """加载模板并校验格式;返回 {"grid": GridLayout, "patches": [...], "copy_rules": [...]}。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data = migrate_template_dict(data)
    return {
        "grid": GridLayout.from_dict(data.get("grid") or {}),
        "patches": [Patch.from_dict(d) for d in data.get("patches", [])],
        "copy_rules": [CopyRule.from_dict(d) for d in data.get("copy_rules", [])],
    }


def default_template_path(name: str) -> str:
    """模板文件路径:软件目录/templates/<名>.fstpl.json。"""
    return os.path.join(_template_dir(), f"{name}.fstpl.json")
