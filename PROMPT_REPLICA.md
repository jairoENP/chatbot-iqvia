# Prompt para replicar Sniper IA en otra sesión

Pegá en una sesión nueva de Claude Code todo lo que está entre las dos líneas.
Si lo vas a usar sobre otros datos, lo único que hay que reescribir son los
bloques **LOS DATOS**, **LA RESTRICCIÓN** y **QUÉ QUIERO QUE SEA**: el resto es
independiente del dominio.

El **apéndice** del final no va en el prompt inicial. Pasáselo más adelante,
cuando el exportador ya ande: es la lista de trampas que ya pagamos.

---

Quiero hacer un chatbot que responda preguntas de negocio en español sobre el
mercado farmacéutico boliviano, con datos de IQVIA. Los usuarios son el equipo
comercial de Abbott Bolivia —gerentes de marca y analistas— que no saben SQL ni
quieren aprender.

## LOS DATOS

Están en SQL Server, base BI_BOLIVIA, esquema dwh, en modelo estrella:
IQVIA_FACT_VENTAS + IQVIA_DIM_PRESENTACIONES + IQVIA_DIM_REGIONES. Son 60 meses
de historia, ~2,9 millones de filas, 12.000 presentaciones, 4 regiones, 52
submercados.

IQVIA reenvía los últimos 60 meses cada mes, así que la ventana rueda: el
histórico no crece, se desplaza. Y entrega con atraso: el último mes disponible
nunca es el mes actual. Hay además filas de proyección que no quiero en el
análisis, solo datos reales.

## LA RESTRICCIÓN

En las computadoras de Abbott no se puede usar IA ni instalar `.exe`, pero son
las únicas que ven SQL Server. Así que el proyecto va partido en dos mitades que
corren en máquinas distintas: un exportador que pasa SQL Server a un archivo
DuckDB, sin nada de IA, y una app de Streamlit en la nube que usa ese archivo y
llama a la API de Claude. El archivo viaja por GitHub.

## EL STACK

Python, DuckDB, Streamlit, Plotly, pandas, pyodbc para el origen. API de
Anthropic con `claude-sonnet-5-5` y effort `medium`. Fijá las versiones con `==`,
no con `>=`: un `>=` baja la última versión en cada reconstrucción del entorno y
la app se puede romper sin que nadie toque código.

## QUÉ QUIERO QUE SEA

No un text-to-SQL de un paso. Un agente con tres herramientas —buscar valores,
ejecutar SQL, ejecutar Python— que decida en cada pregunta qué usar y pueda
encadenarlas, hasta unas doce vueltas. Las preguntas reales son "¿cómo estamos vs
el mercado?" o "¿en qué submercados vamos mal y por qué?", así que necesita
agregar en SQL, calcular crecimientos y Evolution Index en pandas, y a veces
graficar.

La búsqueda de valores existe para que el modelo no adivine cómo se escribe una
marca: busca el texto en las columnas categóricas y devuelve los valores reales
de la base.

Un detalle que importa para el costo: la herramienta de SQL le manda al modelo
solo las primeras 40 filas, y guarda el DataFrame completo en memoria con un
nombre (`df_1`, `df_2`...) para que Python lo retome. Así una consulta de 5.000
filas no se paga como texto.

## CÓMO QUIERO QUE RESPONDA

Empezando por la conclusión, no por los datos. Si pregunto "¿dónde vamos mal?",
no quiero una tabla de 52 submercados ordenada por crecimiento: quiero que me
diga cuál es el problema, de qué tamaño es en dólares, y qué lo explica. La tabla
va abajo, como respaldo de lo que ya me afirmaste.

Cuando pregunte "por qué", quiero que descomponga, no que repita la cifra con
otras palabras. Una caída en dólares es precio, volumen, mezcla de productos o
pérdida de participación, y cada una lleva a una decisión distinta. Si el dato no
permite separarlas, que lo diga.

Cuando una cifra dependa de contra qué se compara, que declare la referencia en
el texto. El Evolution Index de una marca contra su submercado y contra su
molécula dan números distintos y a veces conclusiones opuestas.

