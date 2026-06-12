import sqlite3
from collections import defaultdict

db_file = "uzum.db"

conn = sqlite3.connect(db_file)
cursor = conn.cursor()

# Извлекаем все позиции из barcodes
cursor.execute("""
    SELECT sku_id, sku_title, title, amount, price, status, order_uzum_id 
    FROM barcodes
""")
rows = cursor.fetchall()

if not rows:
    print("[ОШИБКА]: В таблице barcodes нет данных для вывода.")
    conn.close()
    exit(1)

products_summary = defaultdict(lambda: {"sku_title": "", "title": "", "count": 0, "price": 0})
status_summary = defaultdict(int)

print(f"=== СПИСОК ВСЕХ ТОВАРОВ ИЗ БАЗЫ ДАННЫХ (ВСЕГО СТРОК: {len(rows)}) ===\n")
print(f"{'SKU ID':<10} | {'АРТИКУЛ (SKU TITLE)':<40} | {'КОЛ-ВО':<6} | {'ЦЕНА':<10} | {'СТАТУС'}")
print("-" * 85)

total_amount = 0
total_sum = 0

for row in rows:
    sku_id, sku_title, title, amount, price, status, order_id = row
    
    # Агрегация для финального отчета
    products_summary[sku_id]["sku_title"] = sku_title
    products_summary[sku_id]["title"] = title
    products_summary[sku_id]["count"] += amount
    products_summary[sku_id]["price"] = price
    
    status_summary[status] += amount
    total_amount += amount
    total_sum += (price * amount)
    
    # Построчный вывод без сокращений
    short_sku_title = sku_title[:38] + ".." if len(sku_title) > 40 else sku_title
    print(f"{sku_id:<10} | {short_sku_title:<40} | {amount:<6} | {price:<10} | {status}")

print("\n" + "="*50)
print("=== СВОДНЫЙ ОТЧЕТ ПО УНИКАЛЬНЫМ ТОВАРАМ ===")
print("="*50)
for sku_id, info in products_summary.items():
    print(f"ID: {sku_id}")
    print(f"  Артикул: {info['sku_title']}")
    print(f"  Название: {info['title']}")
    print(f"  Итоговое кол-во в БД: {info['count']} шт.")
    print(f"  Цена за ед.: {info['price']} сум")
    print("-" * 40)

print("\n" + "="*50)
print("=== ИТОГОВАЯ СТАТИСТИКА БАЗЫ ===")
print("="*50)
print(f"Всего единиц товара в БД: {total_amount} шт.")
print(f"На общую сумму: {total_sum} сум")
print("\nРаспределение по статусам:")
for status, count in status_summary.items():
    print(f"  {status}: {count} шт.")

conn.close()