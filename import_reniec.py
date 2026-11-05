from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import BinaryIO

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parent
DEFAULT_FILE = ROOT / "reniec" / "reniec.txt"


class DecodedLineIterator:
    def __init__(self, source: BinaryIO, strip_bom: bool) -> None:
        self.source = source
        self.bytes_read = source.tell()
        self.first_line_encoding = "utf-8-sig" if strip_bom else "utf-8"

    def __iter__(self) -> DecodedLineIterator:
        return self

    def __next__(self) -> str:
        raw_line = self.source.readline()
        if not raw_line:
            raise StopIteration
        self.bytes_read = self.source.tell()
        encoding = self.first_line_encoding
        self.first_line_encoding = "utf-8"
        return raw_line.decode(encoding)


def load_env_file() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def get_connection() -> psycopg.Connection:
    load_env_file()
    conninfo = os.environ.get("DATABASE_URL", "").strip()
    if not conninfo:
        raise ValueError("Configura DATABASE_URL en el archivo .env antes de continuar.")
    return psycopg.connect(conninfo, sslmode="disable")


def read_header(file_path: Path) -> list[str]:
    with file_path.open("r", encoding="utf-8-sig", newline="") as source:
        try:
            header = next(csv.reader(source, delimiter="|"))
        except StopIteration as exc:
            raise ValueError(f"El archivo está vacío: {file_path}") from exc
    if not header or any(not name.strip() for name in header):
        raise ValueError("La cabecera tiene nombres vacíos.")
    normalized = [name.strip().casefold() for name in header]
    if len(set(normalized)) != len(normalized):
        raise ValueError("La cabecera contiene nombres de columna duplicados.")
    return [name.strip() for name in header]


def inspect_database(conn: psycopg.Connection, file_header: list[str]) -> None:
    query = """
        SELECT
            n.nspname,
            c.relname,
            (
                SELECT array_agg(a2.attname ORDER BY a2.attnum)
                FROM pg_attribute a2
                WHERE a2.attrelid = c.oid
                  AND a2.attnum > 0
                  AND NOT a2.attisdropped
            ) AS columns,
            bool_or(lower(a.attname) = 'dni') AS has_dni,
            EXISTS (
                SELECT 1
                FROM pg_index i
                JOIN pg_attribute dni
                  ON dni.attrelid = c.oid
                 AND dni.attnum > 0
                 AND NOT dni.attisdropped
                 AND lower(dni.attname) = 'dni'
                WHERE i.indrelid = c.oid
                  AND i.indisunique
                  AND i.indisvalid
                  AND i.indpred IS NULL
                  AND i.indnkeyatts = 1
                  AND i.indkey[0] = dni.attnum
            ) AS dni_unique
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE c.relkind IN ('r', 'p')
          AND a.attnum > 0
          AND NOT a.attisdropped
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        GROUP BY n.nspname, c.relname, c.oid
        ORDER BY n.nspname, c.relname
    """
    with conn.cursor() as cursor:
        cursor.execute(query)
        tables = cursor.fetchall()

    incoming = {name.casefold() for name in file_header}
    candidates = []
    for schema_name, table_name, columns, has_dni, dni_unique in tables:
        shared = [name for name in columns if name.casefold() in incoming]
        if shared:
            candidates.append(
                (schema_name, table_name, columns, has_dni, dni_unique, shared)
            )

    if not candidates:
        print(
            "No se encontraron tablas de usuario con columnas que coincidan "
            "con las cabeceras del archivo."
        )
        return

    print("Tablas con coincidencias de cabecera (solo se consultó el esquema):")
    for schema_name, table_name, columns, has_dni, dni_unique, shared in candidates:
        existing = {name.casefold() for name in columns}
        missing = [name for name in file_header if name.casefold() not in existing]
        compatible = "sí" if not missing else f"no (faltan: {', '.join(missing)})"
        unique_label = "sí" if dni_unique else "no"
        print(
            f"- {schema_name}.{table_name}: "
            f"coinciden={', '.join(shared)}; DNI={('sí' if has_dni else 'no')}; "
            f"tiene todas las cabeceras={compatible}; DNI único={unique_label}"
        )
    print("No se mostró ningún valor de las filas.")