Que avise cuando la base sea chica. En marcas de poco volumen los crecimientos
porcentuales y el Evolution Index se dan vuelta solos de un mes al otro; un 138%
sobre 40 unidades no es una tendencia. Mejor que lo señale a que yo lo descubra
en una reunión.

Que distinga pérdida de participación de pérdida de caja. Son cosas distintas y
el equipo actúa distinto según cuál sea.

Que pregunte en vez de elegir cuando la pregunta sea ambigua. Si pido "ENSURE" y
en la base hay ENSURE y ENSURE ADVANCE, que me pregunte cuál, no que elija una y
me avise al final.

Que diga siempre hasta qué mes llegan los datos, para que nadie lea una cifra
como si fuera del mes corriente.

Y que cierre con una recomendación cuando el dato la sostenga: después de decirme
qué pasa y por qué, qué mirar después o qué haría —marcado como recomendación,
separado de la cifra, para que yo pueda estar en desacuerdo con el consejo sin
dudar del número. Pero que no la fuerce. Si el dato describe un hecho y no
sugiere ninguna acción, que cierre y listo. Un bot que agrega consejo a todo se
vuelve ruido y se deja de leer.

## LAS REGLAS DE NEGOCIO

Todas en un solo archivo que arma el system prompt. Ninguna en la lógica del
código: quiero poder cambiar el comportamiento sin que nadie toque Python, y
quiero poder leer el archivo entero y reconocer cómo trabaja mi equipo.

Para esto vas a tener que entrevistarme. Hay columnas cuyo nombre miente, hay
palabras que para nosotros significan algo específico, y hay métricas que
calculamos de una manera y no de otra. Explorá los datos primero y después
preguntame todo junto, en un solo turno. Cada regla que escribas lleva un
comentario con el caso real que la originó; si no podés nombrar el caso, la regla
probablemente sobra.

## AUDITORÍA

Paneles expandibles que muestren el SQL y el Python que se ejecutaron detrás de
cada respuesta. Sin eso nadie va a confiar en una cifra que salió de un modelo.
Y el costo en USD al pie de cada respuesta, para saber qué pregunta sale cara.

## EN QUÉ ORDEN

1. **El exportador.** Que reconstruya el archivo entero cada corrida y escriba de
   forma atómica: a un `.nuevo`, y reemplazando el bueno recién al final, con la
   verificación de integridad ya pasada. Una corrida fallida —VPN caída,
   credenciales vencidas, driver ausente— tiene que dejar el archivo anterior
   intacto y usable. Que arme también una **vista plana** con el hecho y todas
   sus dimensiones —el modelo comete muchos menos errores sobre una tabla ancha
   que armando JOINs sobre un modelo estrella— y una tabla `meta` de una fila con
   la fecha de corte, el rango y el conteo de filas.
2. **El diccionario de negocio.**
3. **Un set de evaluación**, antes del agente: un YAML de casos con la respuesta
   verdadera calculada en SQL al lado. Todas las fechas derivadas de `meta`,
   nunca escritas a mano, porque si no el set se rompe en cada recarga de datos y
   lo peor es que no se rompe ruidosamente: sigue corriendo y prueba otra cosa.
   Que los casos cubran las trampas del dominio, no lo fácil. Y que el corredor
   distinga tres resultados, no dos: bien, mal, y error de API —un crédito
   agotado hace fallar todo y el resumen queda idéntico al de una regresión de
   calidad.
4. **El agente**, con caché del system prompt y del historial. La API no tiene
   memoria: cada vuelta del bucle reenvía toda la conversación, así que sin caché
   una pregunta de ocho pasos paga el historial ocho veces.
5. **La interfaz.**
6. **Medir.** Corré el set con `sonnet-5-5` y effort `medium`. Si pasa, quedate
   ahí; si falla casos concretos, subí el effort antes de cambiar de modelo, y
   recién después probá Opus. Mostrame aciertos, costo y tiempo de cada
   configuración.

## DOS COSAS QUE NO SE NEGOCIAN

