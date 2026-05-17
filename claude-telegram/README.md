# claude-telegram

Bot de Telegram para hablar con Claude Code (CLI) desde el teléfono.
Cualquier mensaje al bot se le pasa a `claude -p` y la respuesta vuelve
al chat, **streameada en vivo con MarkdownV2** (la respuesta se va
editando en el mismo mensaje a medida que claude la genera, con código
en bloques monoespaciados). También se aceptan fotos e
imágenes-como-documento: el bot las baja a
`~/.cache/claude-telegram/downloads/` y se las pasa a claude
referenciadas por path (claude las abre con su tool Read). Voice
notes / audios: se transcriben localmente con faster-whisper y la
transcripción va a claude como prompt (más eco al chat con `🎙️` para
que veas qué se entendió). Y **documentos genéricos** (PDF, PPT/PPTX,
ZIP, .pkg, código, texto, etc.): se descargan al mismo cache y se le
pasa a claude el path absoluto + nombre original + mime, para que los
inspeccione con la tool que corresponda (Read para PDF/texto/código,
Bash con `unzip`/`pdftotext`/`pandoc`/etc. para los binarios). El
`session_id` de cada turno se persiste en
`~/.local/state/claude-telegram/state.json`, así la conversación
sobrevive a reinicios del daemon (`claude -r <id>` en vez del frágil
`--continue`). Si lo activas, los archivos que claude genere durante
un turno te llegan al chat como adjuntos. Comandos `/new` (resetear
sesión), `/stop` (cancelar), `/session` (ver session_id), **`/model`
(ver/cambiar el modelo: `opus`/`sonnet`/`haiku`/`default`/id completo —
se persiste y sobrevive reinicios)**, `/clear` / `/clear_chat`, `/ping`,
`/cwd`, `/help`.

Hermano del proyecto `sudo-telegram` — misma forma de instalar, pero el
daemon corre como `sergioc` (no necesita root) y no usa askpass ni socket.

## Arquitectura

```
   tu teléfono
        │  HTTPS
        ▼
   api.telegram.org
        │  long polling
        ▼
   claude-telegram-daemon  (sergioc, systemd)
   ├─ valida chat_id
   ├─ si la update trae photo/document(image/*), la baja a
   │  ~/.cache/claude-telegram/downloads/ y la referencia en el prompt
   ├─ si la update trae voice/audio/document(audio/*), la baja, la
   │  transcribe con faster-whisper local y la usa como prompt
   ├─ si la update trae document(otro mime: PDF/PPT/ZIP/.pkg/código/…),
   │  la baja al mismo cache y la pasa como path + filename + mime al
   │  prompt para que claude la inspeccione con Read/Bash
   ├─ ejecuta `claude -p --output-format stream-json --verbose
   │  [-r <session_id>] "<mensaje o caption + path>"`
   ├─ parsea events JSON line-by-line:
   │    • system.init → captura session_id (lo guarda al terminar)
   │    • assistant.text → append al mensaje de Telegram (editado en vivo)
   │    • assistant.tool_use → renderiza 🔧/📖/✏️/🔎… inline
   ├─ snapshotea mtimes pre-turno; al terminar adjunta archivos nuevos
   │  como sendDocument (si output_attach_enabled=true)
   └─ persiste session_id en ~/.local/state/claude-telegram/state.json
        │
        ▼
   Claude Code  →  tus MCPs, skills, settings, CLAUDE.md
```

## Inventario (después de instalar)

| Path | Dueño | Perms | Qué es |
|---|---|---|---|
| `/usr/local/bin/claude-telegram-daemon` | root:root | 0755 | El daemon (Python) |
| `/opt/claude-telegram/venv/` | root:root | 0755 | Venv con `faster-whisper` (transcripción) |
| `/etc/claude-telegram/config.toml` | sergioc:sergioc | 0400 | Token, chat_id, settings |
| `/etc/systemd/system/claude-telegram.service` | root:root | 0644 | Unit |
| `~/.local/state/claude-telegram/state.json` | sergioc:sergioc | 0644 | Último `session_id` + `model` activo (para `claude -r` y `--model` entre reinicios) |

---

## Paso 1 — crear el bot

1. En Telegram, hablar con **@BotFather**:
   ```
   /newbot
   <Name>:    Claude Chat
   <Username>: <algo>_claude_bot
   ```
   Anotar el token. **Que sea otro bot, distinto al de sudo** — para no
   confundirlos visualmente y para que no compartan token.

