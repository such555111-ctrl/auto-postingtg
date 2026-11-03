"""Определение бренда по тексту поста.

Правила:
  * учитываются название бренда и его алиасы (регистр и «ё/е» не важны);
  * совпадение ищется по границам слов: «lv» не найдётся внутри «solvent»;
  * алиас, оканчивающийся на «*», допускает любые окончания: «шанел*» -> шанель, шанели, шанелью;
  * бренд из ПЕРВОЙ непустой строки (заголовка поста) получает большой приоритет —
    так пост «CHOPARD … кристаллы Swarovski» определится как Chopard, а не Swarovski;
  * при равенстве выигрывает бренд, который встретился раньше в тексте;
  * ничего не найдено -> None («не определён»).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from db import Database

HEADLINE_WEIGHT = 100
MAX_ALIAS_LEN = 60
MAX_ALIASES = 50


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    return text


def _compile_alias(alias: str) -> re.Pattern[str] | None:
    a = normalize(alias).strip()
    prefix = a.endswith("*")
    a = a.rstrip("*").strip()
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", a) if p]
    if not parts:
        return None
    body = r"[\s\-]+".join(parts)
    tail = r"\w*" if prefix else ""
    return re.compile(rf"(?<!\w){body}{tail}(?!\w)", re.UNICODE)


@dataclass
class BrandDef:
    id: int
    name: str
    aliases: list[str]
    patterns: list[re.Pattern[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for a in [self.name, *self.aliases]:
            key = normalize(a).strip()
            if key in seen:
                continue
            seen.add(key)
            pat = _compile_alias(a)
            if pat:
                self.patterns.append(pat)


def detect_brand(text: str, brands: list[BrandDef]) -> BrandDef | None:
    norm = normalize(text or "")
    if not norm.strip():
        return None
    head = next((ln for ln in norm.splitlines() if ln.strip()), "")

    best: tuple[tuple, BrandDef] | None = None
    for brand in brands:
        hits = 0
        first_pos = len(norm) + 1
        in_head = False
        for pat in brand.patterns:
            found = list(pat.finditer(norm))
            if found:
                hits += len(found)
                first_pos = min(first_pos, found[0].start())
            if pat.search(head):
                in_head = True
        if hits == 0:
            continue
        score = hits + (HEADLINE_WEIGHT if in_head else 0)
        key = (-score, first_pos, brand.id)
        if best is None or key < best[0]:
            best = (key, brand)
    return best[1] if best else None


def parse_aliases(raw: str) -> list[str]:
    """«Chanel, шанел*; coco chanel» -> нормализованный список без дублей."""
    out: list[str] = []
    for chunk in re.split(r"[,;\n]+", raw):
        a = normalize(chunk).strip()
        if a and a != "-" and a not in out:
            out.append(a[:MAX_ALIAS_LEN])
    return out[:MAX_ALIASES]


async def load_brand_index(db: Database) -> list[BrandDef]:
    return [BrandDef(i, n, a) for i, n, a in await db.brands_with_aliases()]


async def reclassify(db: Database) -> int:
    """Переопределяет бренд у всех постов без ручной привязки. Возвращает число изменившихся."""
    index = await load_brand_index(db)
    changed = 0
    for row in await db.auto_posts():
        found = detect_brand(row["text"], index)
        new_id = found.id if found else None
        if new_id != row["brand_id"]:
            await db.set_post_brand(row["id"], new_id, manual=False)
            changed += 1
    return changed


# Стартовый набор (создаётся один раз при пустой таблице брендов; дальше правится в боте)
DEFAULT_BRANDS: dict[str, list[str]] = {
    "Chanel": ["шанел*"],
    "Chopard": ["шопард*"],
    "Cartier": ["картье"],
    "Van Cleef & Arpels": ["van cleef", "vca", "ван клиф*", "ванклиф*"],
    "Bvlgari": ["bulgari", "булгари"],
    "Tiffany": ["тиффани"],
    "Dior": ["диор*"],
    "Gucci": ["гуччи"],
    "Louis Vuitton": ["lv", "луи виттон", "луи вюиттон"],
    "Hermes": ["hermès", "эрмес*", "гермес*"],
    "Swarovski": ["сваровски"],
    "Pandora": ["пандор*"],
}


async def seed_default_brands(db: Database) -> None:
    if await db.brands_count():
        return
    for name, aliases in DEFAULT_BRANDS.items():
        bid = await db.add_brand(name)
        await db.set_aliases(bid, parse_aliases(",".join(aliases)))
