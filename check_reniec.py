import psycopg2

conn = psycopg2.connect("postgresql://api:Devloper2@166.1.88.169:2233/api?sslmode=disable")
cur = conn.cursor()
cur.execute("""
    SELECT column_name, data_type
    FROM information_schema.columns
    WHERE table_schema='public' AND table_name='reniec'
    ORDER BY ordinal_position
""")
for r in cur.fetchall():
    print(r)
cur.execute("SELECT count(*) FROM public.reniec")
print("total:", cur.fetchone()[0])
cur.execute("SELECT * FROM public.reniec LIMIT 1")
cols = [d[0] for d in cur.description]
row = cur.fetchone()
for c, v in zip(cols, row):
    print(f"{c} = {str(v)[:60]}")
conn.close()
