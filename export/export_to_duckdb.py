"""Exporta el modelo estrella IQVIA de SQL Server a un archivo DuckDB portable.

Corre en la MAQUINA DE TRABAJO. No usa IA ni servicios externos: es puro
movimiento de datos. El archivo .duckdb resultante se copia a la maquina
personal, donde vive el chatbot.

Uso:
    python export/export_to_duckdb.py
    python export/export_to_duckdb.py --meses 36        # solo ultimos 36 meses
    python export/export_to_duckdb.py --excluir-ceros   # descartar filas sin venta
    python export/export_to_duckdb.py --salida otra.duckdb
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pyodbc
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
SALIDA_POR_DEFECTO = RAIZ / "data" / "iqvia.duckdb"

# Cuantas filas del fact se traen por lote. Bajarlo si hay poca RAM.
TAMANO_LOTE = 200_000


# --------------------------------------------------------------------------
# Conexion
# --------------------------------------------------------------------------
def conectar_sqlserver() -> pyodbc.Connection:
    """Abre la conexion a SQL Server usando el mejor driver ODBC disponible."""
    load_dotenv(RAIZ / ".env")

    faltantes = [
        v
        for v in ("SQLSERVER_HOST", "SQLSERVER_DATABASE", "SQLSERVER_USER", "SQLSERVER_PASSWORD")
        if not os.getenv(v)
    ]
    if faltantes:
        sys.exit(f"ERROR: faltan variables en .env: {', '.join(faltantes)}")

    disponibles = pyodbc.drivers()
    preferidos = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "SQL Server",
    ]
    driver = next((d for d in preferidos if d in disponibles), None)
    if driver is None:
        sys.exit(
            "ERROR: no hay driver ODBC de SQL Server instalado.\n"
            f"Drivers detectados: {disponibles}\n"
            "Alternativa sin instalador: pip install pymssql y adaptar esta funcion."
        )

    cadena = (
        f"DRIVER={{{driver}}};"
        f"SERVER={os.environ['SQLSERVER_HOST']};"
        f"DATABASE={os.environ['SQLSERVER_DATABASE']};"
        f"UID={os.environ['SQLSERVER_USER']};"
        f"PWD={os.environ['SQLSERVER_PASSWORD']};"
    )
    # Los drivers 17/18 exigen definir el cifrado; el driver legacy no lo entiende.
    if driver.startswith("ODBC Driver"):
        cadena += "Encrypt=yes;TrustServerCertificate=yes;"

    print(f"Conectando a {os.environ['SQLSERVER_HOST']} con [{driver}] ...")
    return pyodbc.connect(cadena, timeout=30)


# --------------------------------------------------------------------------
# Extraccion
# --------------------------------------------------------------------------
def fecha_corte(cn: pyodbc.Connection) -> date:
    """Ultimo mes con datos REALES (ES_PROYECCION = 0)."""
    valor = cn.cursor().execute(
        "SELECT MAX(FECHA) FROM dwh.IQVIA_FACT_VENTAS WHERE ES_PROYECCION = 0"
    ).fetchval()
    if valor is None:
        sys.exit("ERROR: la tabla de hechos no tiene filas reales.")
    return valor


def fecha_desde(corte: date, meses: int | None) -> date | None:
    """Primer mes a incluir. None = todo el historico disponible."""
    if meses is None:
        return None
    inicio_mes = date(corte.year, corte.month, 1)
    total = inicio_mes.year * 12 + (inicio_mes.month - 1) - (meses - 1)
    return date(total // 12, total % 12 + 1, 1)


def copiar_dimension(cn, con, tabla_origen: str, tabla_destino: str) -> int:
    df = pd.read_sql(f"SELECT * FROM {tabla_origen}", cn)  # noqa: S608 - nombre fijo
    con.register("_tmp", df)
    con.execute(f"CREATE OR REPLACE TABLE {tabla_destino} AS SELECT * FROM _tmp")
    con.unregister("_tmp")
    return len(df)


def copiar_hechos(cn, con, desde: date | None, excluir_ceros: bool) -> int:
    """Trae el fact por lotes. Solo datos reales; opcionalmente sin filas en cero."""
    condiciones = ["ES_PROYECCION = 0"]
    parametros: list = []
    if desde is not None:
        condiciones.append("FECHA >= ?")
        parametros.append(desde)
    if excluir_ceros:
        # El fact es una grilla densa producto x region x mes: ~64% de las filas
        # son ceros puros. Excluirlas achica el archivo pero cambia la semantica:
        # un producto que no vendio en el mes deja de aparecer en los listados, y
        # su serie mensual queda con huecos en vez de ceros (rompe medias moviles
        # y YoY salvo que se reindexe a mano). Por eso NO es el comportamiento
        # por defecto.
        condiciones.append("(UNIDADES <> 0 OR DOLARES <> 0)")

    where = " AND ".join(condiciones)
    consulta = (
        "SELECT FECHA, ID_PRODUCTO, ID_REGION, UNIDADES, DOLARES, PRECIOS, BOLIVIANOS "
        f"FROM dwh.IQVIA_FACT_VENTAS WHERE {where}"
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE fact_ventas (
            FECHA       DATE,
            ID_PRODUCTO INTEGER,
            ID_REGION   INTEGER,
            UNIDADES    DOUBLE,
            DOLARES     DOUBLE,
            PRECIOS     DOUBLE,
            BOLIVIANOS  DOUBLE
        )
        """
    )

    total = 0
    for lote in pd.read_sql(consulta, cn, params=parametros, chunksize=TAMANO_LOTE):
        con.register("_lote", lote)
        con.execute("INSERT INTO fact_ventas SELECT * FROM _lote")
        con.unregister("_lote")
        total += len(lote)
        print(f"    ... {total:,} filas", end="\r", flush=True)
    print(" " * 40, end="\r")
    return total


