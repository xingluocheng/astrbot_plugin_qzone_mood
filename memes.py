"""从「表情包管理器」已下载的图库里按情绪挑图。

目录约定（meme_manager）：
    data/plugin_data/meme_manager/packs/<pack>/memes/<category>/xxx.gif
"""

from __future__ import annotations

import random
from pathlib import Path

IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTS


def meme_root(plugin_data_dir: Path) -> Path:
    return Path(plugin_data_dir) / "meme_manager"


def list_packs(plugin_data_dir: Path) -> list[Path]:
    packs_dir = meme_root(plugin_data_dir) / "packs"
    if not packs_dir.is_dir():
        return []
    return sorted(p for p in packs_dir.iterdir() if p.is_dir())


def _images_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(f for f in directory.iterdir() if f.is_file() and _is_image(f))


def pick_images(
    plugin_data_dir: Path,
    categories: list[str],
    pack_name: str = "",
    limit: int = 2,
) -> list[Path]:
    """按分类优先级挑图；分类全空时退化为该包任意图片。"""
    limit = max(0, int(limit))
    if limit == 0:
        return []

    packs = list_packs(plugin_data_dir)
    if not packs:
        return []
    if pack_name:
        packs = [p for p in packs if p.name == pack_name] or packs

    chosen: list[Path] = []
    seen: set[Path] = set()

    for category in [c.strip() for c in categories if c and c.strip()]:
        for pack in packs:
            if len(chosen) >= limit:
                return chosen
            pool = _images_in(pack / "memes" / category)
            pool = [p for p in pool if p not in seen]
            if not pool:
                continue
            pick = random.choice(pool)
            chosen.append(pick)
            seen.add(pick)
            if len(chosen) >= limit:
                return chosen

    # 兜底：分类都没命中，就从所有包里随便拿
    if len(chosen) < limit:
        everything: list[Path] = []
        for pack in packs:
            memes_dir = pack / "memes"
            if not memes_dir.is_dir():
                continue
            everything.extend(p for p in memes_dir.rglob("*") if p.is_file() and _is_image(p))
        everything = [p for p in everything if p not in seen]
        random.shuffle(everything)
        chosen.extend(everything[: limit - len(chosen)])

    return chosen


def available_categories(plugin_data_dir: Path) -> dict[str, list[str]]:
    """返回 {包名: [分类...]}，给 /空间状态 用。"""
    result: dict[str, list[str]] = {}
    for pack in list_packs(plugin_data_dir):
        memes_dir = pack / "memes"
        if not memes_dir.is_dir():
            continue
        result[pack.name] = sorted(p.name for p in memes_dir.iterdir() if p.is_dir())
    return result
