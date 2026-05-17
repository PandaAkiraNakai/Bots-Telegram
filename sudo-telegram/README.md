# sudo-telegram

Aprobación de sudo fuera de banda vía un segundo bot de Telegram. Cuando
un usuario no-root corre `sudo -A <comando>`, en vez de pedir contraseña
en la terminal, te llega un mensaje al teléfono con el comando completo,
host, CWD, PID y un ID corto, más dos botones inline ✅ Sí / ❌ No.
Tap → el daemon entrega la contraseña al cliente askpass; tap en No,
60s sin respuesta, o Telegram inalcanzable → sudo falla cerrado.

El comando que ves es el `argv` real de `sudo` leído por el daemon (root)
desde `/proc/<sudo_pid>/cmdline`, no un campo enviado por el cliente —
un proceso malicioso no puede mentir sobre lo que está pidiendo aprobar.

## Arquitectura

```
   user / Claude
   sudo -A <cmd>
        │
        ▼
   /usr/local/bin/sudo-telegram-askpass   (cliente, corre como sergioc)
        │  Unix socket /run/sudo-telegram/sock
        ▼
   sudo-telegram-daemon   (root, systemd, CAP_SYS_PTRACE)
   ├─ valida SO_PEERCRED (uid del cliente)
   ├─ lee /proc/<sudo_pid>/cmdline (cmd real, no spoofeable)
   ├─ rate limit
   ├─ envía prompt al chat de Telegram con botones inline ✅/❌
   ├─ espera tap del botón (callback_query) o /yes_<uuid> /no_<uuid> de fallback
   ├─ al resolverse, edita el mensaje en lugar para mostrar el resultado
   ├─ si aprobado → lee /etc/sudo-telegram/password y la devuelve
   └─ append a /var/log/sudo-telegram/audit.log (chattr +a)
        │
        ▼ HTTPS
   api.telegram.org  ←→  bot de aprobaciones  ←→  tu teléfono
```

## Inventario de archivos (después de instalar)

| Path | Dueño | Perms | Qué es |
|---|---|---|---|
| `/usr/local/bin/sudo-telegram-daemon` | root:root | 0755 | El daemon (Python) |
| `/usr/local/bin/sudo-telegram-askpass` | root:root | 0755 | Cliente askpass (Python) |
| `/etc/sudo-telegram/config.toml` | root:root | 0400 | Token, chat_id, settings |
| `/etc/sudo-telegram/password` | root:root | 0400 | Tu password de sudo |
| `/etc/sudoers.d/claude` | root:root | 0440 | `Defaults askpass=` + whitelist |
| `/etc/systemd/system/sudo-telegram.service` | root:root | 0644 | Unit (con `CAP_SYS_PTRACE` para leer cmdline de sudo) |
| `/etc/profile.d/sudo-telegram.sh` | root:root | 0644 | Exporta `SUDO_ASKPASS` y alias |
| `/var/log/sudo-telegram/audit.log` | root:sudo-telegram | 0640 +a | Log append-only |
| `/run/sudo-telegram/sock` | root:sudo-telegram | 0660 | Socket Unix |

---

## Paso 1 — crear el bot de aprobaciones

1. Abre Telegram y habla con **@BotFather**:
   ```
   /newbot
   <Name>:    Sudo Approver
   <Username>: <algo>_sudo_bot   (debe terminar en _bot)
   ```
   Anota el token (`12345:AAA...`).

2. Configura el bot (todavía con BotFather):
   ```
   /setprivacy            → Enable   (DM-only)
   /setjoingroups         → Disable
   /setcommands           → pega:
       ping     - Health check
       list     - Ver IDs pendientes
   ```

   `/yes_<id>` y `/no_<id>` siguen funcionando como fallback de texto pero
   ya no se documentan como comandos del bot — el flujo normal usa los
   botones inline.

3. **Abre el chat con tu bot y mándale `/start`** (sin esto, el bot no
   te puede mensajear).

4. Obtén tu `chat_id`:
   ```bash
   TOKEN=12345:AAA bash tools/get-chat-id.sh
   ```
   Anota el número (`chat_id=123456789  type=private  name=...`).

## Paso 2 — instalar

```bash
cd ~/.bots/sudo-telegram
sudo bash INSTALL.sh
```

