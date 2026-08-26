"""Las tres herramientas del agente, con su estado de sesion.

Cada conversacion tiene su propia `Sesion`: su conexion de solo lectura, sus
DataFrames y su registro de lo ejecutado (que la UI muestra para que toda cifra
sea auditable).
"""
from __future__ import annotations

import ast
import io
import re
import textwrap
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Cuantas filas ve el MODELO. El DataFrame completo queda para ejecutar_python.
FILAS_VISIBLES = 40
# Tope de caracteres de una respuesta de herramienta, para no inundar el contexto.
MAX_CARACTERES = 6000

# Columnas sobre las que buscar_valores puede operar.
COLUMNAS_BUSCABLES = (
    "MOLECULA", "PRODUCTO", "MARCA", "LABORATORIO", "CORPORACION",
    "SUB_MERCADO", "DIVISION", "DIVISION_INTENDED", "FORMA",
    "CLASE1", "CLASE2", "CLASE3", "CLASE4",
)

_PROHIBIDO_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|"
    r"install|load|pragma|call|set)\b",
    re.IGNORECASE,
)

_MODULOS_PERMITIDOS = {
    "pandas", "numpy", "math", "statistics", "datetime", "itertools",
    "collections", "re", "json", "plotly", "plotly.express",
    "plotly.graph_objects", "plotly.subplots",
}


def _recortar(texto: str) -> str:
    if len(texto) <= MAX_CARACTERES:
        return texto
    return texto[:MAX_CARACTERES] + f"\n... (recortado, {len(texto):,} caracteres en total)"


def _tabla(datos: pd.DataFrame | pd.Series) -> str:
    """Formatea un DataFrame o Series para que lo lea el modelo."""
    if datos.empty:
        return "(sin filas)"

    vista = datos.head(FILAS_VISIBLES)
    if isinstance(datos, pd.Series):
        # En una Series el indice suele ser la fecha: hay que mostrarlo.
        texto = vista.to_string()
    else:
        # En un DataFrame el indice por defecto (0,1,2...) es ruido, pero un
        # indice con significado (fechas tras un set_index) no lo es.
        con_indice = not isinstance(datos.index, pd.RangeIndex)
        texto = vista.to_string(index=con_indice, max_colwidth=45)

    if len(datos) > FILAS_VISIBLES:
        texto += f"\n... {len(datos) - FILAS_VISIBLES:,} filas mas (el objeto completo las tiene)"
    return texto


@dataclass
class Paso:
    """Un uso de herramienta, para el panel 'como se calculo'."""
    herramienta: str
    entrada: str
    salida: str


