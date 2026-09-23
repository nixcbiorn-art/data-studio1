"""
Русские подписи для полей базы.
=================================
Сгенерировано, затем поправлено вручную.
Ключ — точное имя колонки в SQLite. Значение — как называть в интерфейсе.
Незнакомые поля показываются как есть — список можно пополнять постепенно.
"""

FIELD_LABELS: dict[str, str] = {

    # ── hc_apartment_stats: сводка по квартирам в ЖК ──
    "hc_id": "ID ЖК",
    "estate_id": "ID объекта",
    "dom_number": "Номер дома",
    "korpus_number": "Номер корпуса",
    "stroenie_number": "Номер строения",
    "total_apartments_estate": "Квартир в объекте",
    "type_id": "ID типа",
    "type_name": "Название типа",
    "object_type_id": "ID типа объекта",
    "total_apartments": "Всего квартир",
    "total_area_min": "Мин. площадь, м²",
    "total_area_max": "Макс. площадь, м²",
    "price_min": "Мин. цена, ₽",
    "price_max": "Макс. цена, ₽",
    "price_m2_min": "Мин. цена за м², ₽",
    "price_m2_max": "Макс. цена за м², ₽",

    # ── regulations: регламенты ──
    "id": "ID",
    "provider_id": "ID провайдера",
    "provider_name": "Провайдер",
    "housing_complex_id": "ID ЖК",
    "housing_complex_name": "Название ЖК",
    "date_from": "Действует с",
    "date_to": "Действует до",
    "status_name": "Статус",

    # ── tariffs: тарифные карты ──
    "uuid": "UUID",
    "provider.id": "ID провайдера",
    "provider.name": "Провайдер",
    "deal_type": "Тип сделки",
    "calculation_method": "Метод расчёта",
    "calculation_indicator": "Показатель расчёта",
    "calculation_month": "Расчётный месяц",
    "calculation_month.id": "ID расчётного месяца",
    "calculation_month.name": "Название расчётного месяца",
    "advance": "Аванс",
    "payment_term": "Срок выплаты, мес.",
    "status": "Статус",
    "comment": "Комментарий",
    "object_type.id": "ID типа объекта",
    "object_type.name": "Тип объекта",
    "payment_type": "Тип платежа",
    "korpus": "Корпус",
    "indicative_fee": "Ориентировочный сбор",
    "min_percent": "Мин. ставка, %",
    "max_percent": "Макс. ставка, %",
    "rules_accrual": "Правила начисления",
    "rules_accrual_show": "Показывать правила начисления",
    "reason_refusal": "Причина отказа",
    "reason_refusal_show": "Показывать причину отказа",
    "housing_complex.id": "ID ЖК",
    "housing_complex.name": "Название ЖК",
    "developer.id": "ID застройщика",
    "developer.name": "Застройщик",

    # ── housing_complexes: жилые комплексы ──
    "slug": "Слаг",
    "name": "Название",
    "address": "Адрес",
    "deadline": "Срок сдачи",
    "sales_start": "Начало продаж",
    "min_price_apartments": "Мин. цена квартиры, ₽",
    "min_price_apartments_formatted": "Мин. цена квартиры (строка), ₽",
    "percent_max": "Макс. процент, %",
    "price_delta_day": "Изменение цены за день, ₽",
    "max_rise": "Макс. рост",
    "max_discount": "Макс. скидка",
    "developer_name": "Застройщик",
    "decorations": "Отделка",
    "categories": "Категории",
    "hc_metros": "Метро рядом с ЖК",
    "railways": "Ж/д станции",
    "apartment_type_data": "Данные типа квартиры",

    # Фото ЖК (вложенный объект image.*)
    "image": "Фото",
    "image.id": "ID фото",
    "image.url": "URL фото",
    "image.name": "Название фото",
    "image.size": "Размер фото",
    "image.mime_type": "MIME-тип фото",
    "image.collection_name": "Коллекция фото",
    "image.generated_conversions.preview": "Фото: превью",
    "image.generated_conversions.large": "Фото: большое",
    "image.generated_conversions.thumb": "Фото: миниатюра",
    "image.generated_conversions.preview_home": "Фото: превью на главной",
    "image.links.preview": "Фото: ссылка превью",
    "image.links.large": "Фото: ссылка большое",
    "image.links.thumb": "Фото: ссылка миниатюра",
    "image.links.preview_home": "Фото: ссылка превью на главной",
    "image.custom_properties.is_virtual": "Фото: виртуальное",
    "image.conversions.original": "Фото: оригинал",
    "image.conversions.preview": "Фото: превью",
    "image.conversions.large": "Фото: большое",
    "image.conversions.thumb": "Фото: миниатюра",

    # Рост цены (вложенный объект max_rise.*)
    "max_rise.current_price": "Рост: текущая цена, ₽",
    "max_rise.current_price_formatted": "Рост: текущая цена (строка), ₽",
    "max_rise.rise_rub": "Рост: прирост, ₽",
    "max_rise.rise_rub_formatted": "Рост: прирост (строка), ₽",
    "max_rise.price_change_percent": "Рост: изменение цены, %",
    "max_rise.price_change_percent_formatted": "Рост: изменение цены (строка), %",

    # Скидка (вложенный объект max_discount.*)
    "max_discount.current_price": "Скидка: текущая цена, ₽",
    "max_discount.current_price_formatted": "Скидка: текущая цена (строка), ₽",
    "max_discount.benefit_rub": "Скидка: выгода, ₽",
    "max_discount.benefit_rub_formatted": "Скидка: выгода (строка), ₽",
    "max_discount.price_change_percent": "Скидка: изменение цены, %",
    "max_discount.price_change_percent_formatted": "Скидка: изменение цены (строка), %",

    # ── apartments: квартиры и лоты ──
    "apartment_type.name": "Тип квартиры",
    "estate_delivery_date": "Срок сдачи",
    "housing_complex_deadline": "Срок сдачи ЖК",
    "floor": "Этаж",
    "total_floor": "Этажей в доме",
    "number": "Номер",
    "total_area": "Площадь общая, м²",
    "land_area": "Площадь участка, м²",
    "base_price": "Базовая цена, ₽",
    "base_price_formatted": "Базовая цена (строка), ₽",
    "price": "Цена, ₽",
    "price_formatted": "Цена (строка), ₽",
    "price_per_sqm": "Цена за м², ₽",
    "decoration_type": "Тип отделки",
    "decoration_type.name": "Тип отделки",
    "lot_price_dynamics": "Динамика цены",
    "housing_complex.url_slug": "URL-слаг ЖК",
    "housing_complex.slug": "Слаг ЖК",
    "housing_complex.uuid": "UUID ЖК",
    "flat_plan_image": "Планировка",

    # Планировка (вложенный объект flat_plan_image.*)
    "flat_plan_image.generated_conversions.preview": "Планировка: превью",
    "flat_plan_image.generated_conversions.large": "Планировка: большое",
    "flat_plan_image.generated_conversions.thumb": "Планировка: миниатюра",
    "flat_plan_image.collection_name": "Планировка: коллекция",
    "flat_plan_image.links.preview": "Планировка: ссылка превью",
    "flat_plan_image.links.large": "Планировка: ссылка большое",
    "flat_plan_image.links.thumb": "Планировка: ссылка миниатюра",
    "flat_plan_image.mime_type": "Планировка: MIME-тип",

    # ── comparison_vs_previous: изменения между запусками ──
    "Категория": "Тип изменения",
    "ID": "ID",
    "Название": "Название",
    "Поле": "Поле",
    "Было": "Было",
    "Стало": "Стало",
    "Информация": "Информация",

    # ── служебные ──
    "run_id": "Номер запуска",
}


def label_for(field: str) -> str:
    """Русская подпись для поля или исходное имя, если перевода нет."""
    return FIELD_LABELS.get(field, field)


def apply_to_columns(columns: list) -> list:
    """Добавляет каждому элементу columns ключ label."""
    for c in columns:
        c["label"] = label_for(c["name"])
    return columns