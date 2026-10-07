"""
Единый нормализатор названий ЖК.
================================
Один источник правды для всех скриптов (webapp, hc_aliases, fill_aliases,
match_complex_names, hc_crosscheck). Уровни сравнения — от строгого к мягкому:

  normalize(raw)   строгий ключ: «ЖК «Скай Гарден», корп. 3» → «скай гарден»
  compact          то же без пробелов: «ново переделкино» = «новопеределкино»
  phon_key(raw)    фонетический ключ (кириллица → латиница + сворачивание):
                   «Скай Гарден» = «Sky Garden», «Прайм» = «Prime»
  skeleton         только согласные — слабый признак, лишь для «кандидатов»

  canon(raw, aliases)          normalize + словарь синонимов (с цепочками)
  similarity(a, b)             (оценка 0..1, причина)
  auto_aliases(left, right)    автосопоставление двух списков названий

Проверка вручную:
    python -m redcat.sources.name_normalizer "ЖК «Скай Гарден», корпус 3" "Sky Garden"
"""
from __future__ import annotations

import html
import re
import sys
import unicodedata
from difflib import SequenceMatcher

# ──────────────────────────────────────────────────────────────
#  Шаги строгой нормализации
# ──────────────────────────────────────────────────────────────
_A, _Z = r"(?<![a-zа-я0-9])", r"(?![a-zа-я0-9])"

# Слова-типы объекта. Убираем в ЛЮБОМ месте строки, а не только в начале.
# «дом», «квартал», «клубный дом» намеренно НЕ входят: это часть названий.
_TYPE_RE = re.compile(
    _A + r"(?:жилой\s+(?:комплекс|квартал|район|массив|городок|микрорайон)"
    r"|клубный\s+(?:дом|квартал|пос[её]лок)|апарт[\s-]?комплекс|коттеджный\s+пос[её]лок|residential\s+complex"
    r"|жк|жр|мкр|микрорайон|кп)" + _Z)

# «дом «Обручева 30»», «клубный дом «The Lake»» — тип перед кавычкой не часть имени,
# а «Дом на Мосфильмовской» остаётся как есть.
_QTYPE_RE = re.compile(
    r"(?:(?:клубный|жилой|многоквартирный|элитный|апарт)\s+)?"
    r"(?:дом|квартал|комплекс|пос[её]лок|район|городок|отель|резиденция|residence)"
    r"\s*[«“„\"']")

_NUM = r"\d+[а-яa-z]?(?:[-/]\d+[а-яa-z]?)*"
_UNITS_FWD = (r"корпус[аи]?|корп|кор|к|строение|стр|литер[аы]?|лит|секция|секц|"
              r"очередь|оч|этап|фаза|позиция|поз|участок|уч|блок|башня")
_UNITS_BACK = (r"очередь|очереди|этап|фаза|секция|корпус|позиция|участок|блок|"
               r"башня|строение|литер|оч|поз|стр|корп")
_ORD_SUF = r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ых|й|я|е)"

# «корпус 3», «к.2», «стр. 1», «литер А1», «очередь II», «№»
_UNIT_FWD_RE = re.compile(
    _A + rf"(?:{_UNITS_FWD})\.?\s*[№#]?\s*{_NUM}" + _Z)
# «2 очередь», «2-я очередь», «1 корпус»
_UNIT_BACK_RE = re.compile(
    _A + rf"\d+(?:\s*[-–—]?\s*{_ORD_SUF})?\s*[-–—]?\s*(?:{_UNITS_BACK})\.?" + _Z)

_ORD_DIGIT_RE = re.compile(_A + rf"(\d+)(?:\s*[-–—]\s*|){_ORD_SUF}" + _Z)
_ORD_LAT_RE = re.compile(_A + r"(\d+)(?:st|nd|rd|th)" + _Z)
_ORD_WORD_RE = re.compile(
    _A + r"(перв|втор|трет|четверт|пят|шест|седьм|восьм|девят|десят)"
    r"(?:ый|ий|ой|ая|яя|ое|ее|ые|ие|ье|ья|ьи)" + _Z)
_ORD_STEM = {"перв": "1", "втор": "2", "трет": "3", "четверт": "4", "пят": "5",
             "шест": "6", "седьм": "7", "восьм": "8", "девят": "9", "десят": "10"}