El instalador:
- crea el grupo `sudo-telegram` y agrega a `sergioc`
- crea `/etc/sudo-telegram/`, `/var/log/sudo-telegram/`
- copia los binarios a `/usr/local/bin/`
- instala el systemd unit
- instala `/etc/sudoers.d/claude` (solo si no existe)
- instala `/etc/profile.d/sudo-telegram.sh` (alias `sudo='sudo -A'`)
- crea archivos vacíos para `config.toml` y `password`
- crea el log con `chattr +a`

Es idempotente — re-ejecutarlo actualiza los binarios sin tocar tus
secretos ni tu whitelist.

## Paso 3 — configurar

Editar `/etc/sudo-telegram/config.toml`:

```bash
sudo $EDITOR /etc/sudo-telegram/config.toml
```

Pon `approval_bot_token`, `chat_id`, y `allowed_users = ["sergioc"]`.

Por default el bot **borra solo sus propios mensajes a los 30 min**
(`chat_auto_delete_s = 1800`) para que el chat con el bot no se llene
de prompts viejos. Un thread reaper chequea cada
`chat_auto_delete_interval_s` (default 60s). Setea `chat_auto_delete_s = 0`
para desactivar. Telegram solo permite a un bot borrar sus propios
mensajes hasta 48h después; los mensajes que vos mandás al bot
(taps de botones cuentan como callback queries, no mensajes) no se
tocan. Cada lote de borrados se loggea como `chat_reaped` en el audit.

Cargar la password de sudo (sin newline final):

```bash
printf '%s' 'TU-PASSWORD' | sudo tee /etc/sudo-telegram/password >/dev/null
sudo chmod 0400 /etc/sudo-telegram/password
```

## Paso 4 — primero `dry_run`, después de verdad

Setea `dry_run = true` en `config.toml` y arranca:

```bash
sudo systemctl start sudo-telegram
sudo journalctl -fu sudo-telegram   # otra terminal
```

Deberías ver `listening on /run/sudo-telegram/sock`.

Manda `/ping` a tu bot — debe responder `pong`. Si no, revisa `journalctl`.

Hacer un re-login (o `newgrp sudo-telegram`) para que tu shell tome el
grupo nuevo y el `SUDO_ASKPASS` del profile.d.

Probar el flujo:

```bash
bash ~/.bots/sudo-telegram/tools/test-flow.sh
```

Te llega el mensaje a Telegram con dos botones. Tap ✅ Sí. El test
imprime `DRY_RUN_FAKE_PASSWORD` (porque el daemon está en dry-run) y
sale 0. Tap ❌ No (o esperar 60s) → exit 1. Si preferís texto, también
funciona `/yes_<id>` o `/no_<id>`.

Cuando todo se vea bien, flip:

```bash
sudo $EDITOR /etc/sudo-telegram/config.toml   # dry_run = false
sudo systemctl restart sudo-telegram
sudo systemctl enable sudo-telegram
```

Y ya el flujo real:

```bash
sudo -A whoami
# → mensaje en Telegram con botones ✅/❌, tap ✅, sale "root"
```

## Paso 5 — afinar la whitelist

Edita `/etc/sudoers.d/claude` y descomenta los comandos que quieras
ejecutar sin aprobación. **Validar siempre** antes de salir:

```bash
sudo visudo -c -f /etc/sudoers.d/claude
```

Reglas de oro:
- No descomentes nada que pueda dropearte a un shell (`vim`, `less`, `find -exec`, `awk`).
- No uses wildcards que un atacante pueda explotar (`/usr/bin/rm *` es trivial de abusar).
- Cada línea de la whitelist es una flecha al pie potencial — si dudas, no la pongas.

## ¿Por qué `-A`?

`sudo` solo invoca `SUDO_ASKPASS` si:
- pasas `-A`, **o**
- no hay tty controlador (cron, ssh sin tty, etc.)

Sin `-A` y con tty, sudo lee del tty. Por eso:

- Para shell interactivo: el alias `sudo='sudo -A'` del profile.d resuelve.
- Para scripts y sub-procesos: usa `sudo -A` explícito (o setea `SUDO_ASKPASS` y elimina toda fuente de tty).
- Para Claude Code: pon `sudo -A` en cada comando.

Si te olvidas y no hay password en tu sesión, sudo te pedirá la pass en
el tty y vas a fallar al intentar tipearla — es el comportamiento
correcto, no un bug.

