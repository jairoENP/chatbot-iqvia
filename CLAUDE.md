# CLAUDE.md — Sniper IA

Memoria operativa del proyecto. Leer antes de proponer cambios.

---

# Descripción general

**Propósito.** Chatbot que responde preguntas de negocio en español sobre el
mercado farmacéutico boliviano (datos de IQVIA), traduciéndolas a SQL y análisis
en pandas. No es un text-to-SQL de un paso: es un agente que decide en cada
pregunta si conviene SQL (agregar, rankear, share) o Python (tendencias, YoY,
CAGR, MAT, Evolution Index, gráficos), y suele encadenar ambos.

**Problema que resuelve.** Hoy responder "¿quién es mi mayor competencia en el
sub-mercado X?" exige saber que "mercado" significa `SUB_MERCADO`, que el
crecimiento se mide YoY, que `PRECIOS` no se suma, que `MARCA` viene con el
código de laboratorio pegado. Ese conocimiento vive en la cabeza de dos o tres
personas. El proyecto lo codificó en [chatbot/contexto.py](chatbot/contexto.py)
y lo puso a disposición de cualquiera que sepa hacer la pregunta.

**Usuarios.** Equipo comercial de Abbott Bolivia (pocas personas): gerentes de
marca y analistas. No necesitan saber SQL ni Power BI.

**Estado.** Desplegado y en uso en Streamlit Community Cloud. Funcional y
estable. Falta correr el set de evaluación contra la API real (ver *Estado
actual*).

---

# Stack tecnológico

| Capa | Qué se usa | Versión declarada |
|---|---|---|
| Lenguaje | Python | 3.11 local · 3.14 en Streamlit Cloud |
| Base de datos | DuckDB (embebida, archivo único) | `>=1.0` |
| LLM | API de Anthropic, `claude-opus-5-5`, effort `medium` | `anthropic>=0.60` |
| Interfaz | Streamlit | `>=1.40` |
| Gráficos | Plotly (`px`, `go`) | `>=5.20` (7.0 en la nube) |
| Datos | pandas · numpy | `>=2.0` · `>=1.26` |
| Origen | SQL Server vía pyodbc (ODBC Driver 18/17) | `pyodbc>=5.0` |
| Evaluación | PyYAML | `>=6.0` |
| Config | python-dotenv | `>=1.0` |

**Servicios externos:** API de Anthropic (único servicio al que sale data),
GitHub (repo + almacenamiento del `.duckdb`), Streamlit Community Cloud (hosting
gratuito, redeploy automático en cada push).

**Deuda conocida:** todas las dependencias usan `>=`, ninguna está fijada. Cada
reconstrucción del entorno baja la última versión disponible; un cambio
incompatible puede romper la app sin que nadie toque código. Ver *Deuda técnica*.

---

# Arquitectura

La decisión central: **el proyecto está partido en dos mitades que corren en
máquinas distintas**, porque en las computadoras de Abbott no se puede usar IA.

```
┌─ MÁQUINA DE TRABAJO (sin IA) ───────────────────────────┐
│  SQL Server BI_BOLIVIA.dwh                              │
│  ├─ IQVIA_FACT_VENTAS                                   │
│  ├─ IQVIA_DIM_PRESENTACIONES  ──► export_to_duckdb.py   │
│  └─ IQVIA_DIM_REGIONES                     │            │
└────────────────────────────────────────────┼────────────┘
                                   data/iqvia.duckdb (44 MB)
                                             │ git push
                                   GitHub jairoENP/chatbot-iqvia
                                             │ deploy automático
┌─ STREAMLIT COMMUNITY CLOUD (con IA) ───────▼────────────┐
│  app.py      UI: chat, gráficos, auditoría, costo       │
│  agent.py    bucle de conversación ◄──► API Claude      │
│  tools.py    buscar_valores · ejecutar_sql · ejec_python│
│  contexto.py reglas de negocio (system prompt)          │
│  iqvia.duckdb  archivo local del contenedor, solo lectura│
└─────────────────────────────────────────────────────────┘
```

**Flujo de una pregunta.** `app.py` captura el texto → `agent.py` lo manda a
Claude → Claude pide una herramienta → `tools.py` la ejecuta → el resultado
vuelve a Claude → repite hasta que responde (máx. 12 vueltas,
`MAX_ITERACIONES`). Cada vuelta es **una llamada facturada**, y reenvía toda la
conversación acumulada (por eso el caché, ver *Decisiones técnicas*).

**Puntos de entrada:**
- `streamlit run chatbot/app.py` — la app
- `python export/export_to_duckdb.py` — el exportador (máquina de trabajo)
- `python eval/run_eval.py` — la validación

---

