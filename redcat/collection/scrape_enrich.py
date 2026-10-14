"""scrape_enrich — обогащение квартир данными ЖК."""
from __future__ import annotations



# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def _enrich_apartments_with_hc(normalized):
    """Дописывает apartments поля housing_complex_id, housing_complex_name
    и developer_name.

    /apartments/fast отдаёт estate_id (id корпуса). Зная estate_id,
    можно найти housing_complex_id в apartments_estates, а название ЖК и
    застройщика — в housing_complexes. Оба источника уже собраны в
    normalized. Застройщик справа нужен для coverage-расчёта (redcat.quality
    .coverage): фильтры cross_check вида «developer_name contains ФСК»
    работают по полям самой таблицы, без JOIN.
    """
    apts = normalized.get("apartments")
    estates = normalized.get("apartments_estates") or []
    hcs = normalized.get("housing_complexes") or []

    if not apts:
        return
    if not estates:
        print("  ⚠️ apartments: нет apartments_estates — "
              "housing_complex_id и housing_complex_name не будут заполнены.")
        return

    # {estate_id: housing_complex_id}
    est_to_hc = {}
    for e in estates:
        eid = e.get("estate_id")
        hcid = e.get("housing_complex_id")
        if eid is not None and hcid is not None:
            est_to_hc[eid] = hcid
            est_to_hc[str(eid)] = hcid

    # {housing_complex_id: housing_complex_name} / : developer_name
    hc_to_name = {}
    hc_to_dev = {}
    for hc in hcs:
        hcid = hc.get("id")
        name = hc.get("name")
        if hcid is not None:
            hc_to_name[hcid] = name
            hc_to_name[str(hcid)] = name
            hc_to_dev[hcid] = hc.get("developer_name")
            hc_to_dev[str(hcid)] = hc.get("developer_name")

    filled_id = 0
    filled_name = 0
    missing = 0
    for row in apts:
        eid = row.get("estate_id")
        if eid is None:
            missing += 1
            continue
        hcid = est_to_hc.get(eid)
        if hcid is None:
            missing += 1
            continue
        row["housing_complex_id"] = hcid
        filled_id += 1
        name = hc_to_name.get(hcid)
        if name:
            row["housing_complex_name"] = name
            filled_name += 1
        dev = hc_to_dev.get(hcid)
        if dev and not row.get("developer_name"):
            row["developer_name"] = dev

    print(f"  🔗 apartments: привязано к ЖК {filled_id} из {len(apts)} записей "
          f"(название ЖК: {filled_name}, без привязки: {missing})")
