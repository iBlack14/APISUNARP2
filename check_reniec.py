import os
import sys

import psycopg


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("Error: configura DATABASE_URL.", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(
            database_url, connect_timeout=10, sslmode="disable"
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = 'reniec'
                    ORDER BY ordinal_position
                    """
                )
                columns = cursor.fetchall()
    except psycopg.Error:
        print(
            "Error: no se pudo verificar la conexión o el esquema.",
            file=sys.stderr,
        )
        return 1

    if not columns:
        print("La tabla public.reniec no existe o no tiene columnas visibles.")
        return 1
    print("Conexión segura establecida. Columnas de public.reniec:")
    for column_name, data_type in columns:
        print(f"- {column_name}: {data_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