# Estructura del repositorio

| Ruta | Responsabilidad |
|---|---|
| [export/export_to_duckdb.py](export/export_to_duckdb.py) | SQL Server → DuckDB. Sin IA. Filtra proyecciones, limpia `MARCA`, arma `dim_calendario`, `vw_ventas` y `meta`, verifica integridad. |
| [chatbot/app.py](chatbot/app.py) | Interfaz Streamlit. **Contiene el aislamiento por sesión** (crítico, ver decisiones). |
| [chatbot/agent.py](chatbot/agent.py) | Bucle con Claude, prompt caching, cálculo de costo. |
| [chatbot/tools.py](chatbot/tools.py) | Las 3 herramientas + clase `Sesion` (estado de una conversación). |
| [chatbot/contexto.py](chatbot/contexto.py) | **El activo real del proyecto**: esquema + diccionario de negocio + reglas. ~5.400 tokens. |
| [eval/preguntas.yaml](eval/preguntas.yaml) | 22 casos con la respuesta verdadera en SQL. |
| [eval/run_eval.py](eval/run_eval.py) | Corredor de la validación. |
| `data/iqvia.duckdb` | 44 MB, **versionado a propósito** (Streamlit Cloud lo sirve del repo). |

**Leer antes de tocar nada:** `chatbot/contexto.py` (toda regla de negocio vive
ahí, no en el código) y este archivo.

---

# Modelo de datos

**Origen (SQL Server, esquema `dwh`):** modelo estrella con
`IQVIA_FACT_VENTAS` + `IQVIA_DIM_PRESENTACIONES` + `IQVIA_DIM_REGIONES`.

**Destino (DuckDB):** `fact_ventas`, `dim_presentaciones`, `dim_regiones`,
`dim_calendario` (generada), `meta` (una fila), y **`vw_ventas`** — la vista
plana que consulta el agente en el 95% de los casos.

**Volumen actual** (corte 2026-07-01, recargado el 2026-09-28): 2.868.240 filas
· 60 meses (2021-08 → 2026-07) · 11.951 presentaciones · 4 regiones · 52
sub-mercados · 44,3 MB.

La ventana de 60 meses **rueda**: al entrar 2026-07 salió 2021-07. IQVIA reenvía
siempre los últimos 60 meses, así que el histórico no crece, se desplaza.

## Reglas de negocio que NO son evidentes en el código

Todas están escritas en `contexto.py`; acá el porqué:

1. **`MARCA` es de ancho fijo: 22 caracteres** = 19 de nombre + 3 del código de
   laboratorio (`'ENSURE ADVANCE     ABT'`). `WHERE MARCA = 'ENSURE'` devuelve
   cero filas. La vista expone `MARCA` limpia, `COD_LABORATORIO` y
   `MARCA_IQVIA` cruda. **El exportador aborta si algún valor no mide 22** —
   es el canario de que IQVIA cambió el formato.
2. **`PRECIOS` nunca se suma ni se promedia sin ponderar.** Es
   `DOLARES/UNIDADES` exacto. El precio de un conjunto es
   `SUM(DOLARES)/SUM(UNIDADES)`.
3. **La grilla es densa: ~64% de las filas son ceros.** Existe una fila por cada
   producto × región × mes aunque no haya venta. Consecuencia: `COUNT(*)` no es
   "productos que vendieron" (hay que filtrar `UNIDADES > 0`).
4. **"Mercado" = `SUB_MERCADO`** cuando se refiere a uno concreto. Excepción:
   "el mercado boliviano/total" es el universo completo. `CLASE1..4` solo si lo
   nombran explícitamente. `SUB_MERCADO` es NULL para ~84% de las presentaciones.
   **"Clase terapéutica" sin aclarar el nivel = `CLASE4`** (decisión del equipo,
   2026-09-28): es el nivel más fino y el que usan para trabajar. Los cuatro
   niveles dan respuestas distintas a la misma pregunta, así que el default tiene
   que estar fijado. Se puede mostrar otro nivel *además*, nunca en lugar de él.
5. **"El mercado" SIEMPRE incluye a Abbott.** `NOT ES_ABBOTT` es competencia, no
   mercado. En tablas Abbott-vs-mercado no se muestra fila "Resto" (confunde).
6. **Crecimiento = YoY por defecto**, nunca contra el período inmediato anterior.
7. **Competencia dentro de un sub-mercado se compara por `MARCA`, no por
   `CORPORACION`** — una corporación puede tener varias marcas en el mismo
   sub-mercado (INTI tiene ENALAPRIL e HIPOPRES en ACERDIL) y agregarlas diluye
   quién es el rival real.
