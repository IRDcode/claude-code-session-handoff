# claude-code-session-handoff

Claude Code comprime automáticamente las sesiones largas: borra la mayor parte de
tu conversación, la sustituye por un resumen y sigue adelante. Normalmente te das
cuenta porque el modelo empieza a preguntar de nuevo cosas que quedaron zanjadas
dos horas antes.

Este repositorio toma el camino opuesto. Mide cuán llena está realmente la sesión
y, cuando el final está genuinamente cerca, exporta la sesión completa a disco y
continúa en una nueva que **lee la transcripción del padre por entero** antes de
hacer nada más. Nada se pierde en un resumen, y la cadena de sesiones queda
visible y recuperable.

También diagnostica una trampa de configuración que vale la pena conocer aunque
no instales nada de esto: consulta [El recorte](#el-recorte).

Otros idiomas: [English](../README.md) · [简体中文](README.zh-CN.md) ·
[日本語](README.ja.md) · [한국어](README.ko.md) · [Français](README.fr.md) ·
[فارسی](README.fa.md)

---

## Instalación

Requiere Python 3.8+ y Claude Code. Nada más; no hay dependencias que instalar.

```bash
git clone https://github.com/IRDcode/claude-code-session-handoff
cd claude-code-session-handoff
python install.py --dry-run     # ver todos los cambios primero
python install.py
```

Luego reinicia Claude Code y comprueba qué ha detectado:

```bash
python ~/.claude/skills/long-session-handoff/scripts/session_weight.py --explain
```

La instalación por defecto **no** cambia cómo Claude Code gestiona el contexto.
La compactación automática se queda tal cual; el guardián simplemente hace el
relevo antes de que pueda dispararse. Para quitarlo todo:

```bash
python install.py --uninstall
```

`settings.json` se respalda antes de tocarlo, tus hooks y tu línea de estado
existentes se dejan intactos, e instalar dos veces no hace nada.

## Qué se instala

| ruta | qué es |
|---|---|
| `~/.claude/skills/long-session-handoff/` | el procedimiento que sigue el modelo, más tres scripts |
| `~/.claude/hooks/session-weight-watch.py` | el detector, en cuatro eventos |
| `~/.claude/hooks/statusline-weight.py` | el peso en la línea de estado, en cada renderizado |
| `~/.claude/runtime/` | estado anti-insistencia, un log y una caché de medición |
| `~/.claude/handoffs/` | las exportaciones y `chains.json`, que enlaza padre e hijo |

Se registran cuatro eventos de hook: `UserPromptSubmit`, `SessionStart`,
`PreCompact` y `PostCompact`. Tus hooks existentes en esos eventos se conservan.

## En qué difiere del comportamiento por defecto

| | Claude Code por defecto | con esto instalado |
|---|---|---|
| cuando la sesión se llena | se dispara la compactación; la mayor parte de la conversación se descarta y se sustituye por un resumen | se te ofrece un relevo mucho antes de ese punto |
| qué sabe la sesión siguiente | lo que captara el resumen, escrito por el agente que ya estaba perdiendo el hilo | la transcripción del padre, leída por entero y verificada con recuentos |
| historial descartado | sin referencia en la sesión viva | recuperado del disco en `05-dropped-context.md` |
| ¿cuán llena está de verdad? | `/context` muestra el % de la ventana | la línea de estado muestra el % del muro que realmente termina la sesión |
| encontrar la continuación después | recorrer `/resume` | `chains.json` registra padre, hijo, el peso en el momento de migrar y si la lectura se verificó |

La línea de estado se ve así:

```
Opus 5 | ████████░░ 85% 830k/977k | 696t 402tc 6.1h | HANDOFF DUE (5) | no-compact
```

Porcentaje del **muro**, no de la ventana. Son distintos, a veces por un factor
de cinco, y eso es justo de lo que trata la sección siguiente.

## El recorte

Vale la pena leerlo incluso si no instalas nada.

Claude Code tiene dos puntos distintos en los que una sesión termina:

```
la compactación se dispara en  ventana − reserva de respuesta (~20k) − búfer de resumen (~13k)
el envío se rechaza en         techo   − reserva de respuesta (~20k) − margen (~3k)
```

El primero aplica con la compactación activada; el segundo, desactivada. Así que
una ventana de 200.000 tokens compacta alrededor de **167.000**.

Aquí está la trampa: **`autoCompactWindow` en `settings.json` se recorta al techo
del modelo, en silencio.** Pide 1.000.000 contra un techo de 200.000 y obtienes
200.000, sin que nada en la interfaz lo diga. Una sesión configurada para un
millón de tokens se compacta a 167.000, tres veces seguidas, mientras unos
830.000 tokens ya pagados quedan sin usar.

No es hipotético. Es el origen de este repositorio: tres compactaciones con
`preTokens` de **167.398 / 167.071 / 166.904**, contra un archivo de configuración
que decía `autoCompactWindow: 1000000`.

`--explain` te dice en qué caso estás:

```
WINDOW
  client reported  1,000,000
  ceiling          1,000,000   (source DISABLE_COMPACT+CLAUDE_CODE_MAX_CONTEXT_TOKENS)
  resolved         1,000,000   (source settings)

WALL -- the token count past which no more work happens here
  1,000,000 ceiling - 20,000 reply reserve - 3,000 margin
  = 977,000   then SENDING IS REFUSED (no summary; a handoff is the only exit)
```

Si dice `settings CLAMPED to …`, tu ajuste de ventana está siendo rebajado.

Solo una configuración escapa al recorte: `DISABLE_COMPACT=1` junto con
`CLAUDE_CODE_MAX_CONTEXT_TOKENS`. El instalador puede ponerlo por ti, pero
pregunta primero y te dice el coste, porque **también desactiva el `/compact`
manual**:

```bash
python install.py --disable-compact --window 1000000
```

Con eso puesto, la sesión ya no termina en un resumen: termina en un rechazo a
enviar. Es un intercambio real. Un rechazo es sobrevivible: haces el relevo y
sigues. Un historial destruido en silencio no lo es. Pero si ignoras todos los
avisos hasta el muro, esa sesión deja de aceptar turnos, y conviene saberlo de
antemano. El guardián se dispara al 85%, dejando unos 147.000 tokens de margen,
así que en la práctica no se llega.

Sáltate este flag si prefieres conservar `/compact`. El relevo sigue funcionando.

## Compatibilidad

Nada de esto parchea ni envuelve Claude Code. Lee dos interfaces documentadas —el
contrato de stdin/stdout de los hooks y la carga de la línea de estado— y analiza
las transcripciones JSONL que el cliente ya escribe. Por eso sobrevive a
actualizaciones que romperían una herramienta construida sobre internos.

Donde hace falta un número exacto, se toma de la evidencia y no se afirma. Tres
capas:

1. La ventana viene de `context_window_size`, que el cliente informa sobre sí mismo
   en cada renderizado de la línea de estado.
2. Si la sesión se ha compactado alguna vez, el disparador viene del `preTokens`
   registrado en la transcripción en ese momento: el disparador observado, no
   calculado. `score()` lo prefiere y lo indica con
   `corrected from observed preTokens`.
3. Solo sin ninguno de los dos recurre a la aritmética de reservas, y `--explain`
   muestra cada entrada para que la desviación sea visible en lugar de silenciosa.

**Versiones antiguas de Claude Code.** Los cuatro eventos de hook y la línea de
estado han sido estables durante muchas versiones. Si falta un evento en la tuya,
ese hook nunca se dispara y el resto sigue funcionando: el detector es aditivo, no
un reemplazo. `--disable-compact` es la única parte que depende de nombres de
ajustes concretos; `--explain` te dirá si no tuvo efecto.

**Sistemas operativos.** Python puro, sin dependencias, sin partes compiladas. Las
rutas pasan por `os.path`, `CLAUDE_CONFIG_DIR` se respeta en todas partes, y el
instalador elige un nombre de intérprete que funcione en *tu* shell en lugar de
fijarlo. El único código específico de plataforma es forzar UTF-8 en stdout, que
Windows necesita y en el resto es inocuo.

Verifícalo en tu propia máquina:

```bash
python tests/test_session_weight.py    # aritmética, la compuerta, las dos trampas
python tests/test_compat.py            # suelo de sintaxis, puntos de entrada, salida de los hooks
```

`test_compat.py` encuentra todos los demás Python instalados en tu máquina y vuelve
a ejecutar la suite en cada uno, así una diferencia de versión aparece como un fallo
y no como una sorpresa más adelante.

## Cuándo se dispara

Se miden siete señales. El contexto es el único que vota **si** hay que moverse;
el resto solo afina **cuánta urgencia** hay.

| señal | umbral |
|---|---|
| contexto frente al muro | ≥ 85% → relevo, ≥ 95% → dejar de preguntar y actuar |
| turnos del asistente | ≥ 900 |
| llamadas a herramientas | ≥ 600 |
| tiempo de trabajo activo | ≥ 4 h |
| la compactación ya se disparó | cualquiera |

**Nada se ofrece por debajo del 62% del muro, sin importar qué más se active.**
Esta compuerta existe porque las demás señales son *aproximaciones* de la presión
de contexto, inventadas para un mundo en el que el contexto no podía medirse
directamente. Medido contra la sesión que construyó esto: 4,3 h de trabajo más dos
compactaciones previas puntuaban «relevo ya», mientras el contexto estaba en
147.527 de 977.000 — el 15%. Moverse entonces habría tirado 829.473 tokens sin
ganar nada.

Dos detalles de medición que importan más de lo que parece:

- **El tiempo activo es la suma de los huecos menores de 10 minutos**, nunca
  último menos primero. Una sesión abierta toda la noche marca 44 h de intervalo y
  11 h de trabajo; puntuar el intervalo dispara un relevo en una sesión inactiva.
- **Las compactaciones se cuentan desde la fila tipada de la transcripción**,
  nunca buscando una cadena marcadora. Busca el marcador una vez y aparecerá en tu
  propia salida de herramienta, y el recuento se infla solo.

El aviso aparece como máximo una vez por tramo — 200 turnos más, u otra décima
parte del muro — con un suelo de 15 minutos. Nunca se dispara dentro de un
subagente.

## El relevo en sí

```
medir  →  preguntar  →  exportar  →  crear la continuación  →  esta lee al padre
```

La exportación escribe cinco archivos: cada mensaje del usuario literal (incluidos
los enviados a mitad de turno, que son fáciles de perder), cada mensaje sustancial
del asistente, la transcripción completa con las cargas de herramientas recortadas,
un índice de recuentos y lo que descartaron las compactaciones anteriores.

Después la continuación se crea y se **despierta sin interfaz** para leer la
exportación antes de que tú la abras. Debe responder con recuentos que coincidan
con el índice; si no coinciden, la lectura fue parcial y el relevo no está hecho.
Esa lectura ocurre en una sesión en la que nadie está esperando, así que la parte
costosa de una migración no te cuesta tiempo real.

Después obtienes el id y el nombre:

```
claude --resume 7157caa1-11ce-4f29-a46a-09913d483fb0
```

o busca el nombre en `/resume`: lleva las palabras del tema del padre más
`(cont. 2)`.

## Verifícalo tú mismo

Todo lo anterior se puede comprobar en tu propia máquina. Los scripts imprimen
números, no tranquilidad:

```bash
# ¿dónde termina realmente mi sesión, y por qué?
session_weight.py --explain

# ¿cuál es el peso actual, con cada señal nombrada?
session_weight.py --session-id <uuid>

# legible por máquina
session_weight.py --session-id <uuid> --json
```

Para confirmar que un cambio de configuración surtió efecto, no te fíes del
archivo: lee la transcripción. Busca el primer turno cuyo total de tokens supere
el umbral antiguo y comprueba que no le sigue ninguna fila de compactación nueva.

## Limitaciones

Dichas sin rodeos, porque una herramienta que mide cosas debe ser honesta sobre lo
que no ha medido:

- Las reservas (~20k / ~13k / ~3k) se derivan del comportamiento observado. Una
  versión futura podría cambiarlas. Hay tres capas de defensa: la ventana que el
  propio cliente informa, el `preTokens` real que queda en la transcripción (el
  disparador observado, que `score()` prefiere), y solo al final esta aritmética de
  reservas — la conjetura es únicamente la tercera capa.
- El muro de rechazo de envío se ha calculado y corroborado, no alcanzado a
  propósito. El guardián está diseñado para que nunca llegues ahí.
- Probado en Windows con Python 3.11, 3.12 y 3.14, y comprobado contra la gramática
  de 3.8. Linux y macOS deberían funcionar — no queda código específico de
  plataforma más allá de la codificación de consola — pero ninguno se ha ejecutado
  de principio a fin.
- La exportación de cinco archivos y el medidor están cubiertos por las pruebas. El
  despertar sin interfaz depende de que tu binario `claude` se pueda lanzar; si no
  se puede, la exportación igualmente se completa y la herramienta te dice qué
  hacer.
- Caché de prompt: un relevo inicia una sesión nueva, así que su caché arranca en
  frío. Para una sesión cerca del muro es un buen intercambio; sigue siendo un
  coste.

## Contribuir

Los informes de fallos son bienvenidos, especialmente «los números no cuadraban en
mi entorno»: incluye la salida de `--explain`. Si una versión de Claude Code mueve
esta aritmética, ese es el informe que lo arregla más rápido.

## Licencia

MIT — ver [LICENSE](../LICENSE).
