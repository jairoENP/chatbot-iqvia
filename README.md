# Analista IQVIA — chatbot sobre el mercado farmacéutico boliviano

Chat en lenguaje natural sobre los datos de IQVIA que hoy viven en el modelo
estrella de SQL Server (`BI_BOLIVIA.dwh`). Permite preguntar cosas como
"¿cuál es el producto que más se vende de la molécula paracetamol?" o
"en el sub-mercado de Ensure, ¿quién es mi mayor competencia?".

No es un traductor de texto a SQL: es un agente que decide en cada pregunta si
conviene **SQL** (agregar, rankear, calcular participación) o **Python/pandas**
(tendencias, YoY, CAGR, medias móviles, MAT, proyecciones), y suele encadenar
ambos.

## Las dos mitades

En las computadoras de trabajo no se puede usar IA, y los datos no pueden salir
a la nube. Por eso el proyecto está partido:

| | Máquina | Usa IA | Qué hace |
|---|---|---|---|
| `export/` | Trabajo | No | Lee SQL Server → escribe `data/iqvia.duckdb` |
| `chatbot/` | Personal | Sí | Lee el `.duckdb` → responde con Claude |

El archivo `.duckdb` pesa ~43 MB y viaja por OneDrive o USB. DuckDB se instala
con `pip`, sin instaladores `.exe`. Nada se sube a ningún servicio externo salvo
las preguntas y los resultados de las consultas, que van a la API de Claude.

## Puesta en marcha

### 1. Máquina de trabajo — generar la base

```bash
pip install duckdb pandas python-dotenv pyodbc
cp .env.example .env        # y completar las credenciales de SQL Server
python export/export_to_duckdb.py
```

Trae las 3 tablas del modelo estrella, arma el calendario y la vista plana, y
verifica la integridad. Tarda ~2 minutos. Al final imprime un control de
dólares y unidades por año: **contrastarlo contra Power BI** antes de confiar
en el archivo.

Opciones útiles:

```bash
python export/export_to_duckdb.py --meses 36        # acotar el histórico
python export/export_to_duckdb.py --excluir-ceros   # achicar el archivo (ver abajo)
```

Copiar `data/iqvia.duckdb` a la máquina personal.

### 2. Máquina personal — levantar el chat

```bash
pip install -r requirements.txt
# agregar ANTHROPIC_API_KEY al .env (se saca de console.anthropic.com)
streamlit run chatbot/app.py
```

### 3. Cada mes

Cuando llegan los flat files de IQVIA y se hace la carga habitual a SQL Server,
volver a correr el exportador y reemplazar el `.duckdb`. Un solo paso extra al
proceso actual.

## Validación

```bash
python eval/run_eval.py --recalcular   # solo las verdades de SQL, sin gastar API
python eval/run_eval.py                # corre las 15 preguntas contra el agente
```

`eval/preguntas.yaml` tiene 15 casos con la respuesta correcta calculada a mano
en SQL, incluidas las trampas conocidas (precio ponderado, grilla con ceros,
molécula inexistente, período futuro). **Correrlo después de cada carga
mensual**: es lo que separa una herramienta confiable de una demo.

## Decisiones que conviene conocer

**Se conservan las filas en cero.** La tabla de hechos es una grilla densa
producto × región × mes: el 64% de las filas no tiene ventas. Excluirlas
achicaría el archivo, pero un producto que no vendió desaparecería de los
listados del sub-mercado, y su serie mensual quedaría con huecos en vez de
ceros. DuckDB comprime esos ceros casi por completo (43 MB con las 2,86M de
filas), así que no cuesta nada tenerlos.

**`MARCA` viene limpia.** En origen es un campo de ancho fijo de 22 caracteres:
19 para el nombre más 3 para el código de laboratorio (`'ENSURE ADVANCE     ABT'`).
La vista expone el nombre limpio en `MARCA`, el código en `COD_LABORATORIO` y el
valor original en `MARCA_IQVIA`. El exportador aborta si IQVIA cambia ese ancho.

**`PRECIOS` nunca se suma.** Es el precio unitario promedio, exactamente
`DOLARES/UNIDADES`. El precio de un conjunto se calcula
`SUM(DOLARES)/SUM(UNIDADES)`.

**"Mercado" significa `SUB_MERCADO`.** Es la convención del equipo: cuando
alguien pregunta por *un* mercado ("el mercado de Pediasure", "en este mercado"),
el bot filtra por `SUB_MERCADO`, la clasificación propia de Abbott que agrupa una
marca con sus rivales. Única excepción: "el mercado boliviano" o "el mercado
total" es el universo completo. Las clases terapéuticas de IQVIA (`CLASE1`–
`CLASE4`) solo se usan si se las nombra explícitamente.

**La métrica por defecto es DOLARES.** Cambia solo si se la nombra:
"unidades"/"und" → `UNIDADES`; "bolivianos"/"bob"/"bs"/"bs." → `BOLIVIANOS`.
El bot siempre aclara en qué métrica está respondiendo.

**Solo datos reales.** Las filas con `ES_PROYECCION = 1` quedan fuera. IQVIA
entrega con atraso, así que el último mes disponible no es el mes actual; la
fecha de corte está en la tabla `meta` y se muestra en la barra lateral.

## Estructura

```
export/export_to_duckdb.py   SQL Server → DuckDB (sin IA)
chatbot/contexto.py          esquema + diccionario de negocio (system prompt)
chatbot/tools.py             buscar_valores · ejecutar_sql · ejecutar_python
chatbot/agent.py             bucle de conversación con Claude
chatbot/app.py               interfaz Streamlit
eval/preguntas.yaml          15 casos con su respuesta verdadera
eval/run_eval.py             corredor de la validación
```

### Las tres herramientas del agente

- **`buscar_valores`** — resuelve cómo está escrito realmente un valor antes de
  filtrarlo. Sin esto, "paracetamol" contra `MOLECULA` (que está en inglés y
  mayúsculas) devuelve cero filas sin avisar, que es peor que un error.
- **`ejecutar_sql`** — solo SELECT, sobre una conexión abierta en modo lectura.
  Guarda el DataFrame completo como `df_1`, `df_2`… El modelo ve solo las
  primeras filas.
- **`ejecutar_python`** — pandas, numpy y Plotly (gráficos interactivos) con
  esos DataFrames ya cargados. Sin acceso al sistema de archivos ni a la red.

Toda respuesta numérica es auditable: la interfaz muestra el SQL y el Python que
se ejecutaron.

## Seguridad

- Las credenciales van en `.env`, que está en `.gitignore`. Nunca en el código.
- La conexión a DuckDB del chatbot es de **solo lectura**; el agente no puede
  modificar los datos.
- `ejecutar_python` usa `exec()` con builtins restringidos, sin `os`,
  `subprocess`, `open` ni red. Corre local, sobre código generado a partir de
  las preguntas del propio usuario.
- Las preguntas y los resultados de las consultas se envían a la API de Claude.
  Los datos crudos completos nunca salen de la máquina.