def get_table_columns(
    conn: psycopg.Connection, schema_name: str, table_name: str
) -> list[tuple[str, str, bool, bool, bool]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.oid
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
            """,
            (schema_name, table_name),
        )
        relation = cursor.fetchone()
        if relation is None:
            raise ValueError(f"No existe la tabla PostgreSQL {schema_name}.{table_name}.")

        cursor.execute(
            """
            SELECT
                a.attname,
                pg_catalog.format_type(a.atttypid, a.atttypmod),
                a.attnotnull,
                a.attgenerated <> '',
                d.adbin IS NOT NULL
            FROM pg_attribute a
            LEFT JOIN pg_attrdef d
              ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (relation[0],),
        )
        return cursor.fetchall()


def ensure_destination_table(
    conn: psycopg.Connection, table: str, file_header: list[str]
) -> None:
    schema_name, table_name = split_table_name(table)
    if "dni" not in {name.casefold() for name in file_header}:
        raise ValueError("El archivo no contiene una columna DNI.")

    definitions = []
    for column in file_header:
        definition = sql.SQL("{} TEXT{}").format(
            sql.Identifier(column),
            sql.SQL(" PRIMARY KEY") if column.casefold() == "dni" else sql.SQL(""),
        )
        definitions.append(definition)

    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                sql.Identifier(schema_name)
            )
        )
        cursor.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                sql.Identifier(schema_name, table_name),
                sql.SQL(", ").join(definitions),
            )
        )
    print(f"Tabla lista: {schema_name}.{table_name} (DNI único; columnas TEXT).")