@dataclass
class Sesion:
    ruta_db: Path
    con: duckdb.DuckDBPyConnection = field(init=False)
    dataframes: dict[str, pd.DataFrame] = field(default_factory=dict)
    figuras: list = field(default_factory=list)
    pasos: list[Paso] = field(default_factory=list)
    _contador: int = 0

    def __post_init__(self) -> None:
        # read_only es la garantia real de que el agente no puede escribir.
        self.con = duckdb.connect(str(self.ruta_db), read_only=True)

    def cerrar(self) -> None:
        self.con.close()

    # -- herramienta 1 ----------------------------------------------------
    def buscar_valores(self, columna: str, termino: str, limite: int = 25) -> str:
        columna = columna.upper().strip()
        if columna not in COLUMNAS_BUSCABLES:
            return (
                f"Columna '{columna}' no buscable. "
                f"Opciones: {', '.join(COLUMNAS_BUSCABLES)}"
            )

        # Los datos estan en mayusculas y en ingles para MOLECULA; comparamos
        # sin distinguir mayusculas y por coincidencia parcial en ambos sentidos.
        df = self.con.execute(
            f"""
            SELECT {columna} AS VALOR,
                   COUNT(DISTINCT COD_PRODUCTO) AS PRESENTACIONES,
                   ROUND(SUM(DOLARES), 0)       AS DOLARES_TOTAL
            FROM vw_ventas
            WHERE {columna} IS NOT NULL
              AND (upper({columna}) LIKE '%' || upper(?) || '%'
                   OR damerau_levenshtein(upper({columna}), upper(?)) <= 2)
            GROUP BY {columna}
            ORDER BY DOLARES_TOTAL DESC
            LIMIT ?
            """,  # noqa: S608 - columna validada contra COLUMNAS_BUSCABLES
            [termino, termino, limite],
        ).df()

        if df.empty:
            # Segundo intento: por palabras sueltas, util para combinaciones
            # tipo 'SULFAMETHOXAZOLE - TRIMETHOPRIM'.
            palabras = [p for p in re.split(r"[\s,\-+]+", termino) if len(p) > 3]
            if palabras:
                condicion = " OR ".join(f"upper({columna}) LIKE '%' || upper(?) || '%'" for _ in palabras)
                df = self.con.execute(
                    f"""
                    SELECT {columna} AS VALOR,
                           COUNT(DISTINCT COD_PRODUCTO) AS PRESENTACIONES,
                           ROUND(SUM(DOLARES), 0)       AS DOLARES_TOTAL
                    FROM vw_ventas
                    WHERE {columna} IS NOT NULL AND ({condicion})
                    GROUP BY {columna} ORDER BY DOLARES_TOTAL DESC LIMIT ?
                    """,  # noqa: S608
                    [*palabras, limite],
                ).df()

        if df.empty:
            return (
                f"Ningun valor de {columna} coincide con '{termino}'.\n"
                f"Ojo: MOLECULA esta en INGLES (IBUPROFEN, no ibuprofeno). "
                f"Proba con otro termino o con otra columna."
            )

        return f"Valores de {columna} que coinciden con '{termino}':\n\n{_tabla(df)}"

    # -- herramienta 2 ----------------------------------------------------
    def ejecutar_sql(self, consulta: str) -> str:
        limpia = consulta.strip().rstrip(";")
        sin_comentarios = re.sub(r"--[^\n]*|/\*.*?\*/", " ", limpia, flags=re.DOTALL)

        if not re.match(r"^\s*(select|with)\b", sin_comentarios, re.IGNORECASE):
            return "ERROR: solo se permiten consultas SELECT o WITH."
        if _PROHIBIDO_SQL.search(sin_comentarios):
            return "ERROR: la consulta contiene una sentencia de modificacion, que no esta permitida."

        try:
            df = self.con.execute(limpia).df()
        except Exception as exc:  # noqa: BLE001
            return f"ERROR de SQL: {exc}"

        self._contador += 1
        nombre = f"df_{self._contador}"
        self.dataframes[nombre] = df

        encabezado = (
            f"{nombre}: {len(df):,} filas x {len(df.columns)} columnas "
            f"({', '.join(df.columns)})"
        )
        return _recortar(f"{encabezado}\n\n{_tabla(df)}")

    # -- herramienta 3 ----------------------------------------------------
    def ejecutar_python(self, codigo: str) -> str:
        espacio: dict[str, Any] = {
            "pd": pd, "np": np, "px": px, "go": go,
            "__builtins__": _builtins_restringidos(),
            **self.dataframes,
        }

        salida = io.StringIO()
        try:
            arbol = ast.parse(textwrap.dedent(codigo))
        except SyntaxError as exc:
            return f"ERROR de sintaxis: {exc}"

        # Si la ultima linea es una expresion, mostramos su valor (como en un REPL).
        ultima_expresion = None
        if arbol.body and isinstance(arbol.body[-1], ast.Expr):
            ultima_expresion = ast.Expression(arbol.body.pop().value)

        try:
            with redirect_stdout(salida):
                exec(compile(arbol, "<agente>", "exec"), espacio)  # noqa: S102
                valor = (
                    eval(compile(ultima_expresion, "<agente>", "eval"), espacio)  # noqa: S307
                    if ultima_expresion is not None
                    else None
                )
        except Exception:  # noqa: BLE001
            detalle = traceback.format_exc(limit=2)
            return f"ERROR al ejecutar:\n{detalle}\nSalida parcial:\n{salida.getvalue()}"

        # Los DataFrames nuevos quedan disponibles para llamadas posteriores.
        for nombre, obj in espacio.items():
            if isinstance(obj, pd.DataFrame) and not nombre.startswith("_"):
                self.dataframes[nombre] = obj

        # Cualquier variable de nivel superior que sea una figura de Plotly se
        # toma como grafico nuevo generado en esta llamada (el namespace se
        # arma de cero en cada ejecutar_python, asi que no puede haber una
        # figura "vieja" colandose aca). Deduplicamos por identidad: la misma
        # figura puede estar en dos variables, o en una variable y ademas como
        # ultima expresion, y mostrarla dos veces rompe la interfaz.
        candidatas = [obj for nombre, obj in espacio.items()
                      if isinstance(obj, go.Figure) and not nombre.startswith("_")]
        if isinstance(valor, go.Figure):
            candidatas.append(valor)

        nuevas: list = []
        for figura in candidatas:
            if not any(figura is vista for vista in nuevas):
                nuevas.append(figura)
        self.figuras.extend(nuevas)

        partes = []
        if texto := salida.getvalue().strip():
            partes.append(texto)
        if valor is not None and not isinstance(valor, go.Figure):
            partes.append(_tabla(valor) if isinstance(valor, (pd.DataFrame, pd.Series)) else repr(valor))
        if nuevas:
            partes.append(f"[{len(nuevas)} grafico(s) generado(s) y mostrado(s) al usuario]")
        if not partes:
            partes.append("(sin salida: recorda imprimir o dejar una expresion en la ultima linea)")

        return _recortar("\n\n".join(partes))

    # ---------------------------------------------------------------------
    def ejecutar(self, herramienta: str, entrada: dict) -> str:
        despacho = {
            "buscar_valores": lambda e: self.buscar_valores(
                e["columna"], e["termino"], e.get("limite", 25)
            ),
            "ejecutar_sql": lambda e: self.ejecutar_sql(e["consulta"]),
            "ejecutar_python": lambda e: self.ejecutar_python(e["codigo"]),
        }
        if herramienta not in despacho:
            return f"ERROR: herramienta desconocida '{herramienta}'."
        try:
            resultado = despacho[herramienta](entrada)
        except KeyError as exc:
            resultado = f"ERROR: falta el parametro {exc}."

        self.pasos.append(
            Paso(
                herramienta=herramienta,
                entrada=entrada.get("consulta") or entrada.get("codigo")
                or f"{entrada.get('columna')} ~ {entrada.get('termino')}",
                salida=resultado,
            )
        )
        return resultado