8. **Ventanas:** `MTH`=1 mes, `QTR`=3, `SEM`=6, `YTD`=desde el 1-ene (variable),
   `MAT`=12 móviles.
9. **Evolution Index:** `EI = (100+crec_entidad)/(100+crec_contexto)*100`.
   >100 gana share. **El contexto cambia la conclusión** y debe declararse
   siempre (ACERDIL D: EI 105,6 vs su sub-mercado, 97,2 vs su molécula).
10. **Solo datos reales.** `ES_PROYECCION = 1` queda fuera. IQVIA entrega con
    atraso: el último mes disponible no es el mes actual.

## Cambios de esquema y ciclo de recarga

El exportador **reconstruye el archivo entero**, no es incremental. No hace
`TRUNCATE` ni recrea tabla por tabla: la base nace de cero en cada corrida, así
que nada del mes anterior sobrevive. Eso encaja con que IQVIA reenvía los 60
meses completos cada mes, y evita que el archivo se infle (no necesita `VACUUM`).

Los `CREATE OR REPLACE TABLE` que hay en el código son redundantes en la
práctica —siempre corren sobre una base recién creada— pero dejan el script
correcto si alguien cambia el mecanismo.

**La escritura es atómica.** Se escribe a `data/iqvia.duckdb.nuevo` y el archivo
bueno se reemplaza (`Path.replace()`) recién al final, con `verificar()` ya
pasado. Una corrida fallida deja el `.duckdb` anterior **intacto y usable**. El
`.nuevo` queda en disco para inspeccionarlo y la corrida siguiente lo descarta
(está en `.gitignore`).

Si IQVIA cambia el ancho de `MARCA` o el valor de `CORPORACION`, `verificar()`
corta con `sys.exit()` y el archivo bueno ni se toca.

**Al recargar datos nuevos hay que recalcular el set de evaluación**
(`python eval/run_eval.py --recalcular`): los valores esperados están atados a
la fecha de corte.

---

# Decisiones técnicas

### 1. Dos mitades en máquinas distintas
- **Motivo:** las computadoras de Abbott no permiten IA ni instaladores `.exe`.
- **Alternativas:** todo en la nube (rompe la restricción); todo local (no
  compartible).
- **Consecuencia:** un paso extra mensual (regenerar y subir el `.duckdb`).
- **No cambiar sin analizar:** es la razón de ser de la arquitectura.

### 2. DuckDB en vez de SQL Server remoto o Postgres
- **Motivo:** base embebida, se instala con `pip` (sin `.exe`), un archivo
  portable de 44 MB, consultas de 17-31 ms sobre 2,87M filas.
- **Consecuencia:** el archivo viaja en el repo. Solo lectura, sin concurrencia
  de escritura.
- **MotherDuck fue evaluado y descartado:** metería datos licenciados de IQVIA
  en un tercero, y no resuelve el problema real (el archivo en repo público).

### 3. Vista plana `vw_ventas` en vez de que el LLM arme JOINs
- **Motivo:** un LLM comete muchos más errores sobre un esquema estrella que
  sobre una tabla ancha.
- **Consecuencia:** el 95% de las consultas no tienen JOIN. Las tablas base
  siguen disponibles.

### 4. Se conservan las filas en cero
- **Motivo:** sin ellas, un producto que no vendió desaparece de los listados y
  su serie queda con huecos en vez de ceros (rompe medias móviles y YoY).
- **Alternativa descartada:** `--excluir-ceros` existe como flag opcional, no es
  el default. DuckDB comprime los ceros casi por completo.
- **No cambiar:** el usuario identificó explícitamente esta consecuencia.

### 5. Reglas de negocio en el system prompt, no en el código
- **Motivo:** ajustar comportamiento sin tocar lógica; el conocimiento queda
  legible y auditable en un solo archivo.
- **Consecuencia:** el prompt creció de ~2.100 a ~5.400 tokens. Va cacheado.
- **Cada regla nació de un error real observado en producción.**

### 6. Prompt caching en dos puntos
- **Motivo:** la API no tiene memoria; cada vuelta del bucle reenvía todo. Una
  pregunta de 8 pasos pagaba el historial 8 veces (~USD 0,40/pregunta).
- **Implementación:** el system prompt (fijo) + un punto móvil sobre el último
  mensaje de usuario (`_marcar_cache()` en agent.py).
- **Consecuencia:** ~75% menos costo de entrada. El caché vence a los 5 minutos
  de inactividad.
- **Ojo con el multiplicador de lectura:** es 0,1x en casi todos los modelos
  pero **0,05x en Opus 5.5**. Por eso vive en `PRECIOS_USD[modelo]`, no en una
  constante global.