**Un agente por sesión de navegador**, guardado en `session_state`. No en un
caché compartido: ahí todos los usuarios comparten el mismo objeto y las
conversaciones se mezclan sin ningún síntoma visible —cada uno ve su propio chat
en pantalla mientras el modelo recibe los dos entreverados. La conexión a la base
sí se comparte, en modo solo lectura, y cada sesión toma su propio cursor.

**La conexión a DuckDB en `read_only`.** Es una garantía del motor, no un filtro
de texto. Y la herramienta de Python con builtins restringidos: sin `os`, sin
`subprocess`, sin `open`, sin red.

## CÓMO QUIERO QUE TRABAJES

Proponeme mejoras aunque no las pida. Si ves una decisión mía que te parece
equivocada, decilo en dos líneas y después hacé lo que pedí, dejando el supuesto
escrito. No quiero que te quedes callado ni esperando permiso.

Recomendá, no me pases un menú. Si hay tres caminos, decime cuál tomarías y por
qué; los otros dos en una línea cada uno.

Si te discuto una cifra o un diagnóstico, verificalo antes de darme la razón. Yo
conozco el negocio, vos tenés la base adelante: puede estar equivocado cualquiera
de los dos. Y si el que estaba mal eras vos, corregilo en una línea y seguí.

Separá siempre lo que verificaste de lo que estimaste. Y no digas que algo está
desplegado si solo comprobaste que está subido.

Decime qué archivos tocaste y por qué, en cada cambio.

Mantené un `CLAUDE.md` con la arquitectura, las reglas de negocio no obvias y por
qué existen, las decisiones con su motivo, el estado actual y los riesgos
abiertos. Actualizalo cuando tomemos una decisión, no al final.

Todo en español: código, comentarios, commits y respuestas del bot.
Identificadores sin tildes ni `ñ` para evitar problemas de encoding.

## EMPEZÁ

Explorá la base, hacéme todas las preguntas que te falten en un solo turno, y
proponeme el plan del exportador. No escribas código hasta que lo confirmemos.

---

# Apéndice — trampas ya pagadas

No va en el prompt inicial. Pasalo cuando el proyecto ya esté caminando.

| Trampa | Qué pasó |
|---|---|
| Borrar el archivo destino antes de conectar al origen | Una conexión fallida destruyó la base que funcionaba |
| Fechas literales en el set de evaluación | En la recarga siguiente un caso comparó 4 meses contra 3 y devolvió un 31% sin sentido, con `OK` en verde |
| Caché compartido sobre el agente | Conversaciones de dos usuarios mezcladas, sin ningún síntoma visible |
| Un valor prohibido sin su unidad pegada | `"1,4"` matcheó adentro de `"71,4 M"` y marcó una falla que no existía |
| Lista corta de frases negativas | El bot dijo "no puedo darte" y el set esperaba "no puedo responder" |
| No distinguir error de API de fallo de calidad | Un crédito agotado se leyó como regresión del agente |
| Un redondeo correcto marcado como error | El set esperaba "106" y el bot escribió "107,0 M" sobre 106.950.945 |
| Estimar tokens dividiendo caracteres por 4 | Subestimó el system prompt a la mitad. Contarlos con la API |
| Suponer que un push grande se despliega solo | Tras subir un binario de 43 MB la app siguió sirviendo los datos viejos. Hizo falta reboot manual |
| Repo público con datos licenciados | Se hizo público porque el hosting no veía el privado. El permiso correcto está en *Installed GitHub Apps*, no en *Authorized OAuth Apps*. Resolverlo antes de subir nada |
| Doble eje Y en los gráficos | Engañan. Base 100 o paneles separados |
| Pérdidas con eje negativo | Un eje de −600 a −100 se lee al revés. Magnitudes en positivo, el signo explicado en el texto |
| Etiquetas de barra cortadas | `textposition='inside'`, para que la barra más larga —siempre la más importante— no quede con el número partido |
| Dos gráficos iguales en la misma página | Streamlit deriva el id del contenido y chocan. `key=` explícita en cada figura |
| El símbolo de dólar en el texto | Streamlit interpreta lo que está entre dos `$` como LaTeX y rompe el render. Escribir "USD" |
