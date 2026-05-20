# Bots-Telegram-Claude

Tres bots de Telegram que viven en una PC de escritorio (CachyOS
bare-metal) y se complementan. El primero es un gate de aprobación
para `sudo`, el segundo expone Claude Code (CLI) por Telegram, y el
tercero es un panel de control de la máquina (estado / procesos /
servicios / energía). Juntos permiten hablar con Claude desde el
teléfono, que Claude pueda ejecutar acciones con privilegio solo
cuando las autorizas manualmente desde otro chat, y operar la máquina
(apagar, reiniciar, revisar servicios) sin abrir una sesión SSH.

## Los tres bots

| Carpeta | Bot | Qué hace |
|---|---|---|
| [`sudo-telegram/`](./sudo-telegram/) | `@tu_bot_de_sudo_bot` | `sudo -A <cmd>` te envía el comando al teléfono; respondes con botones ✅/❌ (o `/yes_<id>` / `/no_<id>` como fallback) y la contraseña se libera o `sudo` falla. |
| [`claude-telegram/`](./claude-telegram/) | bot personal de Claude | Cualquier mensaje (texto, foto, nota de voz / audio, o documento genérico — PDF/PPT/ZIP/código/…) se pasa a `claude -p`. La respuesta vuelve al chat con streaming en vivo. `/new` inicia una sesión nueva. |
| [`Bot_Comandos_Torre/`](./Bot_Comandos_Torre/) | bot de control de la máquina | Menú con inline keyboard: estado (uptime / RAM / disco / red / temperaturas / GPU / SMART), procesos top con kill, servicios systemd (failed / clave / start-stop-restart), updates, tendencias en PNG, lanzar GUI apps, **control de pantallas (niri)**, **audio (pactl)**, **media (playerctl/MPRIS)**, **scan de LAN + Wake-on-LAN**, **bridge SSH a VPS**, **lanzar juegos de Steam (con gamescope opcional)**, **notas rápidas a Obsidian**, y acciones de energía (apagar / reiniciar / suspender / bloquear) con confirmación en dos pasos. Estética cyberpunk en menús y push de "Torre iniciada". |

Bots distintos, tokens distintos, chats distintos — diseñado así a
propósito para que no se confundan visualmente y para que un token
filtrado no comprometa a los otros.

## Cómo se integran

`claude-telegram` corre como `sergioc` con `SupplementaryGroups=sudo-telegram`
y exporta `SUDO_ASKPASS=/usr/local/bin/sudo-telegram-askpass`. Resultado:
cuando Claude (invocado por el bot de Claude) ejecuta `sudo -A <cmd>`,
la pregunta de aprobación llega al bot de sudo. Apruebas (o deniegas)
desde el teléfono y Claude continúa.

`Bot_Comandos_Torre` vive aparte: corre como `sergioc` y autoriza
las acciones de energía con una regla polkit de alcance acotado, sin
pasar por sudo-telegram. No comparte token ni chat con los otros dos.

```
Telegram (bot de Claude)         Telegram (bot de sudo)
        │                                  ▲
        │ "instala htop"                   │ "🔐 sudo pacman -S htop?"
        ▼                                  │
   claude-telegram-daemon                  │
        │                                  │
        ▼  claude -p --dangerously-skip-permissions ...
   claude (CLI)                            │
        │                                  │
        ▼  Bash: `sudo -A pacman -S htop`  │
   sudo → askpass → /run/sudo-telegram/sock
                          │
                          ▼
                   sudo-telegram-daemon ───┘
```

## Estado en el host

Los tres servicios quedan `enabled` (arrancan al boot) y `active`:

```bash
systemctl status sudo-telegram
systemctl status claude-telegram
systemctl status bot-comandos-torre
```

Logs en vivo:

```bash
sudo journalctl -fu sudo-telegram
sudo journalctl -fu claude-telegram
sudo journalctl -fu bot-comandos-torre
```

## Instalar desde cero

Cada subcarpeta tiene su propio README y `INSTALL.sh` idempotente.
Orden recomendado:

1. **`sudo-telegram/`** primero — crea el grupo `sudo-telegram`,
   instala el daemon, configura el askpass. Después vuelve a iniciar
   sesión (o ejecuta `newgrp sudo-telegram`) para tomar el grupo.
2. **`claude-telegram/`** segundo — su unit de systemd hereda
   `SUDO_ASKPASS` apuntando al askpass del bot 1, así que la
   integración funciona sola si el bot 1 está instalado.
3. **`Bot_Comandos_Torre/`** independiente — instalable en cualquier
   momento, no depende de los otros dos.

## Qué NO está en este repo (por diseño)

- `/etc/sudo-telegram/config.toml` — bot token + chat_id de aprobación
- `/etc/sudo-telegram/password` — la contraseña real de sudo
- `/etc/claude-telegram/config.toml` — bot token + chat_id del bot de Claude
- `/etc/bot-comandos-torre/config.toml` — bot token + chat_id del panel de control
- `/etc/sudoers.d/claude` — whitelist `NOPASSWD` (vacía por defecto)
- `/var/log/sudo-telegram/audit.log` — log append-only de aprobaciones

Todos viven en `/etc/` y `/var/log/` con permisos estrictos. El repo
contiene solo el código y los `*.example.toml`.

## Riesgos resumidos

Cada README tiene su sección honesta de "qué sigue siendo atacable".
Los vectores principales:

1. **Tu cuenta de Telegram es el ancla de confianza.** Quien la
   controle puede aprobar sudos, hablarle a Claude (con
   `--dangerously-skip-permissions`, eso es acceso a shell) y
   apagar / reiniciar la máquina desde el panel de control. Activar
   2FA en Telegram no es opcional.
2. **La whitelist de `/etc/sudoers.d/claude`.** Cualquier entrada
   `NOPASSWD` salta el bot de aprobación. Está vacía por defecto;
   agrega solo cosas que no puedan caer a shell (no `vim`, no
   `find -exec`, etc.).
3. **La regla polkit de `Bot_Comandos_Torre`** le otorga a `sergioc`
   `poweroff` / `reboot` / `suspend` / `lock` sin auth en cualquier
   contexto, no solo cuando el bot la invoca. Si una shell de
   `sergioc` queda comprometida, también puede apagar la máquina
   sin prompt.