### 7. Opus 5.5 con esfuerzo `medium`
- **Motivo:** medido contra Opus 5 / `high` sobre 7 casos de evaluación
  (2026-09-27): **mismos 7/7 aciertos, 52% menos costo (USD 0,35 vs 0,74) y
  35% menos tiempo (122s vs 188s)**, con menos llamadas a herramientas. Opus 5.5
  además cuesta 4/20 por millón en vez de 5/25.
- **Cifras idénticas** en los dos: EI ACERDIL D 106, TOTAL 449.382, PVM JUNIOR
  199.987 / 13,4% / EI 137, crecimiento QTR +0,8%.
- **Consecuencia observada:** con `medium` las respuestas son a veces menos
  exhaustivas (en `evolution_index` mostró 2 filas en vez de las 5 marcas del
  sub-mercado y no señaló al rival real). No fue sistemático — en
  `mercado_es_submercado` pasó lo contrario. **Si reaparece, subir a `high`
  antes de cambiar de modelo.**
- **Implementación:** `MODELO` y `ESFUERZO` en agent.py; el esfuerzo es
  parámetro del `Agente` (`esfuerzo=`) para poder comparar sin editar código.

### 8. Un Agente por sesión, conexión compartida
- **Motivo:** `@st.cache_resource` devolvía el MISMO `Agente` a todos los
  usuarios. Las conversaciones se cruzaban **en silencio**: cada uno veía su chat
  pero Claude recibía las dos mezcladas.
- **Implementación:** la conexión DuckDB sí se comparte (solo lectura, evita
  abrir 44 MB por usuario); cada sesión toma su `.cursor()` (una conexión no es
  thread-safe y Streamlit corre cada sesión en un hilo); el `Agente` vive en
  `st.session_state`.
- **NO volver a poner `@st.cache_resource` sobre el Agente.**

### 9. El exportador escribe de forma atómica
- **Motivo:** antes se borraba `data/iqvia.duckdb` **antes** de intentar conectar
  a SQL Server. Si la conexión fallaba (VPN caída, credenciales vencidas, driver
  ODBC ausente) o si `verificar()` no pasaba, el archivo anterior —que
  funcionaba— ya estaba destruido. Lo único que lo salvaba era que también está
  versionado en git, una red de seguridad accidental.
- **Implementación:** se escribe a `<salida>.nuevo` y se hace
  `temporal.replace(args.salida)` recién después de `verificar()`.
- **Verificado:** una corrida con `SQLSERVER_HOST` inválido deja el archivo
  anterior sin tocar.

### 10. Plotly en vez de matplotlib
- **Motivo:** gráficos interactivos (hover con el valor exacto), mejor
  integración con Streamlit.
- **Consecuencia:** `st.plotly_chart` deriva el id del contenido, así que dos
  figuras iguales colisionan → **cada gráfico necesita `key=` explícita**.

### 11. Seguridad del sandbox
- Conexión DuckDB en `read_only=True` (garantía real, no un filtro de texto).
- `ejecutar_sql` solo acepta `SELECT`/`WITH`; bloquea INSERT/UPDATE/DELETE/DROP.
- `ejecutar_python` usa `exec()` con builtins restringidos: sin `os`,
  `subprocess`, `open` ni red. Solo pandas, numpy, plotly.

---

# Historial funcional

**Implementado y funcionando:**
- Exportador completo con verificación de integridad y control por año.
- Agente con 3 herramientas, streaming, thinking adaptativo (`effort: high`).
- Interfaz con auditoría (paneles de SQL/Python expandibles), gráficos
  interactivos y contador de gasto.
- 22 casos de evaluación con verdad calculada en SQL.

**Problemas resueltos (cada uno dejó una regla o un fix):**

