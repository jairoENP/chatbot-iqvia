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
    def _turno(self):
        """Una llamada a la API, emitiendo el texto a medida que llega."""
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
            return flujo.get_final_message()

    # ------------------------------------------------------------------
    def reiniciar(self) -> None:
        self.mensajes.clear()
        self.sesion.pasos.clear()
        self.sesion.dataframes.clear()
        self.sesion.figuras.clear()

    def cerrar(self) -> None:
        self.sesion.cerrar()