2. Configurarlo (todavía con BotFather):
   ```
   /setprivacy        → Enable    (DM-only)
   /setjoingroups     → Disable
   /setcommands       → pegá:
       new      - Resetear sesión (olvidar contexto)
       stop     - Cancelar query en curso
       session  - Mostrar session_id actual
       model    - Ver/cambiar modelo (opus/sonnet/haiku/default)
       clear    - Limpiar mensajes rastreados del chat
       cwd      - Mostrar working directory
       ping     - Health check
       help     - Ayuda
   ```

3. **Mándale `/start` desde tu cuenta** — sin esto el bot no te puede
   responder.

4. Obtener el `chat_id`:
   ```bash
   TOKEN=12345:AAA bash tools/get-chat-id.sh
   ```

## Paso 2 — instalar

```bash
cd ~/.bots/claude-telegram
sudo bash INSTALL.sh
```

El instalador:
- crea `/etc/claude-telegram/` (dueño `sergioc`)
- copia el daemon a `/usr/local/bin/claude-telegram-daemon`
- instala el systemd unit
- copia `config.example.toml` a `/etc/claude-telegram/config.toml` si
  no existe (dueño `sergioc`, perms 0400)

Idempotente — volver a correrlo actualiza el binario sin tocar el config.

## Paso 3 — configurar

```bash
$EDITOR /etc/claude-telegram/config.toml
```

(No hace falta `sudo` — el archivo es tuyo.)

Setear `bot_token` y `chat_id`. Opcional:
- `working_dir`: dónde corre claude (afecta CLAUDE.md, paths relativos).
- `extra_args`: ver abajo, sección Permisos.
- `timeout_s`: cap por query (default `10800` = 3h). Si claude se pasa,
  el daemon lo mata con SIGKILL y manda `⚠️ claude exit=-1 / timeout, killed`.
  Cuando ves ese mensaje, el proceso ya está muerto de verdad — no sigue
  trabajando en background.
- `heartbeat_interval_s`: cada cuántos segundos avisar al chat que claude
  sigue vivo durante una query larga (default `300` = 5 min, `0` desactiva).
  Útil porque el indicador "typing…" es efímero y no queda en el historial.
- `whisper_*`: configuración de la transcripción local — ver sección
  "Voice notes" más abajo.

## Paso 4 — arrancar

```bash
sudo systemctl start claude-telegram
sudo journalctl -fu claude-telegram   # otra terminal
```

Mandar `/ping` al bot — debe responder `pong`. Si no, revisa el journal.

Después:
```bash
sudo systemctl enable claude-telegram
```

## Voice notes / audios

El bot acepta `voice` (notitas de Telegram, OGG/Opus), `audio` (mp3/wav
mandados como música) y `document` con mime `audio/*`. Flujo:

1. Telegram entrega el `file_id`.
2. El daemon baja el audio a `~/.cache/claude-telegram/downloads/`.
3. Lo transcribe con [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper)
   corriendo **local** en el venv `/opt/claude-telegram/venv/`. El modelo
   se carga lazy a RAM la primera vez que llega un audio y queda residente.
4. Mete la transcripción al chat con `🎙️ <texto>` para que veas qué
   se entendió, y la usa como prompt para `claude -p`.
5. Si el audio venía con caption, se pega `caption + transcripción`.

### Configuración

```toml
whisper_enabled = true
whisper_model = "small"          # tiny|base|small|medium|large-v3|distil-large-v3|large-v3-turbo
whisper_device = "cpu"           # cpu|cuda|auto
whisper_compute_type = "int8"    # int8 (cpu) | float16 (cuda) | int8_float16 | float32
whisper_language = ""            # "" = autodetect; "es", "en", etc.
```

Defaults: `small` en CPU con int8 — ~470MB de pesos, corre rápido en
cualquier CPU moderna, alcanza para voice notes en español. Si tienes
GPU, `whisper_device = "cuda"` y `whisper_compute_type = "float16"`
con un modelo más grande te da mejor calidad.

Pesos se cachean en `~/.cache/huggingface/hub/` (no en el repo, no en
`/etc/`). Primera transcripción descarga el modelo (~30s–2min según
red), las siguientes son instantáneas.

### Privacidad