| Problema | Solución |
|---|---|
| `MARCA` con código de lab pegado → 0 filas | `TRIM(LEFT(MARCA,19))` + validación de ancho 22 |
| `Series.to_string()` no acepta `max_colwidth` | `isinstance` check en `_tabla()` |
| Excluir ceros rompía series y listados | Default pasó a conservarlos |
| Tablas Abbott vs mercado con fila "Resto" ambigua | Regla: solo ABBOTT y MERCADO |
| Gráficos con doble eje Y engañosos | Prohibido `secondary_y`; base 100 o paneles |
| `$` en el texto → Streamlit lo renderiza como LaTeX | Regla: usar "USD", nunca `$` |
| `StreamlitDuplicateElementId` con Plotly | `key=` explícita por figura + dedup por identidad |
| Costo ~USD 0,40/pregunta | Caché del historial → ~USD 0,12 |
| Un `FALLA` falso: `no_debe_contener: "1,4"` matcheaba dentro de "71,4 M" | El valor prohibido lleva el `%` pegado |
| Una llamada Python por cada ventana de tiempo | Regla: consolidar en una sola llamada |
| Formato de tabla no se aplicaba en preguntas abiertas | Regla atada al **output**, no a cómo se pregunta |
| Leyendas de subplots mezcladas a la derecha | Una leyenda por panel (`legend`/`legend2`) |
| **Conversaciones cruzadas entre usuarios** | Agente por sesión (ver decisión 8) |
| "Clase terapéutica" daba 4 respuestas según el nivel ATC | Default fijado en `CLASE4` |
| El set de evaluación se rompía en cada recarga de datos | Fechas derivadas de `meta.FECHA_CORTE` |
| Un `FALLA` falso: el bot decía "no puedo darte" y la lista pedía "no puedo responder" | `SENALES_NEGATIVAS` ampliada |
| Un corte de la API (crédito agotado) daba un resumen idéntico a una regresión | Marca `APIERR` y aviso de que la corrida no sirve |
| Un `FALLA` falso: se esperaba "106" y el bot redondeó a "107,0 M" | El caso acepta ambos; lo que discrimina es el orden de magnitud |
| Los ejemplos de la barra lateral eran texto muerto y escondían las capacidades | Botones clickeables, etiqueta corta + pregunta completa |
| Gráfico de pérdidas con eje negativo y la etiqueta de la barra más larga cortada | Regla: magnitudes en positivo, `textposition='inside'` |

**Descartado:** RAG (los datos son estructurados, SQL es la herramienta
correcta); memory tool autónomo (las reglas deben pasar por revisión humana);
MotherDuck; migrar a `pyproject.toml` (riesgo sin beneficio urgente).

---

# Estado actual

**Terminado:** exportador, agente, interfaz, aislamiento por sesión, caché,
las 3 herramientas, el diccionario de negocio, deploy automático.

**Terminado también:** el **2026-09-28 se corrió el set completo (22 casos)
contra la API real por primera vez**, al corte 2026-07 y con Opus 5.5 /
`medium`. Resultado final **22/22**, de los cuales 6 son de revisión manual.

La corrida del **2026-10-02** (tras fijar `CLASE4`) dio **22/22**, confirmando
que la regla nueva no causó regresiones. Su única falla inicial fue otro falso
positivo del arnés: se esperaba `"106"` y el bot contestó `"Bs 107,0 millones"`
—correcto, la verdad es 106.950.945— así que el caso ahora acepta ambos.

Las 3 fallas de la primera pasada tampoco eran del bot:
- `fuera_de_rango` — el bot respondió *"No puedo darte la venta de enero de
  2027: ese mes todavía no está en la base"*, impecable, pero `SENALES_NEGATIVAS`
  solo tenía `"no puedo responder"`. Lista ampliada.
- `competencia_submercado` — esperaba `PROMEDICAL`, pero existen ENSURE y ENSURE
  ADVANCE, así que la regla de ambigüedad manda **preguntar**. El caso
  contradecía una regla propia; pasó a revisión manual. La conducta de fondo la
  cubre `mercado_es_submercado`, donde PEDIASURE no tiene hermanos de nombre.
- `clase_terapeutica` — ver la pregunta de negocio abierta más abajo.

**Pendiente / riesgos:**
- ⚠️ **El repo es público y contiene `data/iqvia.duckdb`** con 2,86M filas de
  datos licenciados de IQVIA, descargables por cualquiera. Se hizo público
  porque Streamlit no veía el repo privado; el permiso correcto está en
  *Installed GitHub Apps*, no en *Authorized OAuth Apps*. **Pendiente de
  decisión del usuario.**
- ⚠️ **Credenciales expuestas en el chat de desarrollo:** contraseña de SQL
  Server y `ANTHROPIC_API_KEY`. **Ambas deberían rotarse.** Sin confirmar que se
  haya hecho.
- La app de Streamlit también es pública (cualquiera con el link gasta crédito).
- `SQLAlchemy` está en `requirements.txt` pero **no se importa en ningún
  archivo** — el exportador usa `pyodbc` directo. Dependencia muerta.
- Dependencias sin fijar (`>=`): riesgo de romperse solo.
- El `.duckdb` se reemplaza entero cada mes → el repo crece ~44 MB por
  actualización (~500 MB/año). Eventualmente, Git LFS.

**Contradicciones documentación ↔ implementación:**
1. `README.md:23,49,51` describe copiar el `.duckdb` por OneDrive/USB a una
   "máquina personal". El deploy real es GitHub → Streamlit Cloud. El README
   **no menciona GitHub ni Streamlit Cloud**.
2. `app.py:76-79`: el mensaje de error dice "copialo a `data/`", instrucción
   válida solo para el escenario local.
