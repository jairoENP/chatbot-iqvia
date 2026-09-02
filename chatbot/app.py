"""Interfaz de chat sobre los datos de mercado de IQVIA.

    streamlit run chatbot/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import Agente  # noqa: E402
from contexto import RUTA_DB_POR_DEFECTO  # noqa: E402
from tools import Paso  # noqa: E402

EJEMPLOS = [
    "Cual es el producto que mas se vende de la molecula paracetamol?",
    "En el sub-mercado de Ensure, quien es mi mayor competencia?",
    "Como viene la tendencia de las ventas de Abbott en los ultimos 24 meses?",
    "Que participacion de mercado tiene Abbott en cada region?",
    "Que marcas de la competencia crecieron mas el ultimo ano?",
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


@st.cache_resource(show_spinner="Conectando...")
def crear_agente():
    return Agente()


# ---------------------------------------------------------------------------
if not RUTA_DB_POR_DEFECTO.exists():
    st.error(
        f"No encuentro la base en `{RUTA_DB_POR_DEFECTO}`.\n\n"
        "Genera el archivo en la maquina de trabajo con "
        "`python export/export_to_duckdb.py` y copialo a `data/`."
    )
    st.stop()

try:
    agente = crear_agente()
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
        st.caption(
            f"Gasto estimado de la sesion: **US$ {agente.costo_usd:.3f}** "
            f"({agente.llamadas_api} llamadas a la API)"
        )
    st.divider()
    if st.button("Nueva conversacion", use_container_width=True):
        agente.reiniciar()
        st.session_state.historial = []
        st.rerun()
    st.divider()
    st.caption("**Ejemplos**")
    for ejemplo in EJEMPLOS:
        st.caption(f"· {ejemplo}")

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

# -- turno nuevo ------------------------------------------------------------
if pregunta := st.chat_input("Pregunta sobre el mercado..."):
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

    st.session_state.historial.append(
        {"rol": "assistant", "texto": texto, "pasos": pasos, "figuras": figuras}
    )
