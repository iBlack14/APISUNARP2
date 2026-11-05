import psycopg2
import psycopg2.extras

conn = psycopg2.connect("postgresql://api:Devloper2@166.1.88.169:2233/api?sslmode=disable")
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute('SELECT * FROM public.reniec WHERE "DNI" = %s', ("00000001",))
r = cur.fetchone()
print({k: str(v)[:50] for k, v in dict(r).items()} if r else "no encontrado")
conn.close()
