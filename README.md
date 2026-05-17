# Bots-Telegram-Claude

Tres bots de Telegram que viven en mi PC torre (CachyOS bare-metal) y
se complementan. El primero es un gate de aprobación para `sudo`, el
segundo expone Claude Code (CLI) por Telegram, y el tercero es un
panel de control de la torre (estado/procesos/servicios/poder).
Juntos permiten hablar con Claude desde el teléfono, que Claude
pueda hacer cosas con privilegio sólo cuando vos las autorizás
manualmente desde otro chat, y operar la máquina (apagar, reiniciar,
chequear servicios) sin tener que abrir SSH.

## Los tres bots

| Carpeta | Bot | Qué hace |
|---|---|---|
| [`sudo-telegram/`](./sudo-telegram/) | `@tu_bot_de_sudo_bot` | `sudo -A <cmd>` te manda el comando al teléfono; respondés con botones ✅/❌ (o `/yes_<id>` / `/no_<id>` como fallback) y la password se libera o sudo falla. |
| [`claude-telegram/`](./claude-telegram/) | bot personal de Claude | Cualquier mensaje (texto, foto, voice note / audio, o documento genérico — PDF/PPT/ZIP/código/…) se le pasa a `claude -p`. La respuesta vuelve al chat streameada en vivo. `/new` empieza sesión nueva. |
| [`Bot_Comandos_Torre/`](./Bot_Comandos_Torre/) | bot de control de la torre | Menú con inline keyboard: estado (uptime/RAM/disco/red/temps), procesos top, servicios, y acciones de poder (apagar/reiniciar/suspender/lock) con confirmación 2-pasos. |

Bots distintos, tokens distintos, chats distintos — diseñado así a
propósito para que no se confundan visualmente y para que un token
filtrado no comprometa a los otros.

## Cómo se integran

`claude-telegram` corre como `sergioc` con `SupplementaryGroups=sudo-telegram`
y exporta `SUDO_ASKPASS=/usr/local/bin/sudo-telegram-askpass`. Resultado:
cuando Claude (invocado por el bot de Claude) ejecuta `sudo -A <cmd>`,
la pregunta de aprobación cae en el bot de sudo. Vos aprobás (o
denegás) desde el teléfono y Claude continúa.

`Bot_Comandos_Torre` vive aparte: corre como `sergioc` y autoriza
las acciones de poder con una regla polkit narrow-scope, sin pasar
por sudo-telegram. No comparte token ni chat con los otros dos.

```
Telegram (bot de Claude)         Telegram (bot de sudo)
        │                                  ▲
        │ "instalá htop"                   │ "🔐 sudo pacman -S htop?"
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

## Estado en este host

Los tres servicios `enabled` (arrancan al boot) y `active`:

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
   instala el daemon, configura el askpass. Después relogueate (o
   `newgrp sudo-telegram`) para tomar el grupo.
2. **`claude-telegram/`** segundo — su systemd unit hereda
   `SUDO_ASKPASS` apuntando al askpass del bot 1, así que la
   integración funciona sola si el bot 1 está instalado.
3. **`Bot_Comandos_Torre/`** independiente — instalable en cualquier
   momento, no depende de los otros dos.

## Qué NO está en este repo (por diseño)

- `/etc/sudo-telegram/config.toml` — bot token + chat_id de aprobación
- `/etc/sudo-telegram/password` — la password real de sudo
- `/etc/claude-telegram/config.toml` — bot token + chat_id del bot de Claude
- `/etc/bot-comandos-torre/config.toml` — bot token + chat_id del panel de control
- `/etc/sudoers.d/claude` — whitelist NOPASSWD (vacía por defecto)
- `/var/log/sudo-telegram/audit.log` — log append-only de aprobaciones

Todos viven en `/etc/` y `/var/log/` con perms estrictos. El repo
contiene sólo el código y los `*.example.toml`.

## Riesgos resumidos

Cada README tiene su sección honesta de "qué sigue siendo atacable".
Los vectores principales:

1. **Tu cuenta de Telegram es el ancla.** Quien la controle puede
   aprobar sudos, hablarle a Claude (con `--dangerously-skip-permissions`,
   eso es shell access) y apagar/reiniciar la torre desde el panel
   de control. Activar 2FA en Telegram es no-opcional.
2. **La whitelist de `/etc/sudoers.d/claude`.** Cualquier `NOPASSWD`
   ahí saltea el bot de aprobación. Está vacía por defecto; agregar
   sólo cosas que no puedan dropear a shell (no `vim`, no `find -exec`,
   etc.).
3. **La regla polkit de `Bot_Comandos_Torre`** le da a `sergioc`
   poweroff/reboot/suspend/lock sin auth en cualquier contexto, no
   solo cuando el bot la invoca. Si una shell de `sergioc` queda
   comprometida, también puede apagar la máquina sin prompt.
