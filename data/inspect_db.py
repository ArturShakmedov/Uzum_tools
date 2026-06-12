import sqlite3
import json

conn = sqlite3.connect("uzum.db")
cursor = conn.cursor()

print("=== 1. СТРУКТУРА ТАБЛИЦЫ barcodes ===")
cursor.execute("PRAGMA table_info(barcodes);")
for col in cursor.fetchall():
    print(col)

print("\n=== 2. СКОЛЬКО ВСЕГО ЗАПИСЕЙ В ТАБЛИЦАХ ===")
cursor.execute("SELECT COUNT(*) FROM invoices;")
print(f"Накладных в БД (invoices): {cursor.fetchone()[0]}")
cursor.execute("SELECT COUNT(*) FROM barcodes;")
print(f"Строк со штрихкодами в БД (barcodes): {cursor.fetchone()[0]}")

print("\n=== 3. ПРИМЕРЫ ДАННЫХ ИЗ ТАБЛИЦЫ barcodes (Первые 5 строк) ===")
cursor.execute("SELECT * FROM barcodes LIMIT 5;")
rows = cursor.fetchall()
for row in rows:
    print(row)

print("\n=== 4. ПРОВЕРКА СУММЫ ПОЛЕЙ AMOUNT В БАЗЕ ===")
try:
    cursor.execute("SELECT SUM(amount) FROM barcodes;")
    print(f"Сумма всех amount в barcodes: {cursor.fetchone()[0]}")
except Exception as e:
    print(f"Ошибка при подсчете SUM(amount): {e}")

conn.close()