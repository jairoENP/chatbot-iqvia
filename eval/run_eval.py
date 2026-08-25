"""Corre el set de validacion contra el agente y contrasta con la verdad de SQL.

    python eval/run_eval.py                  # todos los casos
    python eval/run_eval.py --caso precio_promedio
    python eval/run_eval.py --recalcular     # solo imprime las verdades, sin gastar API

La revision automatica es deliberadamente simple: busca los valores clave en la
respuesta. Lo importante es la columna de la derecha, la verdad calculada en
SQL, que permite revisar a ojo si el agente razono bien. Un OK automatico no
garantiza que la respuesta sea buena, pero un FALLA siempre merece atencion.
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import duckdb
import yaml

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "chatbot"))

from contexto import RUTA_DB_POR_DEFECTO  # noqa: E402

# Frases que indican que el agente reconocio no tener el dato.
SENALES_NEGATIVAS = (
    "no tengo", "no hay datos", "no existe", "no encontre", "no encuentro",
    "no dispongo", "fuera del periodo", "no figura", "no aparece",
    "no puedo responder", "sin datos", "no se encontro",
)


def normalizar(texto: str) -> str:
    """Minusculas, sin acentos y con los miles normalizados, para comparar."""
    texto = unicodedata.normalize("NFKD", texto.lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    # 15.244.345 y 15,244,345 se vuelven comparables
    return re.sub(r"(?<=\d)[.,](?=\d{3}\b)", "", texto)


def revisar(caso: dict, respuesta: str) -> tuple[bool, str]:
    texto = normalizar(respuesta)

    if caso.get("espera_negativa"):
        if any(s in texto for s in SENALES_NEGATIVAS):
            return True, "reconocio que no tiene el dato"
        return False, "NO aviso que falta el dato (riesgo de respuesta inventada)"

    esperados = caso.get("debe_contener") or []
    prohibidos = caso.get("no_debe_contener") or []

    encontrados = [e for e in esperados if normalizar(str(e)) in texto]
    aparecidos = [p for p in prohibidos if normalizar(str(p)) in texto]

    if aparecidos:
        return False, f"aparece un valor incorrecto: {aparecidos}"

    if not esperados:
        return True, "sin verificacion automatica: revisar a mano"

    if caso.get("modo_contener") == "cualquiera":
        ok = bool(encontrados)
    else:
        ok = len(encontrados) == len(esperados)

    faltantes = [e for e in esperados if e not in encontrados]
    return ok, "todo presente" if ok else f"falta en la respuesta: {faltantes}"


def verdad(con, sql: str) -> str:
    try:
        return con.execute(sql).df().to_string(index=False)
    except Exception as exc:  # noqa: BLE001
        return f"(error en la verificacion: {exc})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--caso", help="Corre un solo caso por id.")
    ap.add_argument("--recalcular", action="store_true",
                    help="Solo imprime las verdades de SQL. No llama a la API.")
    args = ap.parse_args()

    casos = yaml.safe_load((RAIZ / "eval" / "preguntas.yaml").read_text(encoding="utf-8"))
    if args.caso:
        casos = [c for c in casos if c["id"] == args.caso]
        if not casos:
            sys.exit(f"No existe el caso '{args.caso}'.")

    con = duckdb.connect(str(RUTA_DB_POR_DEFECTO), read_only=True)

    if args.recalcular:
        for caso in casos:
            print(f"\n{'=' * 74}\n{caso['id']}: {caso['pregunta']}\n{'=' * 74}")
            print(verdad(con, caso["verificacion"]))
        con.close()
        return

    from agent import Agente  # noqa: PLC0415 - solo si vamos a llamar a la API

    agente = Agente()
    resultados = []

    for i, caso in enumerate(casos, 1):
        print(f"\n{'=' * 74}")
        print(f"[{i}/{len(casos)}] {caso['id']}")
        print(f"{'=' * 74}")
        print(f"PREGUNTA: {caso['pregunta']}")
        print(f"QUE PRUEBA: {caso.get('prueba', '').strip()}\n")

        agente.reiniciar()
        respuesta, herramientas = "", []
        for evento in agente.preguntar(caso["pregunta"]):
            if evento.tipo == "texto":
                respuesta += evento.texto
            elif evento.tipo == "herramienta":
                herramientas.append(evento.paso.herramienta)
            elif evento.tipo == "error":
                respuesta += f"\n[ERROR] {evento.texto}"

        ok, motivo = revisar(caso, respuesta)
        resultados.append((caso["id"], ok, motivo))

        print(f"RESPUESTA:\n{respuesta}\n")
        print(f"HERRAMIENTAS: {' -> '.join(herramientas) or '(ninguna)'}")
        print(f"\nVERDAD SEGUN SQL:\n{verdad(con, caso['verificacion'])}")
        print(f"\n{'OK  ' if ok else 'FALLA'} :: {motivo}")

    agente.cerrar()
    con.close()

    print(f"\n\n{'=' * 74}\nRESUMEN\n{'=' * 74}")
    for nombre, ok, motivo in resultados:
        print(f"  {'OK   ' if ok else 'FALLA'}  {nombre:<26} {motivo}")
    aciertos = sum(1 for _, ok, _ in resultados if ok)
    print(f"\n  {aciertos}/{len(resultados)} casos pasaron la revision automatica.")
    print("  Revisar igual las respuestas contra la verdad de SQL: el chequeo")
    print("  automatico detecta cifras erroneas, no razonamientos flojos.")


if __name__ == "__main__":
    main()