3. `README.md` dice "15 casos" en el set de evaluación; hay **22**.

---

# Convenciones

- **Idioma:** todo en español (código, comentarios, commits, respuestas del
  bot). Identificadores sin tildes ni `ñ` para evitar problemas de encoding.
- **Nombres:** funciones y variables en `snake_case` español (`crear_vista_plana`,
  `fecha_corte`, `buscar_valores`). Constantes en `MAYUSCULAS`.
- **Comentarios:** explican el **porqué**, no el qué. Densidad alta en las
  decisiones no obvias (ver los comentarios sobre `MARCA`, los ceros, el caché).
- **Consultas:** SQL en strings multilínea, indentado. El agente usa `vw_ventas`;
  las tablas base solo para casos raros.
- **Errores:** las herramientas devuelven strings que empiezan con `"ERROR"` en
  vez de lanzar excepciones — el modelo los lee y corrige. El exportador usa
  `sys.exit()` con mensaje explicativo ante fallo de integridad.
- **Logging:** no hay librería de logging; el exportador imprime progreso a
  stdout, la app muestra los pasos en expanders.
- **Seguridad:** credenciales solo en `.env` (gitignored). Conexión read-only.
  Sandbox de Python restringido.
- **Commits:** título corto en imperativo + cuerpo explicando el porqué y el
  impacto. Sin `Co-Authored-By` en este repo.

---

# Comandos frecuentes

```bash
# --- Máquina de TRABAJO ---
pip install duckdb pandas python-dotenv pyodbc
cp .env.example .env                      # completar credenciales SQL Server
python export/export_to_duckdb.py         # regenera data/iqvia.duckdb
python export/export_to_duckdb.py --meses 36        # acotar historia
python export/export_to_duckdb.py --excluir-ceros   # achicar (no recomendado)

# --- Chatbot (local) ---
pip install -r requirements.txt
streamlit run chatbot/app.py

# --- Validación ---
python eval/run_eval.py --recalcular      # solo verdades SQL, sin gastar API
python eval/run_eval.py                   # los 22 casos contra el agente
python eval/run_eval.py --caso evolution_index

# --- Verificación rápida tras un cambio ---
python -m compileall -q chatbot export eval
python -c "import yaml; yaml.safe_load(open('eval/preguntas.yaml', encoding='utf-8'))"
python -c "import sys; sys.path.insert(0,'chatbot'); from contexto import *; \
  p=construir_system_prompt(leer_meta()); print(f'{len(p):,} chars')"

# --- Deploy ---
git add . && git commit -m "..." && git push    # Streamlit Cloud redeploya solo
```

No hay linter, formateador ni suite de tests unitarios configurados.

---

# Configuración y variables de entorno

Archivo `.env` en la raíz (gitignored). Plantilla en
[.env.example](.env.example). Cargado con `load_dotenv()` desde
`export_to_duckdb.py:39` y `agent.py:51`.

| Variable | Para qué | Obligatoria en |
|---|---|---|
| `SQLSERVER_HOST` | Servidor y puerto (`WQ00408D,50009`) | Máquina de trabajo |
| `SQLSERVER_DATABASE` | `BI_BOLIVIA` | Máquina de trabajo |
| `SQLSERVER_USER` | Usuario ODBC | Máquina de trabajo |
| `SQLSERVER_PASSWORD` | Contraseña | Máquina de trabajo |
| `ANTHROPIC_API_KEY` | Acceso a la API de Claude | Chatbot |

**En Streamlit Cloud** la API key va en el panel de **Secrets** (formato TOML).
Streamlit la expone también como variable de entorno, por eso `os.getenv()`
funciona sin cambios. No se usa `st.secrets` a propósito: acoplaría `agent.py` a
Streamlit y rompería `run_eval.py` desde terminal.

---

# Pruebas y validación

**Qué existe:** [eval/preguntas.yaml](eval/preguntas.yaml), 22 casos. Cada uno
trae la pregunta, qué se está probando, y la consulta SQL que produce la
respuesta verdadera. `run_eval.py` corre la pregunta contra el agente y muestra
ambas lado a lado.

**Mecanismo de revisión** (deliberadamente simple): `debe_contener`,
`no_debe_contener`, `modo_contener: cualquiera`, `espera_negativa`. Normaliza
acentos y separadores de miles. Un `FALLA` siempre merece atención; un `OK`
automático no garantiza que la respuesta sea buena.