# римские — только заглавные и только однозначные (иначе «MIX», «CIVIC»)
_ROMAN_RE = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9])(VIII|VII|III|II|IV|VI|IX)(?![A-Za-zА-Яа-яЁё0-9])")
_ROMAN = {"II": "2", "III": "3", "IV": "4", "VI": "6", "VII": "7", "VIII": "8", "IX": "9"}

_BRACKETS_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]|\{[^}]*\}")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
_LEAD_THE_RE = re.compile(r"^the\s+")

# Визуально одинаковые буквы: латиница ↔ кириллица
_LAT2CYR = dict(zip("acepxykmtbh", "асерхукмтвн"))
_CYR2LAT_HG = {v: k for k, v in _LAT2CYR.items()}


def _fix_homoglyphs(s: str) -> str:
    """«Сkай» (латинская k внутри кириллического слова) → «Скай»."""
    def fix(m: re.Match) -> str:
        w = m.group(0)
        cyr = sum("а" <= c <= "я" or c == "ё" for c in w)
        lat = sum("a" <= c <= "z" for c in w)
        if not cyr or not lat or cyr == lat:
            return w
        table = _LAT2CYR if cyr > lat else _CYR2LAT_HG
        return "".join(table.get(c, c) for c in w)
    return _WORD_RE.sub(fix, s)


def _strip_brackets(s: str) -> str:
    """Убирает (…) […] {…}; если внутри было всё название — берёт его."""
    out = _BRACKETS_RE.sub(" ", s)
    if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", out):
        inner = re.findall(r"[\(\[\{]([^)\]\}]*)[\)\]\}]", s)
        return " ".join(inner) or s
    return out


def normalize(raw) -> str | None:
    """Строгий ключ названия ЖК. None, если после чистки ничего не осталось.

    «ЖК «Скай Гарден», корпус 3»   → «скай гарден»
    «Жилой комплекс Sky  Garden (Москва)» → «sky garden»
    «Первый Измайловский»          → «1 измайловский»
    «Амбер Сити, II очередь»       → «амбер сити»
    """
    if raw is None:
        return None
    s = html.unescape(str(raw))
    s = unicodedata.normalize("NFKC", s)          # nbsp, «ｓ», лигатуры
    s = _ROMAN_RE.sub(lambda m: _ROMAN[m.group(1)], s)   # до lower()!
    s = _strip_brackets(s)
    s = _fix_homoglyphs(s.lower())
    s = s.replace("ё", "е").replace("ъ", "").replace("&", " ")
    s = _QTYPE_RE.sub(" ", s)
    s = _TYPE_RE.sub(" ", s)
    s = _UNIT_BACK_RE.sub(" ", s)
    s = _UNIT_FWD_RE.sub(" ", s)
    s = _ORD_DIGIT_RE.sub(r"\1", s)
    s = _ORD_LAT_RE.sub(r"\1", s)
    s = _ORD_WORD_RE.sub(lambda m: _ORD_STEM[m.group(1)], s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s).strip()
    s = _LEAD_THE_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip() or None


def compact(raw) -> str | None:
    """normalize без пробелов: «Ново-Переделкино» = «Новопеределкино»."""
    n = normalize(raw)
    return n.replace(" ", "") if n else None


# ──────────────────────────────────────────────────────────────
#  Фонетика: кириллица → латиница → сворачивание
# ──────────────────────────────────────────────────────────────
_CYR2LAT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "s", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def translit(s: str) -> str:
    return s.translate(_CYR2LAT)


def _fold_latin(t: str) -> str:
    """Свёртка латиницы к «как слышится по-русски»: sky ≈ skay, prime ≈ praim."""
    t = t.replace("ch", "§")
    for a, b in (("ph", "f"), ("ck", "k"), ("kh", "h"), ("qu", "kv"),
                 ("q", "k"), ("x", "ks"), ("w", "v")):
        t = t.replace(a, b)
    t = re.sub(r"c(?=[eiy])", "s", t).replace("c", "k").replace("§", "ch")
    t = re.sub(r"(?<![aeiouy])([aiou])([^aeiouy\s0-9])e(?=\s|$)",
               lambda m: {"a": "ei", "i": "ai", "o": "ou", "u": "yu"}[m.group(1)] + m.group(2), t)
    t = t.replace("y", "i").replace("ai", "i")
    t = re.sub(r"(?<=[^aeiou\s0-9])e(?=\s|$)", "", t)     # немое e: prime → prim
    return re.sub(r"([a-z])\1+", r"\1", t)                # rotterdam → roterdam