El audio **no sale del PC torre**. faster-whisper hace todo offline.
Lo único que pasa por la red es la descarga inicial del modelo desde
HuggingFace.

### Para desactivar

```toml
whisper_enabled = false
```

Si llega un audio con esto en false, el bot responde con un warning
en vez de transcribir.

## Streaming + sesiones persistentes

Por defecto el daemon corre claude con
`--output-format stream-json --verbose`. Cada línea del stdout es un
event JSON (`system.init`, `assistant`, `tool_use`, `result`), que se
parsea y se va volcando al chat:

- **Texto del modelo**: se appendea al mismo mensaje de Telegram con
  `editMessageText`, throttleado a `streaming_min_edit_s` (default
  `1.2s` — Telegram tira flood control si pasas de ~1/seg al mismo
  chat). Cuando supera ~3800 chars, deja ese mensaje cerrado y arranca
  uno nuevo.
- **Tool use**: cada vez que claude llama una tool (Bash, Read, Edit,
  Grep, WebFetch, etc.) se inserta una línea inline tipo
  `🔧 Bash: ls -la`, en cursiva. Sirve para ver lo que está haciendo
  sin tener que abrir los logs.
- **Formato MarkdownV2**: el texto se escapa con un parser propio que
  respeta los fences ` ``` ` (los pasa como bloques de código con
  monoespaciado y opcional language hint), y escapa el resto como prosa.
  Si el render falla por algún caracter raro, el helper retira el
  `parse_mode` y reintenta como texto plano.

El `session_id` que claude emite en `system.init` (y reconfirma en
`result`) se persiste en `~/.local/state/claude-telegram/state.json`
al final de cada turno exitoso. La próxima invocación arranca con
`claude -p -r <session_id>` en vez del frágil `--continue` (que se
basa en "última sesión de la cuenta" y se pierde si reinicias el
daemon o intercalás sesiones de claude desde otra terminal). `/new`
limpia el archivo y empieza de cero.

Comando `/session` muestra el id actual (útil para debug o para
reanudar la conversación desde la terminal con
`claude -r <id>`).

### Mensaje resumen al final

Por default (`summary_message_enabled = true`), después del streaming
en vivo el daemon manda **un mensaje nuevo** con la respuesta final
consolidada — solo el texto del modelo, sin los indicadores de
`tool_use`. La idea es separar visualmente "lo que claude hizo" (el
mensaje editado en vivo) de "la respuesta a lo pedido" (el mensaje
limpio al final). Hay duplicación con lo que ya viste streamearse;
ese es el costo. Pon `summary_message_enabled = false` si prefieres
solo el streaming sin el mensaje extra.

### Para volver al modo viejo

```toml
streaming_enabled = false
```

Con eso vuelve a `--output-format json`, espera el output completo y
lo manda en chunks al final. La persistencia del session_id sigue
funcionando.

## Adjuntar archivos generados

Cuando claude crea o modifica archivos en `working_dir` durante un
turno (un CSV, un PDF, un patch, una imagen), el daemon puede mandártelos
de vuelta como `sendDocument`. **Está apagado por default** porque
`working_dir` suele ser `/home/sergioc` y eso significa adjuntar
cualquier write incidental (cache, descargas del navegador, etc.).

Para activarlo de manera útil, apuntalo a un repo o a un scratch dir:

```toml
output_attach_enabled = true
output_attach_dir = "/home/sergioc/scratch"   # "" = working_dir
output_attach_max_files = 5
output_attach_max_bytes = 10485760  # 10 MiB por archivo
output_attach_max_depth = 4
```

Cómo funciona:

1. Antes de correr claude, el daemon registra `started_at = time.time()`.
2. Después de terminar (rc==0), camina `output_attach_dir` hasta
   `output_attach_max_depth` niveles, ignorando hidden dirs y nombres
   de noise (`.git`, `.cache`, `node_modules`, `__pycache__`, `.venv`,
   `venv`, `target`, `dist`, `build`, etc.).
3. Toma los archivos con `mtime ≥ started_at`, descarta vacíos y los
   que pasen de `output_attach_max_bytes`, ordena por mtime desc, y
   manda los primeros `output_attach_max_files` como `sendDocument`
   con caption `📎 <ruta relativa>`.

## Permisos de tools (importante)

Por defecto, `extra_args = []` y claude corre en su modo de permisos
normal. Sobre Telegram **no puedes contestar prompts de permisos**, así
que cualquier query que requiera permiso (Bash, Edit en archivos
nuevos, etc.) va a fallar o quedar colgada.

Para uso sólo de Q&A / lectura / análisis, eso está bien.

Para que actúe agentic sin pedir permisos (cuidado):
```toml
extra_args = ["--dangerously-skip-permissions"]
```
Con esto, cualquier mensaje desde tu chat equivale a estar sentado en
una terminal con shell access. La whitelist de `chat_id` es la única
defensa — si tu cuenta de Telegram se compromete, el atacante tiene
tu shell.

Modo intermedio:
```toml
extra_args = ["--permission-mode", "acceptEdits"]
```
Acepta edits automáticamente, pero seguirá pidiendo permiso para Bash.

## Integración con sudo

Cuando claude (corriendo bajo este bot) hace `sudo -A <cmd>`, el flujo
es:

1. El unit setea `SUDO_ASKPASS=/usr/local/bin/sudo-telegram-askpass` y
   pone a `sergioc` en el grupo `sudo-telegram` (para que pueda hablar al
   socket del daemon).
2. `sudo` invoca al askpass, que conecta al daemon de `sudo-telegram`.
3. El daemon te manda un prompt al **bot de aprobaciones**
   (`@tu_bot_de_sudo_bot`, distinto al de chat con claude). Con botón
   ✅ Sí / ❌ No.
4. Apruebas → la contraseña se libera → sudo escala a root → el comando corre.

Para que esto funcione el unit tiene `NoNewPrivileges=no` y no incluye
`RestrictSUIDSGID=yes`. Sin esos dos flags off, el kernel le rehúsa a
sudo siquiera intentar setuid (el askpass nunca llega a ejecutarse). El
control de seguridad real es la aprobación por Telegram, no esos flags.

## Operaciones

| Acción | Comando |
|---|---|
| Estado | `sudo systemctl status claude-telegram` |
| Logs | `sudo journalctl -fu claude-telegram` |
| Reiniciar | `sudo systemctl restart claude-telegram` |
| Cambiar config | editar `/etc/claude-telegram/config.toml` y reiniciar |
| Cancelar query | enviar `/stop` al bot |
| Resetear sesión | enviar `/new` al bot |
| Ver session_id | enviar `/session` al bot |
| Cambiar modelo | enviar `/model opus` (o `sonnet`/`haiku`/`default`/id completo) |
| Reanudar sesión desde terminal | `claude -r $(jq -r .session_id ~/.local/state/claude-telegram/state.json)` |
| Desinstalar | `sudo bash UNINSTALL.sh` |

## Riesgos residuales (lectura honesta)

1. **Telegram es el ancla de confianza.** Quien controle tu cuenta de
   Telegram puede hablarle al bot. Con `--dangerously-skip-permissions`
   eso es shell access. Sin eso, es Q&A pero igual lee tu home y
   ejecuta tools de lectura. Activa 2FA en Telegram.

2. **El bot corre como `sergioc`** y, vía `sudo -A` + sudo-telegram,
   puede pedir aprobación para ejecutar cualquier cosa como root. Lo
   que tú puedes hacer sin sudo, el bot también; lo que requiera sudo
   queda gateado por una aprobación tuya en `@tu_bot_de_sudo_bot`.
   Lee tus archivos personales, ssh keys (si claude lo decide o si se
   lo pides), etc.

3. **El token del bot, si se filtra, permite leer tus mensajes** (qué
   le estás pidiendo a Claude). NO permite mandar al chat porque sólo
   tu chat_id está autorizado, pero un atacante puede ver el contenido
   de tus prompts y respuestas. Rota el token si sospechas.

4. **Claude tiene contexto persistente entre mensajes** (`--continue`).
   Si quieres "olvida lo anterior", usa `/new`.

5. **No hay rate limit.** Si alguien te bombardea (sólo posible si
   tiene tu chat_id y tu cuenta) las queries se serializan pero se van
   acumulando. El proceso de claude consume créditos / API quota.

6. **No hay audit log estructurado.** Todo va a journald
   (`journalctl -u claude-telegram`). Si necesitas trazabilidad
   formal, agrégalo en `handle_text`.

## Desinstalar

```bash
sudo bash UNINSTALL.sh
```

Borra binario y unit. **Deja** `/etc/claude-telegram/` (con tu token)
para que decidas qué hacer.