# --------------------------------------------------------------------------
# Construccion del modelo en DuckDB
# --------------------------------------------------------------------------
def crear_calendario(con) -> int:
    con.execute(
        """
        CREATE OR REPLACE TABLE dim_calendario AS
        SELECT d::DATE                                     AS FECHA,
               YEAR(d)                                     AS ANIO,
               CAST(strftime(d, '%Y%m') AS INTEGER)        AS ANIO_MES,
               'Q' || QUARTER(d)                           AS TRIMESTRE,
               MONTH(d)                                    AS MES_NUM,
               strftime(d, '%Y-%m')                        AS ANIO_MES_TEXTO
        FROM generate_series(
                 (SELECT MIN(FECHA) FROM fact_ventas),
                 (SELECT MAX(FECHA) FROM fact_ventas),
                 INTERVAL 1 MONTH) t(d)
        """
    )
    return con.execute("SELECT COUNT(*) FROM dim_calendario").fetchone()[0]


def crear_vista_plana(con) -> None:
    """La vista que consulta el chatbot.

    Aplanar el esquema estrella en una sola vista reduce muchisimo los errores
    de JOIN que comete un LLM. Las tablas base siguen disponibles por si hace
    falta (por ejemplo, para preguntar por productos SIN ventas).
    """
    con.execute(
        """
        CREATE OR REPLACE VIEW vw_ventas AS
        SELECT
            f.FECHA,
            YEAR(f.FECHA)                          AS ANIO,
            MONTH(f.FECHA)                         AS MES_NUM,
            CAST(strftime(f.FECHA, '%Y%m') AS INTEGER) AS ANIO_MES,
            'Q' || QUARTER(f.FECHA)                AS TRIMESTRE,

            r.REGION,
            r.COD_REGION,

            p.COD_PRODUCTO,
            p.PRODUCTO,
            -- MARCA en origen es un campo de ancho fijo de 22 caracteres:
            -- 19 para el nombre (rellenado con espacios) + 3 para el codigo de
            -- laboratorio ('ENSURE ADVANCE     ABT'). Comparar con '=' contra el
            -- nombre que escribe una persona nunca coincide, asi que exponemos
            -- el nombre ya limpio y dejamos el valor original aparte.
            TRIM(LEFT(p.MARCA, 19))                AS MARCA,
            RIGHT(p.MARCA, 3)                      AS COD_LABORATORIO,
            p.MARCA                                AS MARCA_IQVIA,
            p.MOLECULA,
            p.FORMA,
            p.LANZAMIENTO,

            p.CORPORACION,
            p.LABORATORIO,
            (p.CORPORACION = 'ABBOTT CORP')        AS ES_ABBOTT,

            p.SUB_MERCADO,
            p.DIVISION,
            p.DIVISION_INTENDED,
            p.INTENDED,
            p.TIPO_PRODUCTO,
            p.TIPO_MERCADO,

            p.COD_CLASE1, p.CLASE1,
            p.COD_CLASE2, p.CLASE2,
            p.COD_CLASE3, p.CLASE3,
            p.COD_CLASE4, p.CLASE4,

            f.UNIDADES,
            f.DOLARES,
            f.BOLIVIANOS,
            f.PRECIOS
        FROM fact_ventas f
        JOIN dim_presentaciones p ON p.ID_PRODUCTO = f.ID_PRODUCTO
        JOIN dim_regiones       r ON r.ID_REGION   = f.ID_REGION
        """
    )