## Auditoría

Cada solicitud (aprobada, denegada, expirada, rate-limited, fallo de red)
se appendea como una línea JSON a `/var/log/sudo-telegram/audit.log`.
El archivo está `chattr +a`, así que ni root puede truncar/rotar sin
hacer `chattr -a` primero.

```bash
sudo cat /var/log/sudo-telegram/audit.log | jq -c '{ts, event, peer_user, cmd, decision}'
```

## Operaciones

| Acción | Comando |
|---|---|
| Estado | `sudo systemctl status sudo-telegram` |
| Logs daemon | `sudo journalctl -fu sudo-telegram` |
| Logs auditoría | `sudo tail -f /var/log/sudo-telegram/audit.log` |
| Reiniciar | `sudo systemctl restart sudo-telegram` |
| Cambiar password | `printf '%s' '...' \| sudo tee /etc/sudo-telegram/password >/dev/null` |
| Health check | enviar `/ping` al bot |
| Listar pendientes | enviar `/list` al bot |
| Rotar log | `sudo chattr -a /var/log/sudo-telegram/audit.log && sudo mv ... && sudo systemctl restart sudo-telegram && sudo chattr +a ...` |
| Dry-run | `dry_run = true` en config + restart |
| Desinstalar | `sudo bash UNINSTALL.sh` |

## Riesgos residuales (lectura honesta)

Esto sube significativamente el listón pero **no convierte una cuenta no-root
en una sandbox**. Lo que sigue siendo atacable:

1. **La whitelist es el eslabón débil.** Cualquier `NOPASSWD` en
   `/etc/sudoers.d/claude` ejecuta sin pasar por Telegram. Comandos con
   wildcards, args editables (vim/less/find -exec), o que aceptan scripts
   son trivialmente escapables a root shell. Mantén la whitelist mínima
   y revisa cada línea como si fuera un commit a producción.

2. **Telegram es el ancla de confianza.** Quien controle tu cuenta de
   Telegram aprueba sudo. Activa 2FA en Telegram, no logs en
   dispositivos compartidos, audita sesiones activas regularmente.

3. **Una shell de `sergioc` comprometida puede instalar una función
   `sudo()` en `~/.bashrc`** que captura el output del askpass cuando
   sudo lo lee. Mitigación: la password debería ser efímera (token
   one-shot que se invalida tras el uso). Esto requiere PAM custom y
   tampoco está implementado aquí.

4. **Root sigue siendo root.** Quien ya tenga root en el host lee
   `/etc/sudo-telegram/password`, mata el daemon, hace `chattr -a` y
   modifica el log, o reemplaza los binarios. Esto es un *gate* de sudo,
   no un *containment* de privilegio.

5. **Long polling = dependencia outbound a `api.telegram.org`.** Si la
   red bloquea ese host (DNS spoof, firewall, Telegram blocked), sudo
   falla cerrado — comportamiento correcto pero también vector DoS:
   cualquiera que rompa tu red te quita sudo.

6. **El token del bot, si se filtra, permite leer tus prompts** (qué
   estás aprobando) e impersonar al bot. NO permite aprobar — los
   `callback_query` de los botones y los `/yes_<uuid>` de fallback solo
   se aceptan desde tu `chat_id`, que el atacante no controla. Aún así
   rota el token si sospechas filtración.

7. **El bot de aprobaciones puede confundirse con el bot de canales.**
   Diseña los nombres y avatares para que sea visualmente obvio cuál es
   cuál. No reuses tokens entre los dos.

8. **El cmd del prompt es confiable, pero la ejecución posterior no.**
   El daemon lee `/proc/<sudo_pid>/cmdline` como root (con
   `CAP_SYS_PTRACE`), así que ves el `argv` real con el que se invocó
   `sudo`. Lo que no podés ver es qué pasa *después* de que sudo libera
   el password — un binario whitelisteado o un comando que escribe a
   archivos arbitrarios sigue siendo un vector. La verificación real
   exigiría un PAM custom con token one-shot atado al PID; fuera de
   scope.

## Desinstalar

```bash
sudo bash UNINSTALL.sh
```

Borra binarios, unit, sudoers, profile.d. **Deja** `/etc/sudo-telegram/`
y `/var/log/sudo-telegram/` para que tú decidas qué hacer (revisar el log,
borrar la password, etc.).