def latin_form(raw) -> str | None:
    """Фонетическая форма со словами: «скай гарден» → «ski garden»."""
    n = normalize(raw)
    return _fold_latin(translit(n)) if n else None


def phon_key(raw) -> str | None:
    f = latin_form(raw)
    return f.replace(" ", "") if f else None


def skeleton(raw) -> str | None:
    """Только согласные и цифры. Слабый признак — только для подсказок."""
    k = phon_key(raw)
    if not k:
        return None
    return re.sub(r"([a-z])\1+", r"\1", re.sub(r"[aeiou]", "", k))


# ──────────────────────────────────────────────────────────────
#  Синонимы
# ──────────────────────────────────────────────────────────────
def canon(raw, aliases: dict | None = None) -> str | None:
    """normalize + подстановка канона. Цепочки a→b→c проходятся до конца."""
    n = normalize(raw)
    if n is None or not aliases:
        return n
    seen = {n}
    for _ in range(8):
        nxt = aliases.get(n)
        if not nxt or nxt in seen:
            break
        seen.add(nxt)
        n = nxt
    return n


# ──────────────────────────────────────────────────────────────
#  Схожесть и автосопоставление
# ──────────────────────────────────────────────────────────────
class Name:
    """Предвычисленные формы названия — чтобы не считать их в цикле N×M."""
    __slots__ = ("raw", "norm", "compact", "lat", "phon", "skel", "digits", "sorted")

    def __init__(self, raw):
        self.raw = raw
        self.norm = normalize(raw) or ""
        self.compact = self.norm.replace(" ", "")
        self.lat = _fold_latin(translit(self.norm))
        self.phon = self.lat.replace(" ", "")
        self.skel = re.sub(r"([a-z])\1+", r"\1", re.sub(r"[aeiou]", "", self.phon))
        self.digits = tuple(re.findall(r"\d+", self.norm))
        self.sorted = " ".join(sorted(self.lat.split()))


def _pair(a: Name, b: Name) -> tuple[float, str]:
    if not a.norm or not b.norm:
        return 0.0, "empty"
    if a.norm == b.norm:
        return 1.0, "exact"
    if a.digits != b.digits:                 # «Скай 1» ≠ «Скай 2»
        return 0.0, "digits"
    if a.compact == b.compact:
        return 0.99, "compact"
    if a.phon == b.phon and len(a.phon) >= 3:
        return 0.97, "phonetic"
    ta, tb = a.lat.split(), b.lat.split()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if (len(short) < len(long_) and long_[:len(short)] == short
            and sum(map(len, short)) >= 4):
        # Защита от опасных склеек: если «Спутник» содержится
        # в «Скай Спутник», это НЕ то же самое. Требуем, чтобы
        # разница в длине была небольшой (≤ 2 символа) — тогда
        # это, скорее всего, «ЖК» или «корпус». Иначе — отказ.
        diff_len = len(long_) - len(short)
        if diff_len <= 2:
            return 0.86, "contains"
        # Разница большая — игнорируем эту пару для auto-match.
        # Через hc_aliases.json её всё равно можно задать вручную.
        return 0.5, "contains-too-far"
    score = max(SequenceMatcher(None, a.lat, b.lat).ratio(),
                SequenceMatcher(None, a.sorted, b.sorted).ratio())
    if a.skel == b.skel and len(a.skel) >= 3:
        return max(score, 0.85), "skeleton"   # только подсказка
    return score, "fuzzy"


def similarity(a, b) -> tuple[float, str]:
    """Схожесть двух названий: (0..1, причина)."""
    return _pair(Name(a), Name(b))


