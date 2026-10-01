@echo off
chcp 65001 >nul
setlocal

for %%J in (
  "Измайловский"
  "Химкинский"
  "Ясеневский"
  "Донской"
  "Ленинградский"
  "Лермонтовский"
  "Саларевский"
  "Шереметевский"
  "Южный"
  "Битца"
) do (
  echo.
  echo === ЖК: %%~J ===
  python crosscheck_ids.py --ext-table fsk_apartments ^
    --complex "%%~J" --complex-right "%%~J" ^
    --key "area_total=total_area,floor=floor,price=price" ^
    --filter-right "developer_name LIKE '%%ДСК%%'" ^
    --show-only-left 3 --show-only-right 3 --show-shared 2
)
endlocal