def _builtins_restringidos() -> dict:
    """Builtins seguros para el codigo que escribe el modelo.

    El riesgo real es bajo (corre local, sobre codigo generado a partir de las
    preguntas del propio usuario), pero bloquear el acceso al sistema de
    archivos y a la red evita que una alucinacion toque algo que no debe.
    """
    import builtins

    permitidos = {
        "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
        "float", "format", "frozenset", "getattr", "hasattr", "int", "isinstance",
        "issubclass", "len", "list", "map", "max", "min", "next", "print", "range",
        "repr", "reversed", "round", "set", "slice", "sorted", "str", "sum",
        "tuple", "type", "zip", "True", "False", "None", "Exception",
        "ValueError", "KeyError", "TypeError", "IndexError", "ZeroDivisionError",
    }
    espacio = {n: getattr(builtins, n) for n in permitidos if hasattr(builtins, n)}

    def _importar(nombre, *args, **kwargs):
        if nombre.split(".")[0] not in {m.split(".")[0] for m in _MODULOS_PERMITIDOS}:
            raise ImportError(
                f"El modulo '{nombre}' no esta permitido. "
                f"Disponibles: {', '.join(sorted(_MODULOS_PERMITIDOS))}"
            )
        return builtins.__import__(nombre, *args, **kwargs)

    espacio["__import__"] = _importar
    return espacio


# ---------------------------------------------------------------------------
# Definiciones que ve el modelo
# ---------------------------------------------------------------------------
HERRAMIENTAS = [
    {
        "name": "buscar_valores",
        "description": (
            "Busca como esta escrito realmente un valor de texto en la base. "
            "USALA SIEMPRE antes de filtrar por una molecula, producto, marca, "
            "laboratorio, corporacion, sub-mercado o clase terapeutica que "
            "menciono el usuario. Tolera errores de tipeo y coincidencias "
            "parciales, y devuelve los valores ordenados por facturacion. "
            "Sin esto, un WHERE con el texto del usuario devuelve cero filas "
            "sin avisar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "columna": {
                    "type": "string",
                    "enum": list(COLUMNAS_BUSCABLES),
                    "description": "Columna donde buscar.",
                },
                "termino": {
                    "type": "string",
                    "description": "Lo que dijo el usuario, tal cual. Ej: 'paracetamol', 'ensure'.",
                },
                "limite": {
                    "type": "integer",
                    "description": "Cuantos valores devolver (por defecto 25).",
                },
            },
            "required": ["columna", "termino"],
        },
    },
    {
        "name": "ejecutar_sql",
        "description": (
            "Ejecuta una consulta SELECT de DuckDB sobre los datos de IQVIA y "
            "guarda el resultado completo como df_1, df_2, etc. Vos solo ves las "
            "primeras filas; el DataFrame entero queda disponible para "
            "ejecutar_python. Usala para filtrar, agrupar, sumar y rankear. "
            "La vista principal es vw_ventas. Solo lectura."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "consulta": {
                    "type": "string",
                    "description": "Consulta SELECT o WITH en dialecto DuckDB.",
                }
            },
            "required": ["consulta"],
        },
    },
    {
        "name": "ejecutar_python",
        "description": (
            "Ejecuta Python con pandas (pd), numpy (np) y Plotly (px = "
            "plotly.express, go = plotly.graph_objects), con todos los "
            "DataFrames de consultas previas (df_1, df_2, ...) ya cargados. "
            "Usala para lo que SQL hace mal: tendencias, crecimiento YoY, "
            "CAGR, medias moviles, MAT, evolucion de participacion, "
            "estacionalidad y proyecciones. Cualquier figura de Plotly que "
            "quede en una variable de nivel superior (o como ultima "
            "expresion) se le muestra al usuario, interactiva. Imprimi con "
            "print() o deja una expresion en la ultima linea para ver un "
            "resultado que no sea grafico."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "codigo": {"type": "string", "description": "Codigo Python a ejecutar."}
            },
            "required": ["codigo"],
        },
    },
]