def auto_aliases(left_names, right_names, *, min_score: float = 0.9,
                 existing: dict | None = None, margin: float = 0.05,
                 left_counts: dict | None = None, right_counts: dict | None = None,
                 count_tol: float = 0.15) -> dict:
    """Автосопоставление названий из двух источников.

    Возвращает {"aliases": {ключ_слева: ключ_справа},
                "suggest": [(слева, справа, score, reason)],
                "ambiguous": [(слева, [справа…])]}.

    Сильные совпадения (exact/compact/phonetic/fuzzy ≥ min_score) принимаются,
    если пара однозначна: лучший кандидат заметно лучше второго и выбор взаимный.
    Слабые (вложенность «Хольм» ⊂ «Хольм.Эйк», skeleton, fuzzy ≥ 0.75) —
    только если известны счётчики строк (left_counts/right_counts) и они
    отличаются не более чем на count_tol; пара при этом единственная с обеих
    сторон. Пара с похожим названием (≥ 0.4) и счётчиками в пределах 5% тоже
    принимается — так находятся «ВЕЙВ» ↔ «Wave»-подобные случаи.
    """
    existing = existing or {}
    L, R = {}, {}
    for n in left_names:
        x = Name(n)
        if x.norm:
            L.setdefault(x.norm, x)
    for n in right_names:
        x = Name(n)
        if x.norm:
            R.setdefault(x.norm, x)
    only_l = {k: v for k, v in L.items() if k not in R and k not in existing}
    only_r = {k: v for k, v in R.items() if k not in L}

    def cnt(counts, x: Name):
        if not counts:
            return None
        return counts.get(x.raw, counts.get(x.norm))

    def ratio(lx: Name, rx: Name) -> float | None:
        cl, cr = cnt(left_counts, lx), cnt(right_counts, rx)
        if not cl or not cr:
            return None
        return min(cl, cr) / max(cl, cr)

    def ranked(x: Name, pool: dict) -> list:
        res = []
        for k, y in pool.items():
            if (abs(len(x.lat) - len(y.lat)) > max(len(x.lat), len(y.lat)) * 0.5
                    and x.phon != y.phon and not y.lat.startswith(x.lat)
                    and not x.lat.startswith(y.lat)):
                continue
            s, why = _pair(x, y)
            if s >= 0.4:
                res.append((s, k, why))
        return sorted(res, reverse=True)

    fwd = {k: ranked(x, only_r) for k, x in only_l.items()}
    back = {k: ranked(y, only_l) for k, y in only_r.items()}

    def strong_ok(cands, lk_or_rk_back=False):
        s1 = cands[0][0]
        s2 = cands[1][0] if len(cands) > 1 else 0.0
        return s1 >= min_score and s1 - s2 >= margin

    out: dict[str, str] = {}
    suggest, ambiguous = [], []
    for lk, cands in fwd.items():
        if not cands:
            continue
        lx = only_l[lk]
        s1, rk, why = cands[0]
        mutual = bool(back[rk]) and back[rk][0][1] == lk and (
            len(back[rk]) < 2 or back[rk][0][0] - back[rk][1][0] >= margin)
        if strong_ok(cands) and mutual:
            out[lk] = rk
            continue
        if s1 >= min_score:                      # похожи, но неоднозначно
            ambiguous.append((lx.raw, [only_r[c[1]].raw for c in cands[:3]]))
            continue

        # слабые совпадения: подтверждаем счётчиками строк
        def near(x, pool_cands, back_side):
            res = []
            for s, k, w in pool_cands:
                other = (only_r if not back_side else only_l)[k]
                r = ratio(x, other) if not back_side else ratio(other, x)
                if r is None:
                    continue
                # Мягкие совпадения только при очень похожих счётчиках
                # и заметной схожести названий. Раньше стояло 0.4 — из-за
                # этого «Тропарево Парк» (286) и «Котельники парк» (301)
                # сливались в один ЖК, хотя это разные места в Москве.
                if ((s >= 0.85 and r >= 1 - count_tol)
                        or (s >= 0.82 and r >= 0.98)):
                    res.append((s, k, w, r))
            return res

        mine = near(lx, cands, False)
        if len(mine) == 1:
            s, k, w, r = mine[0]
            theirs = near(only_r[k], back[k], True)
            if len(theirs) == 1 and theirs[0][1] == lk:
                out[lk] = k
                continue
        if s1 >= 0.75:
            suggest.append((lx.raw, only_r[rk].raw, round(s1, 2), why))
    suggest.sort(key=lambda t: -t[2])
    return {"aliases": out, "suggest": suggest, "ambiguous": ambiguous}


# ──────────────────────────────────────────────────────────────
def explain(raw) -> dict:
    n = Name(raw)
    return {"raw": raw, "normalize": n.norm or None, "compact": n.compact or None,
            "phon_key": n.phon or None, "skeleton": n.skel or None}


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    for a in args:
        print(explain(a))
    if len(args) == 2:
        print("similarity:", similarity(*args))