def crear_meta(con, corte: date, meses: int | None, excluir_ceros: bool) -> None:
    rango = con.execute(
        "SELECT MIN(FECHA), MAX(FECHA), COUNT(DISTINCT FECHA) FROM fact_ventas"
    ).fetchone()
    con.execute(
        """
        CREATE OR REPLACE TABLE meta AS
        SELECT ?::DATE      AS FECHA_CORTE,
               ?::DATE      AS FECHA_DESDE,
               ?::INTEGER   AS MESES_INCLUIDOS,
               ?::BOOLEAN   AS EXCLUYE_FILAS_EN_CERO,
               ?::VARCHAR   AS VENTANA_SOLICITADA,
               now()::TIMESTAMP AS GENERADO_EN
        """,
        [corte, rango[0], rango[2], excluir_ceros,
         "historico completo" if meses is None else f"ultimos {meses} meses"],
    )


def verificar(con) -> None:
    """Chequeos de integridad. Si alguno falla, el archivo no sirve."""
    huerfanos = con.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM fact_ventas f
             WHERE NOT EXISTS (SELECT 1 FROM dim_presentaciones p WHERE p.ID_PRODUCTO = f.ID_PRODUCTO)),
          (SELECT COUNT(*) FROM fact_ventas f
             WHERE NOT EXISTS (SELECT 1 FROM dim_regiones r WHERE r.ID_REGION = f.ID_REGION))
        """
    ).fetchone()
    if any(huerfanos):
        sys.exit(f"ERROR de integridad: {huerfanos[0]} filas sin producto, {huerfanos[1]} sin region.")

    filas_vista, filas_fact = con.execute(
        "SELECT (SELECT COUNT(*) FROM vw_ventas), (SELECT COUNT(*) FROM fact_ventas)"
    ).fetchone()
    if filas_vista != filas_fact:
        sys.exit(f"ERROR: vw_ventas tiene {filas_vista:,} filas y fact_ventas {filas_fact:,}.")

    if con.execute("SELECT COUNT(*) FROM vw_ventas WHERE ES_ABBOTT").fetchone()[0] == 0:
        sys.exit("ERROR: ninguna fila quedo marcada como ABBOTT. Revisar el valor de CORPORACION.")

    # La limpieza de MARCA asume ancho fijo de 22 (19 nombre + 3 codigo de lab).
    # Si IQVIA cambia el formato, mejor enterarse aca que en una respuesta erronea.
    anomalas = con.execute(
        "SELECT COUNT(*) FROM dim_presentaciones WHERE LENGTH(MARCA) <> 22"
    ).fetchone()[0]
    if anomalas:
        sys.exit(
            f"ERROR: {anomalas:,} valores de MARCA no miden 22 caracteres. "
            "Cambio el formato de IQVIA: revisar TRIM(LEFT(MARCA, 19)) en crear_vista_plana()."
        )


def resumen(con, ruta: Path) -> None:
    print("\n" + "=" * 70)
    print("RESUMEN")
    print("=" * 70)

    for tabla in ("dim_presentaciones", "dim_regiones", "fact_ventas", "dim_calendario"):
        n = con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]  # noqa: S608
        print(f"  {tabla:<22} {n:>12,} filas")

    desde, hasta, meses = con.execute(
        "SELECT MIN(FECHA), MAX(FECHA), COUNT(DISTINCT FECHA) FROM fact_ventas"
    ).fetchone()
    print(f"\n  Periodo: {desde} a {hasta}  ({meses} meses)")
    print(f"  Archivo: {ruta}  ({ruta.stat().st_size / 1024 / 1024:.1f} MB)")

    print("\n  Control por anio (contrastar contra Power BI):")
    control = con.execute(
        """
        SELECT ANIO,
               COUNT(*)                          AS FILAS,
               ROUND(SUM(DOLARES), 0)            AS DOLARES,
               ROUND(SUM(UNIDADES), 0)           AS UNIDADES,
               ROUND(SUM(CASE WHEN ES_ABBOTT THEN DOLARES END), 0) AS DOLARES_ABBOTT
        FROM vw_ventas GROUP BY ANIO ORDER BY ANIO
        """
    ).df()
    print(control.to_string(index=False).replace("\n", "\n  "))
    print("=" * 70)


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="SQL Server -> DuckDB para el chatbot IQVIA")
    ap.add_argument(
        "--meses",
        type=int,
        default=None,
        help="Cuantos meses de historia traer. Por defecto: todo el historico real.",
    )
    ap.add_argument(
        "--excluir-ceros",
        action="store_true",
        help="Descartar las filas producto/region/mes sin ventas (~64%% del total). "
        "Achica el archivo, pero los productos sin venta desaparecen de los "
        "listados y sus series quedan con huecos en vez de ceros.",
    )
    ap.add_argument("--salida", type=Path, default=SALIDA_POR_DEFECTO)
    args = ap.parse_args()

    inicio = time.time()
    args.salida.parent.mkdir(parents=True, exist_ok=True)
    if args.salida.exists():
        args.salida.unlink()

    cn = conectar_sqlserver()
    con = duckdb.connect(str(args.salida))
    try:
        corte = fecha_corte(cn)
        desde = fecha_desde(corte, args.meses)
        print(f"Ultimo mes real en SQL Server: {corte}")
        print(f"Ventana: {'todo el historico' if desde is None else f'desde {desde}'}")
        print(f"Filas en cero: {'se excluyen' if args.excluir_ceros else 'se conservan'}\n")

        print("Dimensiones ...")
        n = copiar_dimension(cn, con, "dwh.IQVIA_DIM_PRESENTACIONES", "dim_presentaciones")
        print(f"  dim_presentaciones: {n:,}")
        n = copiar_dimension(cn, con, "dwh.IQVIA_DIM_REGIONES", "dim_regiones")
        print(f"  dim_regiones: {n:,}")

        print("Hechos ...")
        n = copiar_hechos(cn, con, desde, args.excluir_ceros)
        print(f"  fact_ventas: {n:,}")
        if n == 0:
            sys.exit("ERROR: no se trajo ninguna fila.")

        print("Calendario, vista plana y metadatos ...")
        crear_calendario(con)
        crear_vista_plana(con)
        crear_meta(con, corte, args.meses, args.excluir_ceros)

        print("Verificando integridad ...")
        verificar(con)
    finally:
        cn.close()
        con.close()

    con = duckdb.connect(str(args.salida), read_only=True)
    resumen(con, args.salida)
    con.close()
    print(f"\nListo en {time.time() - inicio:.1f}s. Copiar {args.salida.name} a la maquina personal.")


if __name__ == "__main__":
    main()