**Casos críticos que no deben romperse** (valores al corte 2026-07):
- `precio_promedio` — trampa del precio ponderado (29,22)
- `conteo_presentaciones` — trampa de la grilla densa (117, no 968)
- `crecimiento_qtr_yoy` — YoY (-6,5%), no QoQ (-8,9%)
- `evolution_index` / `evolution_index_contexto_cambia` — 98,2 vs 93,2
- `fuera_de_rango` / `molecula_inexistente` — debe decir que no tiene el dato
- `mercado_es_submercado` — "mercado" = SUB_MERCADO
- `clase_terapeutica` — default `CLASE4` (LECHES PARA NINOS), no `CLASE1`

Al corte 2026-06 el par de Evolution Index mostraba la conclusión
**invirtiéndose** (105,6 ganaba share contra el sub-mercado, 97,2 la perdía
contra la molécula). Con los datos de julio ACERDIL D pierde share en los dos
contextos (98,2 y 93,2): lo que el caso prueba es la **brecha** entre contextos,
no que uno cruce el 100.

**Limitaciones:** varios casos nuevos no tienen verificación automática confiable
(formato de tabla, si preguntó ante ambigüedad). Los valores esperados
estaban calculados al corte **2026-06** y **quedaron obsoletos** con la recarga
al corte 2026-07 del 2026-09-28. Ver el riesgo en *Estado actual*.

**Después de cada cambio:** `compileall`, validar el YAML si se tocó, y
`--recalcular` si se tocaron reglas de negocio.

---

# Reglas para futuras conversaciones

1. **Leer este CLAUDE.md antes de proponer cambios.**
2. **Revisar el código relacionado antes de modificarlo.** Las reglas de negocio
   están en `contexto.py`, no en la lógica.
3. **No asumir que algo funciona sin validarlo.** Este proyecto tiene datos
   reales disponibles: verificá contra `data/iqvia.duckdb` en vez de estimar.
4. **No cambiar arquitectura, contratos, tablas ni interfaces sin explicar el
   impacto** — en particular: la separación en dos mitades, `vw_ventas`, el
   aislamiento por sesión, el read-only.
5. **Mantener compatibilidad** con la implementación existente.
6. **Ejecutar las validaciones disponibles** tras cada cambio (ver *Comandos*).
7. **Indicar qué archivos fueron modificados** y por qué.
8. **No eliminar código aparentemente obsoleto** sin comprobar dependencias.
9. **Preguntar solo ante ambigüedad real** que no se resuelva leyendo el repo.
10. **Actualizar este CLAUDE.md** cuando se tome una decisión importante o cambie
    el estado del proyecto.
11. **Toda regla de negocio nueva va a `contexto.py` + un caso en
    `preguntas.yaml`** con su verdad calculada en SQL.

---

# Próximo paso recomendado

**Acción:** sincronizar la documentación con el deploy real y sacar la
dependencia muerta.

**Por qué es el próximo paso:** son las últimas incoherencias conocidas entre
lo que dice el repo y lo que hace. Ninguna rompe nada hoy, pero desorientan a
quien llegue nuevo (incluido Claude en una conversación futura).

**Qué hay que tocar:**
- `README.md` — todavía describe mover el `.duckdb` por OneDrive o USB a una
  "máquina personal". El deploy real es GitHub → Streamlit Cloud, y el README
  no lo menciona.
- `chatbot/app.py:76-79` — el mensaje de error dice "copialo a `data/`",
  instrucción válida solo para el escenario local.
- `requirements.txt` — `SQLAlchemy` no se importa en ningún archivo; el
  exportador usa `pyodbc` directo.

**Criterios de terminado:** el README describe el deploy real, el mensaje de
error no manda copiar archivos a mano, y `pip install -r requirements.txt` no
baja nada que no se use.

**Decisiones del usuario que siguen pendientes** (no técnicas): la exposición
del repo público y la rotación de credenciales.

---

# Registro de decisiones

