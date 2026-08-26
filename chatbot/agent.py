"""El agente: bucle de conversacion con Claude y sus herramientas.

Emite eventos a medida que avanza para que la interfaz muestre el progreso en
vivo en lugar de quedarse congelada mientras el modelo consulta la base.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from contexto import RUTA_DB_POR_DEFECTO, construir_system_prompt, leer_meta
from tools import HERRAMIENTAS, Paso, Sesion

MODELO = "claude-opus-5"
MAX_TOKENS = 16_000
# Tope de vueltas del bucle: evita que un error repetido consuma la cuenta.
MAX_ITERACIONES = 12

# Precio por millon de tokens, para estimar el gasto en pantalla.
# Fuente: precios de lista de la API de Anthropic.
PRECIOS_USD = {
    "claude-opus-5":   {"entrada": 5.0, "salida": 25.0},
    "claude-sonnet-5": {"entrada": 2.0, "salida": 10.0},
    "claude-haiku-4-5": {"entrada": 1.0, "salida": 5.0},
}
# Lo leido del cache cuesta ~10% de la entrada normal; escribirlo, ~25% mas.
FACTOR_CACHE_LECTURA = 0.1
FACTOR_CACHE_ESCRITURA = 1.25


@dataclass
class Evento:
    """Algo que la interfaz puede mostrar apenas ocurre."""
    tipo: str  # "texto" | "razonamiento" | "herramienta" | "error"
    texto: str = ""
    paso: Paso | None = None


class Agente:
    def __init__(self, ruta_db: Path | str = RUTA_DB_POR_DEFECTO, modelo: str = MODELO):
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "Falta ANTHROPIC_API_KEY. Agregala al archivo .env "
                "(se saca de console.anthropic.com)."
            )

        self.modelo = modelo
        self.cliente = anthropic.Anthropic()
        self.sesion = Sesion(Path(ruta_db))
        self.meta = leer_meta(ruta_db)
        self.mensajes: list[dict] = []
        self.costo_usd = 0.0       # acumulado de la sesion
        self.llamadas_api = 0

        # Bloque estable => se cachea y las preguntas siguientes cuestan ~10%.
        self.system = [
            {
                "type": "text",
                "text": construir_system_prompt(self.meta),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # ------------------------------------------------------------------
    def preguntar(self, pregunta: str) -> Iterator[Evento]:
        """Procesa una pregunta y va emitiendo eventos hasta tener la respuesta."""
        self.sesion.figuras.clear()
        self.mensajes.append({"role": "user", "content": pregunta})

        for _ in range(MAX_ITERACIONES):
            try:
                respuesta = yield from self._turno()
            except anthropic.APIError as exc:
                yield Evento("error", f"Error de la API: {exc}")
                return

            self.mensajes.append({"role": "assistant", "content": respuesta.content})

            if respuesta.stop_reason != "tool_use":
                return

            resultados = []
            for bloque in respuesta.content:
                if bloque.type != "tool_use":
                    continue
                salida = self.sesion.ejecutar(bloque.name, bloque.input)
                yield Evento("herramienta", paso=self.sesion.pasos[-1])
                resultados.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": bloque.id,
                        "content": salida,
                        "is_error": salida.startswith("ERROR"),
                    }
                )

            # Todos los tool_result van en UN solo mensaje de usuario.
            self.mensajes.append({"role": "user", "content": resultados})

        yield Evento(
            "error",
            f"Me pase de {MAX_ITERACIONES} pasos sin llegar a una respuesta. "
            "Proba reformular la pregunta de forma mas acotada.",
        )

    # ------------------------------------------------------------------
    def _marcar_cache(self) -> None:
        """Mueve el punto de cacheo al final de la conversacion.

        La API no tiene memoria: cada paso del bucle de herramientas reenvia
        TODO lo anterior (system prompt, pregunta, y cada resultado de SQL o
        Python acumulado). En una pregunta de 8 pasos eso significa pagar el
        historial ocho veces. Marcando el ultimo mensaje de usuario, la llamada
        siguiente reutiliza todo ese prefijo desde el cache, a ~10% del precio.

        Solo se marca UN punto movil (mas el del system prompt): el cache es por
        prefijo, asi que un punto al final cubre todo lo que vino antes.
        """
        for mensaje in self.mensajes:
            if isinstance(mensaje["content"], list):
                for bloque in mensaje["content"]:
                    if isinstance(bloque, dict):
                        bloque.pop("cache_control", None)

        for mensaje in reversed(self.mensajes):
            if mensaje["role"] != "user":
                continue
            # Un mensaje de usuario puede ser texto suelto (la pregunta) o una
            # lista de tool_result. Solo el segundo caso admite cache_control.
            if isinstance(mensaje["content"], list) and mensaje["content"]:
                ultimo = mensaje["content"][-1]
                if isinstance(ultimo, dict):
                    ultimo["cache_control"] = {"type": "ephemeral"}
            return

    def _turno(self):
        """Una llamada a la API, emitiendo el texto a medida que llega."""
        self._marcar_cache()
        with self.cliente.messages.stream(
            model=self.modelo,
            max_tokens=MAX_TOKENS,
            system=self.system,
            tools=HERRAMIENTAS,
            messages=self.mensajes,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": "high"},
        ) as flujo:
            for evento in flujo:
                if evento.type != "content_block_delta":
                    continue
                if evento.delta.type == "text_delta":
                    yield Evento("texto", evento.delta.text)
                elif evento.delta.type == "thinking_delta":
                    yield Evento("razonamiento", evento.delta.thinking)
            mensaje_final = flujo.get_final_message()

        self._sumar_costo(mensaje_final.usage)
        return mensaje_final

    def _sumar_costo(self, uso) -> None:
        """Acumula el gasto estimado de esta llamada."""
        precio = PRECIOS_USD.get(self.modelo)
        if precio is None:
            return

        entrada = uso.input_tokens or 0
        escritura_cache = getattr(uso, "cache_creation_input_tokens", 0) or 0
        lectura_cache = getattr(uso, "cache_read_input_tokens", 0) or 0
        salida = uso.output_tokens or 0

        costo = (
            entrada * precio["entrada"]
            + escritura_cache * precio["entrada"] * FACTOR_CACHE_ESCRITURA
            + lectura_cache * precio["entrada"] * FACTOR_CACHE_LECTURA
            + salida * precio["salida"]
        ) / 1_000_000

        self.costo_usd += costo
        self.llamadas_api += 1

    # ------------------------------------------------------------------
    def reiniciar(self) -> None:
        # El costo NO se reinicia: es el acumulado de toda la sesion.
        self.mensajes.clear()
        self.sesion.pasos.clear()
        self.sesion.dataframes.clear()
        self.sesion.figuras.clear()

    def cerrar(self) -> None:
        self.sesion.cerrar()
