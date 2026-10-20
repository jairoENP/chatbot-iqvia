"""Interfaz de chat sobre los datos de mercado de IQVIA.

    streamlit run chatbot/app.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import Agente  # noqa: E402
from contexto import RUTA_DB_POR_DEFECTO  # noqa: E402
from tools import Paso  # noqa: E402

# Etiqueta corta para el boton + la pregunta completa que se envia. La barra
# lateral es angosta: el texto entero de estas preguntas ocuparia tres o cuatro
# lineas por boton.
#
# Son las preguntas que el equipo comercial hace de verdad, con sus palabras.
# Tres de las cinco piden "y por que" o "el detalle": eso dispara varias vueltas
# de herramientas, asi que son las mas caras de la app. Se nota en el contador
# al pie de cada respuesta.
EJEMPLOS = [
    ("Cómo estamos vs. el mercado",
     "¿Cómo estamos vs el mercado?"),
    ("Dónde vamos bien",
     "¿En qué submercados vamos bien y por qué?"),
    ("Dónde vamos mal",
     "¿En qué submercados vamos mal y por qué?"),
    ("Ranking por corporación",
     "Dame el ranking por corporación y el detalle."),
]

st.set_page_config(page_title="Sniper IA", page_icon="🎯", layout="wide")


def dibujar_paso(paso: Paso) -> None:
    """Muestra un uso de herramienta de forma compacta y auditable."""
    iconos = {"ejecutar_sql": "🗄️", "ejecutar_python": "🐍", "buscar_valores": "🔎"}
    titulo = {
        "ejecutar_sql": "Consulta SQL",
        "ejecutar_python": "Analisis en Python",
        "buscar_valores": "Busqueda de valores",
    }[paso.herramienta]

    with st.expander(f"{iconos[paso.herramienta]} {titulo}", expanded=False):
        if paso.herramienta == "ejecutar_sql":
            st.code(paso.entrada, language="sql")
        elif paso.herramienta == "ejecutar_python":
            st.code(paso.entrada, language="python")
        else:
            st.caption(paso.entrada)
        st.text(paso.salida[:3000])


def dibujar_costo(costo: float, llamadas: int, segundos: float) -> None:
    """Pie con el costo de UNA pregunta.

    Se escribe "USD" y no el simbolo de dolar: Streamlit interpreta el texto
    entre dos signos `$` como formula LaTeX y rompe el render.
    """
    st.caption(
        f"⌁ USD {costo:.3f} · {llamadas} llamadas a la API · {segundos:.0f}s"
    )


@st.cache_resource(show_spinner="Abriendo la base...")
def abrir_base():
    """Conexion COMPARTIDA a la base, de solo lectura.

    Esto si conviene compartirlo entre usuarios: el archivo se abre una sola
    vez y nadie puede escribir. Cada sesion se queda con su propio cursor.
    """
    return duckdb.connect(str(RUTA_DB_POR_DEFECTO), read_only=True)


def obtener_agente() -> Agente:
    """Un Agente POR SESION de navegador.

    OJO: no usar `st.cache_resource` para el Agente. Ese decorador devuelve el
    MISMO objeto a todos los usuarios, y el Agente guarda la conversacion
    (`mensajes`), los DataFrames (`df_1`, `df_2`...) y las figuras. Compartirlo
    mezclaria las conversaciones de dos personas que usen la app a la vez, sin
    que ninguna lo note: cada una veria su propio chat en pantalla mientras
    Claude recibe los dos entremezclados.
    """
    if "agente" not in st.session_state:
        st.session_state.agente = Agente(conexion_base=abrir_base())
    return st.session_state.agente


# ---------------------------------------------------------------------------
if not RUTA_DB_POR_DEFECTO.exists():
    st.error(
        f"No encuentro la base en `{RUTA_DB_POR_DEFECTO}`.\n\n"
        "Genera el archivo en la maquina de trabajo con "
        "`python export/export_to_duckdb.py` y copialo a `data/`."
    )
    st.stop()

try:
    agente = obtener_agente()
except RuntimeError as exc:
    st.error(str(exc))
    st.stop()

st.session_state.setdefault("historial", [])

# -- barra lateral ----------------------------------------------------------
with st.sidebar:
    st.title("🎯 Sniper IA")
    meta = agente.meta
    st.metric("Datos hasta", meta["FECHA_CORTE"].strftime("%Y-%m-%d"))
    st.caption(
        f"Desde {meta['FECHA_DESDE'].strftime('%Y-%m-%d')} · {meta['MESES_INCLUIDOS']} meses · "
        f"{meta['FILAS']:,} filas"
    )
    st.divider()
    if agente.llamadas_api:
        # Es el gasto de ESTA pestania, no el total del equipo: cada sesion
        # tiene su propio Agente. El acumulado real esta en la consola.
        st.caption(
            f"Gasto estimado de tu sesion: **US$ {agente.costo_usd:.3f}** "
            f"({agente.llamadas_api} llamadas a la API)"
        )
    st.divider()
    if st.button("Nueva conversacion", use_container_width=True):
        agente.reiniciar()
        st.session_state.historial = []
        # Valvula de escape: si un ejemplo quedo pendiente sin consumirse (por
        # ejemplo si el script corto antes de llegar al chat), este boton lo
        # descarta en vez de dispararlo en el proximo rerun.
        st.session_state.pop("pregunta_pendiente", None)
        st.rerun()
    st.divider()
    st.caption("**Ejemplos**")
    # Al apretar un boton, Streamlit reejecuta el script de arriba abajo. La
    # barra lateral se dibuja ANTES del turno nuevo, asi que dejar la pregunta
    # en session_state alcanza: cuando el flujo llegue al chat, ya esta ahi.
    # No hace falta st.rerun().
    for indice, (etiqueta, pregunta_ejemplo) in enumerate(EJEMPLOS):
        if st.button(etiqueta, key=f"ejemplo-{indice}", use_container_width=True):
            st.session_state.pregunta_pendiente = pregunta_ejemplo

# -- historial --------------------------------------------------------------
# st.plotly_chart deriva su id del contenido del grafico, asi que dos figuras
# iguales chocan ("StreamlitDuplicateElementId"). Le damos una clave explicita
# a cada una, derivada de su posicion, que es estable entre reruns.
for indice_turno, turno in enumerate(st.session_state.historial):
    with st.chat_message(turno["rol"]):
        for paso in turno.get("pasos", []):
            dibujar_paso(paso)
        st.markdown(turno["texto"])
        for indice_figura, figura in enumerate(turno.get("figuras", [])):
            st.plotly_chart(
                figura,
                use_container_width=True,
                key=f"hist-{indice_turno}-{indice_figura}",
            )
        if turno.get("costo") is not None:
            dibujar_costo(turno["costo"], turno["llamadas"], turno["segundos"])

# -- turno nuevo ------------------------------------------------------------
pregunta = st.chat_input("Pregunta sobre el mercado...")
# Un boton de ejemplo deja la pregunta aca. Se consume con pop para que no se
# vuelva a disparar en el siguiente rerun. Lo que escriba el usuario gana.
if not pregunta:
    pregunta = st.session_state.pop("pregunta_pendiente", None)

if pregunta:
    st.session_state.historial.append({"rol": "user", "texto": pregunta})
    with st.chat_message("user"):
        st.markdown(pregunta)

    with st.chat_message("assistant"):
        zona_pasos = st.container()
        estado = st.empty()
        zona_texto = st.empty()

        texto = ""
        pasos: list[Paso] = []
        estado.caption("Pensando...")

        # El Agente acumula el gasto de toda la sesion, asi que el costo de
        # ESTA pregunta es la diferencia entre antes y despues del turno.
        costo_antes = agente.costo_usd
        llamadas_antes = agente.llamadas_api
        arranque = time.monotonic()

        for evento in agente.preguntar(pregunta):
            if evento.tipo == "texto":
                texto += evento.texto
                estado.empty()
                zona_texto.markdown(texto + "▌")
            elif evento.tipo == "herramienta":
                pasos.append(evento.paso)
                with zona_pasos:
                    dibujar_paso(evento.paso)
                estado.caption("Analizando...")
            elif evento.tipo == "error":
                st.error(evento.texto)

        zona_texto.markdown(texto)
        estado.empty()

        # Prefijo distinto al del historial: este turno todavia no esta ahi, y
        # en el proximo rerun se redibuja con las claves "hist-".
        figuras = list(agente.sesion.figuras)
        for indice_figura, figura in enumerate(figuras):
            st.plotly_chart(
                figura,
                use_container_width=True,
                key=f"vivo-{len(st.session_state.historial)}-{indice_figura}",
            )

        costo = agente.costo_usd - costo_antes
        llamadas = agente.llamadas_api - llamadas_antes
        segundos = time.monotonic() - arranque
        dibujar_costo(costo, llamadas, segundos)

    st.session_state.historial.append(
        {"rol": "assistant", "texto": texto, "pasos": pasos, "figuras": figuras,
         "costo": costo, "llamadas": llamadas, "segundos": segundos}
    )