| Fecha | Decisión | Motivo | Archivos | Estado |
|---|---|---|---|---|
| 2026-08-25 | Arquitectura en dos mitades | No se puede usar IA en máquinas de Abbott | todo | Vigente |
| 2026-08-25 | DuckDB embebido, archivo portable | Instalable con pip, sin servidor | `export/`, `chatbot/tools.py` | Vigente |
| 2026-08-25 | Vista plana `vw_ventas` | Reduce errores de JOIN del LLM | `export_to_duckdb.py` | Vigente |
| 2026-08-25 | Conservar filas en cero | Sin ellas se rompen series y listados | `export_to_duckdb.py` | Vigente |
| 2026-08-25 | Limpiar `MARCA` (ancho fijo 22) | `WHERE MARCA='ENSURE'` daba 0 filas | `export_to_duckdb.py` | Vigente |
| 2026-08-26 | Repo en GitHub + Streamlit Cloud | El equipo lo usa por navegador | deploy | Vigente |
| 2026-08-26 | Repo **público** | Streamlit no veía el privado | — | ⚠️ A revisar |
| 2026-08-26 | Reglas: mercado incluye Abbott, sin fila "Resto" | Confusión real del usuario | `contexto.py` | Vigente |
| 2026-08-26 | matplotlib → Plotly | Gráficos interactivos | `tools.py`, `app.py` | Vigente |
| 2026-08-26 | Caché del historial + contador de costo | USD 0,40 → 0,12 por pregunta | `agent.py`, `app.py` | Vigente |
| 2026-08-27 | Minimizar llamadas a herramientas | Una llamada por ventana era desperdicio | `contexto.py` | Vigente |
| 2026-08-27 | Preguntar ante ambigüedad | Elegía por su cuenta y avisaba tarde | `contexto.py` | Vigente |
| 2026-08-27 | Prohibir `$` en el texto | Streamlit lo renderiza como LaTeX | `contexto.py` | Vigente |
| 2026-08-27 | Tabla: orden MAT→MTH, prefijo `%G` | "MAT" junto a "MAT USD" era ambiguo | `contexto.py` | Vigente |
| 2026-08-27 | Evolution Index + columnas `EI` | Métrica estándar del equipo | `contexto.py`, `preguntas.yaml` | Vigente |
| 2026-08-28 | Formato atado al output, no a la pregunta | No aplicaba en preguntas abiertas | `contexto.py` | Vigente |
| 2026-09-02 | Rename a "Sniper IA" (🎯) | Decisión del usuario | `app.py`, `README.md` | Vigente |
| 2026-09-02 | Una leyenda por panel + leyenda obligatoria | Leyendas mezcladas / ausentes | `contexto.py` | Vigente |
| 2026-09-10 | Agente por sesión | Las conversaciones se cruzaban en silencio | `app.py`, `agent.py`, `tools.py` | Vigente |
| 2026-09-22 | MotherDuck descartado | No resuelve el problema real y suma un tercero | — | Vigente |
| 2026-09-27 | Opus 5 `high` → Opus 5.5 `medium` | Medido: 7/7 igual, 52% menos costo, 35% menos tiempo | `agent.py` | Vigente |
| 2026-09-27 | Factor de caché por modelo (0,05x en Opus 5.5) | Una constante global sobreestimaba el gasto | `agent.py` | Vigente |
| 2026-09-27 | Valores de `no_debe_contener` con `%` pegado | Un "1,4" pelado matcheaba dentro de "71,4 M" | `preguntas.yaml` | Vigente |
| 2026-09-28 | Costo por pregunta al pie de cada respuesta | El acumulado escondía qué turno costaba más | `app.py` | Vigente |
| 2026-09-28 | Exportador con escritura atómica | Una corrida fallida destruía el `.duckdb` anterior | `export_to_duckdb.py`, `.gitignore` | Vigente |
| 2026-09-28 | Recarga al corte 2026-07 | Llegaron los datos de julio | `data/iqvia.duckdb` | Vigente |
| 2026-09-28 | Fechas del set derivadas de `meta.FECHA_CORTE` | 35 literales rompían el set en cada recarga | `preguntas.yaml`, `run_eval.py` | Vigente |
| 2026-09-28 | Marcadores `{ultimo_mes}` / `{mes_futuro}` en las preguntas | Dos casos tenían el mes escrito a mano | `run_eval.py` | Vigente |
| 2026-09-28 | `SENALES_NEGATIVAS` ampliada | Marcaba FALLA un "no puedo darte" correcto | `run_eval.py` | Vigente |
| 2026-09-28 | Primera corrida completa: 22/22 | Validación end-to-end del agente | — | Vigente |
| 2026-09-28 | "Clase terapéutica" por defecto = `CLASE4` | Decisión del equipo: es el nivel que usan para trabajar | `contexto.py`, `preguntas.yaml` | Vigente |
| 2026-09-28 | El set distingue error de API de fallo de calidad | Un crédito agotado se leía como regresión del agente | `run_eval.py` | Vigente |
| 2026-10-02 | Corrida 22/22 tras la regla `CLASE4` | Confirmar que no hubo regresión | — | Vigente |
| 2026-10-02 | Ejemplos como botones + lista renovada | El texto estático escondía EI, tablas y gráficos | `app.py` | Vigente |
| 2026-10-02 | Las cinco preguntas del equipo como ejemplos (luego cuatro) | Son las que hacen de verdad; la de "crecimiento inferior" partía de una premisa falsa en el MAT | `app.py` | Vigente |
| 2026-10-02 | Pérdidas en positivo + etiquetas que no se cortan | Eje de −600 a −100 y rótulo cortado en la barra más importante | `contexto.py` | Vigente |
