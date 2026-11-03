"""Определение бренда. Тексты взяты из реальных постов канала (скриншоты)."""
from brands import DEFAULT_BRANDS, BrandDef, detect_brand, parse_aliases

CHANEL_POST = """🅒 CHANEL 🅒

Шикарные комплект от Chanel в премиальном качестве ⚡
Состав / Латунь и смола и стеклянные бусы 🌸

Ожерелье 🏷 13.000₽
Чокер 🏷 9.500₽
Браслет 🏷 6.500₽
Пояс 🏷 12.500₽

По всем вопросам ⬇️
@bijuteria01"""

CHOPARD_POST = """CHOPARD

Поступление от CHOPARD в премиальном качестве с оригинальным 1:1

Состав:- V Gold с покрытием золото 750 пробы Камни кристалл Swarovski 💎 и природный малахит

Подвеска 12.000₽
Серёжки 12.500₽"""


def make_index() -> list[BrandDef]:
    return [BrandDef(i, n, parse_aliases(",".join(a))) for i, (n, a) in enumerate(DEFAULT_BRANDS.items(), start=1)]


def name_of(text, index=None):
    found = detect_brand(text, index or make_index())
    return found.name if found else None


def test_chanel_post():
    assert name_of(CHANEL_POST) == "Chanel"


def test_chopard_wins_over_swarovski_in_body():
    # Swarovski встречается в составе, но заголовок поста — CHOPARD
    assert name_of(CHOPARD_POST) == "Chopard"


def test_undetermined():
    assert name_of("Новое поступление! Колье и серьги, цена 5000₽") is None
    assert name_of("") is None


def test_cyrillic_alias_with_endings():
    assert name_of("Комплект в стиле шанели, премиум") == "Chanel"
    assert name_of("Новинка от Шанель") == "Chanel"


def test_word_boundaries():
    # «lv» — алиас Louis Vuitton, но не должен находиться внутри слов
    assert name_of("solvent and elvis") is None
    assert name_of("Сумка LV новая") == "Louis Vuitton"


def test_headline_priority_over_frequency():
    text = "CARTIER\nпохоже на dior, dior, dior"
    index = [BrandDef(1, "Cartier", []), BrandDef(2, "Dior", [])]
    assert name_of(text, index) == "Cartier"


def test_tie_resolved_by_first_position():
    index = [BrandDef(1, "Gucci", []), BrandDef(2, "Prada", [])]
    assert name_of("текст prada потом gucci", index) == "Prada"


def test_yo_and_case_insensitive():
    index = [BrandDef(1, "Ёлка Бренд", [])]
    assert name_of("новинка ЕЛКА БРЕНД", index) == "Ёлка Бренд"


def test_parse_aliases():
    assert parse_aliases("Chanel, шанел*;  coco chanel\nchanel,-") == ["chanel", "шанел*", "coco chanel"]