def has_unique_dni(
    conn: psycopg.Connection, schema_name: str, table_name: str, dni_column: str
) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_index i
                JOIN pg_class c ON c.oid = i.indrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a
                  ON a.attrelid = c.oid
                 AND a.attnum = i.indkey[0]
                WHERE n.nspname = %s
                  AND c.relname = %s
                  AND i.indisunique
                  AND i.indisvalid
                  AND i.indpred IS NULL
                  AND i.indnkeyatts = 1
                  AND lower(a.attname) = lower(%s)
            )
            """,
            (schema_name, table_name, dni_column),
        )
        return cursor.fetchone()[0]


def split_table_name(value: str) -> tuple[str, str]:
    parts = value.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError("Indica la tabla como esquema.tabla, por ejemplo public.personas.")
    return parts[0], parts[1]


def normalize_row(
    row: list[str], expected_fields: int, address_index: int, line_number: int
) -> tuple[list[str], bool]:
    if len(row) == expected_fields:
        return row, False
    if len(row) < expected_fields:
        raise ValueError(
            f"La fila {line_number} tiene {len(row)} campos; "
            f"se esperaban {expected_fields}."
        )

    trailing_fields = expected_fields - address_index - 1
    address_fragments_end = len(row) - trailing_fields
    if address_fragments_end <= address_index:
        raise ValueError(f"No se pudo interpretar la fila {line_number}.")
    normalized = (
        row[:address_index]
        + ["|".join(row[address_index:address_fragments_end])]
        + row[-trailing_fields:]
    )
    if len(normalized) != expected_fields:
        raise ValueError(f"No se pudo interpretar la fila {line_number}.")
    return normalized, True


def prepare_checkpoint(
    conn: psycopg.Connection,
    schema_name: str,
    table_name: str,
    file_path: Path,
    file_size: int,
    file_mtime_ns: int,
) -> tuple[sql.Identifier, str, int, int, int, int, int]:
    checkpoint_table = sql.Identifier(schema_name, "_reniec_import_progress")
    source_path = str(file_path.resolve())
    with conn.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """
                CREATE TABLE IF NOT EXISTS {} (
                    target_table TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_size BIGINT NOT NULL,
                    source_mtime_ns BIGINT NOT NULL,
                    byte_offset BIGINT NOT NULL DEFAULT 0,
                    rows_processed BIGINT NOT NULL DEFAULT 0,
                    rows_inserted BIGINT NOT NULL DEFAULT 0,
                    rows_repaired BIGINT NOT NULL DEFAULT 0,
                    rows_missing_dni BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (target_table, source_path)
                )
                """
            ).format(checkpoint_table)
        )
        cursor.execute(
            sql.SQL(
                """
                INSERT INTO {} (
                    target_table, source_path, source_size, source_mtime_ns
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (target_table, source_path) DO NOTHING
                """
            ).format(checkpoint_table),
            (table_name, source_path, file_size, file_mtime_ns),
        )
        cursor.execute(
            sql.SQL(
                """
                SELECT source_size, source_mtime_ns, byte_offset,
                       rows_processed, rows_inserted, rows_repaired, rows_missing_dni
                FROM {}
                WHERE target_table = %s AND source_path = %s
                """
            ).format(checkpoint_table),
            (table_name, source_path),
        )
        state = cursor.fetchone()
        if state is None:
            raise RuntimeError("No se pudo crear el punto de reanudación.")

        old_size, old_mtime, offset, processed, inserted, repaired, missing_dni = state
        if old_size != file_size or old_mtime != file_mtime_ns:
            offset = processed = inserted = repaired = missing_dni = 0
            cursor.execute(
                sql.SQL(
                    """
                    UPDATE {}
                    SET source_size = %s, source_mtime_ns = %s, byte_offset = 0,
                        rows_processed = 0, rows_inserted = 0,
                        rows_repaired = 0, rows_missing_dni = 0
                    WHERE target_table = %s AND source_path = %s
                    """
                ).format(checkpoint_table),
                (file_size, file_mtime_ns, table_name, source_path),
            )
        elif offset < 0 or offset > file_size:
            raise ValueError("El punto de reanudación guardado no es válido.")

    conn.commit()
    return (
        checkpoint_table,
        source_path,
        offset,
        processed,
        inserted,
        repaired,
        missing_dni,
    )


def save_checkpoint(
    cursor: psycopg.Cursor,
    checkpoint_table: sql.Identifier,
    table_name: str,
    source_path: str,
    file_size: int,
    file_mtime_ns: int,
    byte_offset: int,
    rows_processed: int,
    rows_inserted: int,
    rows_repaired: int,
    rows_missing_dni: int,
) -> None:
    cursor.execute(
        sql.SQL(
            """
            UPDATE {}
            SET byte_offset = %s,
                rows_processed = rows_processed + %s,
                rows_inserted = rows_inserted + %s,
                rows_repaired = rows_repaired + %s,
                rows_missing_dni = rows_missing_dni + %s
            WHERE target_table = %s
              AND source_path = %s
              AND source_size = %s
              AND source_mtime_ns = %s
            """
        ).format(checkpoint_table),
        (
            byte_offset,
            rows_processed,
            rows_inserted,
            rows_repaired,
            rows_missing_dni,
            table_name,
            source_path,
            file_size,
            file_mtime_ns,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("No se pudo guardar el punto de reanudación.")


def import_file(
    conn: psycopg.Connection,
    file_path: Path,
    table: str,
    batch_size: int,
    file_header: list[str],
) -> None:
    schema_name, table_name = split_table_name(table)
    target_columns = get_table_columns(conn, schema_name, table_name)
    target_by_name = {column[0].casefold(): column for column in target_columns}
    source_names = [name.casefold() for name in file_header]

    missing = [name for name in file_header if name.casefold() not in target_by_name]
    if missing:
        raise ValueError(
            f"La tabla destino no tiene estas columnas del archivo: {', '.join(missing)}"
        )

    insert_columns = [target_by_name[name] for name in source_names]
    dni_source_index = next(
        (index for index, name in enumerate(source_names) if name == "dni"), None
    )
    if dni_source_index is None:
        raise ValueError("El archivo no contiene una columna DNI.")
    address_source_index = next(
        (index for index, name in enumerate(source_names) if name == "direccion"), None
    )
    if address_source_index is None or address_source_index < dni_source_index:
        raise ValueError("No se encontró DIRECCION después de DNI en la cabecera.")
    dni_target = insert_columns[dni_source_index]
    if dni_target[3]:
        raise ValueError("La columna DNI de destino es generada y no se puede importar.")
    if not has_unique_dni(conn, schema_name, table_name, dni_target[0]):
        raise ValueError(
            f"La columna {dni_target[0]} de {schema_name}.{table_name} no tiene "
            "una restricción única. No se puede garantizar que la carga omita "
            "todos los DNI ya existentes."
        )

    supplied_columns = {column[0] for column in insert_columns}
    required_extras = [
        column[0]
        for column in target_columns
        if column[2]
        and not column[3]
        and not column[4]
        and column[0] not in supplied_columns
    ]
    if required_extras:
        raise ValueError(
            "La tabla requiere otras columnas sin valor predeterminado: "
            + ", ".join(required_extras)
        )

    conn.commit()
    file_stat = file_path.stat()
    file_size = file_stat.st_size
    table_key = f"{schema_name}.{table_name}"
    (
        checkpoint_table,
        source_path,
        byte_offset,
        total_processed,
        total_inserted,
        total_repaired,
        total_missing_dni,
    ) = prepare_checkpoint(
        conn,
        schema_name,
        table_key,
        file_path,
        file_size,
        file_stat.st_mtime_ns,
    )

    stage_name = "reniec_import_stage"
    stage_columns = [f"c{index}" for index in range(len(file_header))]
    stage_definitions = sql.SQL(", ").join(
        sql.SQL("{} {}").format(
            sql.Identifier(stage_column),
            sql.SQL(target_column[1]),
        )
        for stage_column, target_column in zip(stage_columns, insert_columns)
    )
    target_identifier = sql.Identifier(schema_name, table_name)
    stage_identifier = sql.Identifier(stage_name)
    stage_column_identifiers = [sql.Identifier(name) for name in stage_columns]
    dni_stage = stage_column_identifiers[dni_source_index]
    target_identifier_columns = [sql.Identifier(column[0]) for column in insert_columns]

    inserted = 0
    seen = 0
    repaired = 0
    missing_dni = 0
    last_progress = -1
    print(
        f"Reanudando desde {byte_offset * 100 / file_size:.2f}% "
        f"({total_processed:,} filas procesadas previamente)."
    )
    with file_path.open("rb") as source:
        source.seek(byte_offset)
        lines = DecodedLineIterator(source, strip_bom=byte_offset == 0)
        reader = csv.reader(lines, delimiter="|")
        if byte_offset == 0:
            actual_header = next(reader, None)
            if actual_header is None or [
                name.strip().casefold() for name in actual_header
            ] != source_names:
                raise ValueError("La cabecera del archivo cambió durante la ejecución.")
            byte_offset = lines.bytes_read
            with conn.transaction():
                with conn.cursor() as cursor:
                    save_checkpoint(
                        cursor,
                        checkpoint_table,
                        table_key,
                        source_path,
                        file_size,
                        file_stat.st_mtime_ns,
                        byte_offset,
                        0,
                        0,
                        0,
                        0,
                    )

        batch: list[list[str]] = []
        batch_repaired = 0
        batch_missing_dni = 0
        with conn.cursor() as cursor:
            for line_number, row in enumerate(reader, start=2):
                if not row or not row[dni_source_index].strip():
                    missing_dni += 1
                    batch_missing_dni += 1
                else:
                    row, was_repaired = normalize_row(
                        row, len(file_header), address_source_index, line_number
                    )
                    repaired += was_repaired
                    batch_repaired += was_repaired
                    batch.append(row)

                if len(batch) >= batch_size:
                    with conn.transaction():
                        cursor.execute(
                            sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
                                stage_identifier, stage_definitions
                            )
                        )
                        batch_inserted = load_batch(
                            cursor,
                            target_identifier,
                            target_identifier_columns,
                            stage_identifier,
                            stage_column_identifiers,
                            dni_stage,
                            batch,
                        )
                        save_checkpoint(
                            cursor,
                            checkpoint_table,
                            table_key,
                            source_path,
                            file_size,
                            file_stat.st_mtime_ns,
                            lines.bytes_read,
                            len(batch),
                            batch_inserted,
                            batch_repaired,
                            batch_missing_dni,
                        )
                    inserted += batch_inserted
                    seen += len(batch)
                    total_inserted += batch_inserted
                    total_processed += len(batch)
                    total_repaired += batch_repaired
                    total_missing_dni += batch_missing_dni
                    batch.clear()
                    batch_repaired = 0
                    batch_missing_dni = 0

                    percent = min(100, lines.bytes_read * 100 // max(file_size, 1))
                    if percent > last_progress:
                        print(
                            f"Progreso: {percent}% | "
                            f"filas procesadas: {total_processed:,} | "
                            f"filas insertadas en esta ejecución: {inserted:,}."
                        )
                        last_progress = percent

            if batch or lines.bytes_read != byte_offset:
                with conn.transaction():
                    if batch:
                        cursor.execute(
                            sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
                                stage_identifier, stage_definitions
                            )
                        )
                        batch_inserted = load_batch(
                            cursor,
                            target_identifier,
                            target_identifier_columns,
                            stage_identifier,
                            stage_column_identifiers,
                            dni_stage,
                            batch,
                        )
                    else:
                        batch_inserted = 0
                    save_checkpoint(
                        cursor,
                        checkpoint_table,
                        table_key,
                        source_path,
                        file_size,
                        file_stat.st_mtime_ns,
                        lines.bytes_read,
                        len(batch),
                        batch_inserted,
                        batch_repaired,
                        batch_missing_dni,
                    )
                inserted += batch_inserted
                seen += len(batch)
                total_inserted += batch_inserted
                total_processed += len(batch)
                total_repaired += batch_repaired
                total_missing_dni += batch_missing_dni

    print(f"Filas procesadas con DNI en esta ejecución: {seen:,}")
    print(f"Filas nuevas insertadas en esta ejecución: {inserted:,}")
    print(f"Filas procesadas acumuladas: {total_processed:,}")
    print(f"Filas insertadas acumuladas según el checkpoint: {total_inserted:,}")
    print(f"Filas con separadores extra reunidos en DIRECCION: {total_repaired:,}")
    print(f"Filas omitidas por no tener DNI: {total_missing_dni:,}")
    print("Progreso: 100%")


def load_batch(
    cursor: psycopg.Cursor,
    target_identifier: sql.Composable,
    target_columns: list[sql.Identifier],
    stage_identifier: sql.Identifier,
    stage_columns: list[sql.Identifier],
    dni_stage: sql.Identifier,
    rows: list[list[str]],
) -> int:
    copy_statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        stage_identifier, sql.SQL(", ").join(stage_columns)
    )
    with cursor.copy(copy_statement) as copy:
        for row in rows:
            copy.write_row(row)

    selected_columns = sql.SQL(", ").join(
        sql.SQL("s.{}").format(column) for column in stage_columns
    )
    order_columns = sql.SQL(", ").join(
        sql.SQL("s.{}").format(column) for column in [dni_stage, *stage_columns]
    )
    cursor.execute(
        sql.SQL(
            """
            INSERT INTO {} ({})
            SELECT DISTINCT ON (s.{}) {}
            FROM {} AS s
            WHERE NOT EXISTS (
                SELECT 1 FROM {} AS existing
                WHERE existing.{} = s.{}
            )
            ORDER BY {}
            ON CONFLICT ({}) DO NOTHING
            """
        ).format(
            target_identifier,
            sql.SQL(", ").join(target_columns),
            dni_stage,
            selected_columns,
            stage_identifier,
            target_identifier,
            target_columns[stage_columns.index(dni_stage)],
            dni_stage,
            order_columns,
            target_columns[stage_columns.index(dni_stage)],
        )
    )
    inserted = cursor.rowcount
    cursor.execute(sql.SQL("TRUNCATE TABLE {}").format(stage_identifier))
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Importa reniec.txt sin volver a insertar DNI ya existentes."
    )
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE, help="Archivo separado por |.")
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Busca tablas con DNI sin consultar valores personales.",
    )
    parser.add_argument(
        "--table",
        help="Tabla destino en formato esquema.tabla (por ejemplo public.reniec).",
    )
    parser.add_argument(
        "--create-table",
        action="store_true",
        help="Crea la tabla destino si no existe, con las cabeceras del TXT.",
    )
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size debe ser mayor que cero.")
    if not args.file.is_file():
        parser.error(f"No existe el archivo: {args.file}")
    if not args.inspect and not args.table:
        parser.error("Usa --inspect para revisar la base o indica --table esquema.tabla.")

    try:
        header = read_header(args.file)
        with get_connection() as conn:
            if args.inspect:
                inspect_database(conn, header)
            else:
                if args.create_table:
                    ensure_destination_table(conn, args.table, header)
                import_file(conn, args.file, args.table, args.batch_size, header)
    except (OSError, ValueError, psycopg.Error, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
