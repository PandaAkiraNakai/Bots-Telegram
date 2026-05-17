# Bot Comandos Torre

Bot de Telegram para controlar y **monitorear** esta torre desde el celular.

Hace dos cosas:

1. **Pull** (pulsas botones, te contesta): estado, procesos, servicios,
   logs, GPU, SMART, updates, tendencias, otros hosts vía SSH, **lanzar
   apps GUI en tu sesión**, **encender/apagar monitores (niri DPMS) y
   reordenar el layout**, **cambiar la salida de audio (pactl/PipeWire,
   moviendo los streams activos)**, acciones de poder.
2. **Push** (te avisa solo): alertas con histéresis (CPU, RAM, disco,
   temps, GPU, load), servicios que pasan a failed, nuevas sesiones,
   retorno de suspend, **torre iniciada** (boot real del PC, con el
   menú principal pegado para empezar a navegar).

Hermano de los proyectos `sudo-telegram` y `claude-telegram`. A diferencia
de esos, **no** usa askpass ni socket: corre como `sergioc`, las acciones
de poder y de control de servicios se autorizan vía una regla polkit
narrow-scope (`50-bot-comandos-torre.rules`).

UI vía inline keyboard. Solo responde al `chat_id` autorizado en config —
cualquier otro chat se ignora silenciosamente.

## Arquitectura

```
   Telegram bot  ←→  api.telegram.org  ←→  long-polling
                                             │
                                             ▼
                                  bot-comandos-torre  (sergioc, systemd)
                                  ├─ main thread
                                  │   ├─ valida chat_id
                                  │   ├─ render menú según callback_data
                                  │   ├─ Estado (sistema/disco/red/temps/GPU/SMART)
                                  │   ├─ Procesos (top + kill)
                                  │   ├─ Servicios (failed/clave/start-stop-restart)
                                  │   ├─ Logs (journalctl -p err)
                                  │   ├─ Updates (checkupdates)
                                  │   ├─ Tendencia (PNG con matplotlib)
                                  │   ├─ Otros hosts (SSH multihost)
                                  │   ├─ Apps (lanzar GUI vía systemd-run --user)
                                  │   ├─ Pantallas (niri DPMS + on/off por output)
                                  │   ├─ Audio (pactl: cambiar sink + mover streams)
                                  │   └─ Poder (off/reboot/suspend/lock)
                                  │
                                  └─ monitor thread (cada interval_s)
                                      ├─ snapshot de métricas → SQLite
                                      ├─ chequeo de umbrales (alertas)
                                      ├─ delta de servicios failed / sesiones
                                      └─ detección de resume

   En `main()`, antes del monitor: setMyCommands + setChatMenuButton
   (botón "Menu" al lado del input con todos los slash commands), y
   notificación "Torre iniciada" con el menú principal — deduplicada
   por boot_id, una vez por boot real del PC.
```

## Inventario (después de instalar)

| Path | Dueño | Perms | Qué es |
|---|---|---|---|
| `/usr/local/bin/bot-comandos-torre` | root:root | 0755 | El daemon (Python stdlib + matplotlib opcional) |
| `/etc/bot-comandos-torre/config.toml` | sergioc:sergioc | 0400 | Token + chat_id + umbrales + hosts |
| `/etc/systemd/system/bot-comandos-torre.service` | root:root | 0644 | Unit (StateDirectory + LogsDirectory) |
| `/etc/polkit-1/rules.d/50-bot-comandos-torre.rules` | root:root | 0644 | Permite a sergioc poweroff/reboot/suspend/lock + manage-units narrow-scope |
| `/var/lib/bot-comandos-torre/metrics.db` | sergioc:sergioc | 0600 | SQLite con histórico de métricas (WAL) |
| `/var/lib/bot-comandos-torre/known_hosts` | sergioc:sergioc | 0600 | known_hosts dedicado para multihost SSH |
| `/var/lib/bot-comandos-torre/last_boot_id` | sergioc:sergioc | 0644 | Boot ID del último arranque notificado (dedup de "Torre iniciada") |
| `/var/log/bot-comandos-torre/audit.log` | sergioc:sergioc | 0640 + `chattr +a` | Auditoría JSONL append-only |

## Instalar

1. Crear el bot en BotFather (`/newbot`), anotar el token.

2. Instalar:

   ```bash
   cd ~/.bots/Bot_Comandos_Torre
   sudo bash INSTALL.sh
   ```

   Es idempotente: re-correrlo solo upgrade-a binario, unit y polkit rule.
   Nunca pisa `/etc/bot-comandos-torre/config.toml` si ya existe.

3. Editar config y pegar el token (la primera vez):

   ```bash
   sudo -u sergioc $EDITOR /etc/bot-comandos-torre/config.toml
   # bot_token = "<el de BotFather>"
   # chat_id   = <tu_chat_id_numerico>
   ```

4. Arrancar:

   ```bash
   sudo systemctl start bot-comandos-torre
   sudo journalctl -fu bot-comandos-torre
   ```

5. En Telegram, envíale `/start` o `/menu` al bot. Debe mostrar el menú.

6. Si todo OK, habilitar al boot:

   ```bash
   sudo systemctl enable bot-comandos-torre
   ```

### Paquetes opcionales

El bot funciona con stdlib de Python 3.11+. Estos extras prenden features:

| Paquete | Qué habilita |
|---|---|
| `lm_sensors` | sensores de temperatura (sin esto cae a `/sys/class/thermal`) |
| `python-matplotlib` | gráficos PNG en `📈 Tendencia` (sin esto cae a texto) |
| `pacman-contrib` | comando `checkupdates` en `📦 Updates` |
| `smartmontools` | reportes SMART en `📊 SMART` |
| `niri` corriendo | menú `🖥 Pantallas` (vía `niri msg` sobre `/run/user/<uid>/niri.*.sock`) |
| `pipewire-pulse` o `pulseaudio` | menú `📢 Audio` (vía `pactl`) |

`INSTALL.sh` te avisa al final cuáles te faltan.

## Comandos del bot

Al arrancar, el daemon registra los comandos contra Telegram
(`setMyCommands` + `setChatMenuButton`), así que aparece un botón **Menu**
al lado del campo de texto con la lista entera:

| Comando | Qué abre |
|---|---|
| `/menu` (o `/start`) | Menú principal |
| `/estado` | Estado del sistema (sub-menú: sistema/disco/red/temps/GPU/SMART) |
| `/procesos` | Top procesos (CPU / RAM) |
| `/servicios` | Servicios (fallidos / clave / controlar) |
| `/logs` | Logs recientes |
| `/tendencia` | Gráficas de tendencia (1 h / 6 h / 24 h) |
| `/updates` | Updates pendientes |
| `/hosts` | Otros hosts (SSH) |
| `/apps` | Lanzar aplicaciones GUI |
| `/pantallas` | Encender / apagar monitores (niri DPMS + on/off por output) |
| `/audio` | Cambiar la salida de audio (sink default + mover streams) |
| `/poder` | Acciones de poder (off / reboot / suspend / lock) |
| `/ping` | Health check (responde `pong`) |
| `/help` | Lista de comandos |

Cada slash command abre el mismo sub-menú que el botón inline equivalente.
A partir de ahí se navega con botones. Cualquier otro mensaje se ignora
silenciosamente.

## Estructura de menús

```
Main
├─ 📊 Estado
│   ├─ Sistema       (host, kernel, uptime, CPU%, cores, load, RAM)
│   ├─ Disco         (df por mount, %)
│   ├─ Red           (interfaces, throughput rx/tx, ping a hosts)
│   ├─ Temperaturas  (lm_sensors o /sys/class/thermal)
│   ├─ GPU           (uso %, VRAM, temp, power, fan — AMD + NVIDIA)
│   └─ SMART         (smartctl -H por device — opcional, ver config)
├─ 🧠 Procesos
│   ├─ Top CPU       (ps top 10 + botón ☠ kill por PID)
│   └─ Top RAM       (idem)
├─ 🔧 Servicios
│   ├─ Fallidos      (systemctl --state=failed)
│   ├─ Activos clave (sshd, NetworkManager, sudo-telegram, …)
│   └─ Controlar     (start/stop/restart por unit del whitelist)
├─ 📜 Logs           (journalctl -p err -n 30)
├─ 📈 Tendencia      (1 h / 6 h / 24 h, gráfico PNG)
├─ 📦 Updates        (checkupdates)
├─ 🌐 Otros hosts    (SSH a vpspriv, vpsgames, etc.)
├─ 🚀 Apps          (botones por app configurada → lanza directo, sin confirmar)
├─ 🖥 Pantallas     (lista outputs de niri; on/off por output + DPMS global)
├─ 📢 Audio         (lista sinks de pactl; ✅ marca el default; click cambia
│                    el default y mueve los streams activos al sink nuevo)
└─ ⚡ Poder
    ├─ 🔴 Apagar      → confirmación → systemctl poweroff
    ├─ 🔁 Reiniciar   → confirmación → systemctl reboot
    ├─ 💤 Suspender   → confirmación → systemctl suspend
    └─ 🔒 Bloquear    → confirmación → loginctl lock-sessions
```

Cada acción de poder, kill de proceso, o start/stop/restart de servicio
pide confirmación `✅ Sí / ❌ No` antes de ejecutarse, y el mensaje se
edita en lugar para mostrar el resultado. Los lanzamientos de apps NO
piden confirmación (son inocuos).

## Alertas (push)

El monitor thread muestrea cada `interval_s` (default 60) y dispara
alertas con histéresis (`hi` para arm, `lo` para rearm) y cooldown.

Alertas configurables (con sus defaults):

| Métrica | hi | lo | cooldown |
|---|---|---|---|
| `cpu_pct` | 90 % | 75 % | 10 min |
| `ram_pct` | 90 % | 80 % | 10 min |
| `disk_pct` | 90 % | 80 % | 60 min |
| `cpu_temp_c` | 85 °C | 75 °C | 10 min |
| `gpu_temp_c` | 85 °C | 75 °C | 10 min |
| `gpu_pct` | 95 % | 80 % | 15 min |
| `load1_per_core` | 2.0 | 1.5 | 10 min |

Cada alerta de CPU / RAM / disco / load incluye un snapshot de los
top-5 procesos del recurso afectado. Cada alerta trae un botón
**🔕 Silenciar 1 h** que arma el snooze para esa métrica.

Eventos sin umbral (también push):

- Servicio que pasa a `failed`
- Nueva sesión de logind
- Retorno de suspend (gap > 60 s entre ticks del monitor)
- **Torre iniciada** — al startup del daemon, si el `boot_id` actual
  (`/proc/sys/kernel/random/boot_id`) difiere del último persistido en
  `/var/lib/bot-comandos-torre/last_boot_id`. Garantiza una notificación
  por boot real del PC, sin falsos positivos por `systemctl restart`
  manual. El mensaje trae el menú principal pegado para empezar a
  navegar de inmediato.

## Histórico

`/var/lib/bot-comandos-torre/metrics.db` (SQLite WAL). Snapshot por minuto
de CPU%, RAM%, GPU%, T CPU, T GPU, load1, Disco%. Retain 14 días por
default. Prune cada 6 h.

`📈 Tendencia` lee desde ahí y arma un PNG de 2 paneles (porcentajes y
temperaturas) si matplotlib está disponible.

## Multihost

Cualquier `[hosts.<nombre>]` en config aparece como botón en `🌐 Otros
hosts`. SSH usa `BatchMode=yes ConnectTimeout=5` así que un host caído
no cuelga la UI. El primer connect a cada host pobla
`/var/lib/bot-comandos-torre/known_hosts` (TOFU vía
`StrictHostKeyChecking=accept-new`).

```toml
[hosts.vpspriv]
ssh_alias = "vpspriv"

[hosts.vpsgames]
ssh_alias = "vpsgames"
```

`ssh_alias` se resuelve vía `/home/sergioc/.ssh/config` (que el daemon
lee read-only por `ProtectHome=read-only`). Las claves privadas también
se leen desde ahí.

## Apps (lanzar GUI desde el celular)

Cada `[apps.<nombre>]` en config aparece como botón en `🚀 Apps`. Al
clickear, el bot ejecuta:

```
systemd-run --user --collect --quiet -- <cmd>
```

con `XDG_RUNTIME_DIR=/run/user/<uid>` en el env, así el `systemd --user`
de tu sesión arranca la app heredando `DISPLAY` / `WAYLAND_DISPLAY` /
`DBUS_SESSION_BUS_ADDRESS` / etc. Sin shell expansion, sin globs:
`cmd` es lista directa de strings.

```toml
[apps.firefox]
cmd = ["/usr/lib/firefox/firefox"]
label = "🦊 Firefox"

[apps.obsidian-ciencias]
cmd = ["obsidian", "obsidian://open?vault=Ciencias-"]
label = "📓 Obsidian (Ciencias)"
```

**Requisitos**:

- Sesión gráfica activa de `sergioc` (login abierto). Sin sesión, el
  `systemd --user` no está corriendo y `systemd-run --user` falla con
  *"Failed to connect to bus"*. Si quieres que funcione sin sesión:
  `loginctl enable-linger sergioc` (no recomendado — apps GUI quedan
  huérfanas hasta que abras niri).
- Path absoluto si el binario no está en el `PATH` del environment
  del daemon (ver el unit). Para apps en `/opt/...` o paquetes con
  binarios fuera de `/usr/bin`, usa el path completo.

A diferencia de poder/kill/svc, los lanzamientos de apps **no** piden
confirmación (son inocuos). Se loggean en el audit log como `app_launch`.

## Polkit rule

`50-bot-comandos-torre.rules` deja al usuario `sergioc` invocar **sin auth**:

- `org.freedesktop.login1.power-off` (+ `multiple-sessions`)
- `org.freedesktop.login1.reboot` (+ `multiple-sessions`)
- `org.freedesktop.login1.suspend` (+ `multiple-sessions`)
- `org.freedesktop.login1.lock-sessions`
- `org.freedesktop.systemd1.manage-units` (solo para units en una whitelist
  hardcoded en la rule: sshd, NetworkManager, systemd-resolved,
  sudo-telegram, claude-telegram, docker)

`bot-comandos-torre.service` **no** está en la whitelist de manage-units —
así el bot no puede reiniciarse a sí mismo (eso lo haces por SSH o sudo).

Si no quieres alguna de estas concesiones, edita la rule. El bot sigue
funcionando para reportes aunque la rule no exista — solo fallarán las
acciones afectadas con *"Interactive authentication required"*.

`INSTALL.sh` valida que `[services].manageable` esté alineada con
`allowedUnits` de la rule polkit, y warnea si hay drift (servicios
expuestos en el menú que polkit no autoriza). El daemon también lo
intenta al startup, pero `/etc/polkit-1/rules.d/` es típicamente
`0750 root:polkitd` así que no puede leerla — silenciosamente cae a la
validación que hizo INSTALL.sh.

## Auditoría

`/var/log/bot-comandos-torre/audit.log` es JSONL append-only (`chattr +a`).
El daemon graba:

- `start` — startup del proceso
- `callback` — todo callback recibido (con `data`)
- `power` — acción de poder ejecutada
- `kill` — kill de PID solicitado
- `svc` — start/stop/restart de servicio
- `alert` — alerta disparada (con `key` y `value`)
- `snooze` — silencio manual de alerta
- `service_failed` — push de servicio que pasó a failed
- `session_new` — nueva sesión de logind
- `boot` — push de torre iniciada (incluye `boot_id`)
- `resume` — push de retorno de suspend (con `gap_s`)
- `app_launch` — lanzamiento de app GUI vía `systemd-run --user`
- `audio_set` — cambio de sink default (`sink`, `result`)
- `pantallas_set` — on/off de un output de niri (`output`, `action`, `result`)
- `pantallas_dpms` — DPMS global on/off (`action`, `result`)
- `chat_reaped` — lote de mensajes auto-borrados por el reaper (`count`)

Para leer (requiere ser sergioc o root):

```bash
tail -f /var/log/bot-comandos-torre/audit.log | jq .
```

## Auto-borrado del chat

Por default (`chat_auto_delete_s = 1800` en `config.toml`) el bot
**borra solo sus propios mensajes a los 30 min**. Hace falta porque
el bot genera mucho volumen (alertas push, reportes pedidos por
botón, gráficos de tendencia, "torre iniciada" por boot) y sin
auto-borrado el chat se vuelve un scroll infinito. Un thread reaper
chequea cada `chat_auto_delete_interval_s` (default 60s).
`chat_auto_delete_s = 0` desactiva.

Telegram solo permite a un bot borrar **sus propios** mensajes y solo
hasta 48h después de enviados. Los taps de botones (callback queries)
y los pocos mensajes que tú envías al bot no se tocan. Cada lote de
borrados se loggea como `chat_reaped` (con `count`) en el audit.

## Operaciones

| Acción | Comando |
|---|---|
| Estado | `sudo systemctl status bot-comandos-torre` |
| Logs | `sudo journalctl -fu bot-comandos-torre` |
| Reiniciar | `sudo systemctl restart bot-comandos-torre` |
| Cambiar token | `sudo -u sergioc $EDITOR /etc/bot-comandos-torre/config.toml` + restart |
| Health check | enviar `/ping` al bot |
| Reinstalar / upgrade | `sudo bash ~/.bots/Bot_Comandos_Torre/INSTALL.sh` |
| Desinstalar | `sudo bash UNINSTALL.sh` |

## Riesgos residuales

1. **Cualquiera con tu cuenta de Telegram puede apagar/reiniciar la torre,
   matar procesos o reiniciar servicios.** El gate único es el `chat_id`.
   Activa 2FA en Telegram. No te conectes desde dispositivos compartidos.

2. **El token del bot, si se filtra, permite leer todos los mensajes que
   te llegan al bot** e impersonarlo. NO permite ejecutar acciones — los
   `callback_query` solo se aceptan desde tu `chat_id`. Aun así, rota el
   token si sospechas filtración (BotFather → `/token`).

3. **La regla polkit es ancha en el sentido de que aplica a sergioc en
   *cualquier* contexto** (no solo cuando el bot la invoca). Si una shell
   de sergioc quedara comprometida, también podría apagar/reiniciar o
   tocar los servicios whitelist sin prompt. Mitigación posible (no
   implementada): chequear `subject.unit` en la regla polkit para
   limitarla a la unit del bot.

4. **SSH multihost usa TOFU.** El primer connect a un host nuevo acepta
   y graba la clave automáticamente. Para hardening: pre-poblar
   `/var/lib/bot-comandos-torre/known_hosts` con `ssh-keyscan` antes de
   habilitar el host.

5. **Auditoría persistente parcial.** El audit.log es append-only por
   `chattr +a` (sergioc puede agregar pero no truncar). Pero root sí
   puede borrarlo. Si alguien escala a root, puede limpiar tracks.

## Desinstalar

```bash
sudo bash UNINSTALL.sh
```

Borra binario, service unit, y la regla polkit. **Deja**:

- `/etc/bot-comandos-torre/` (token + config)
- `/var/lib/bot-comandos-torre/` (SQLite + known_hosts)
- `/var/log/bot-comandos-torre/` (audit.log con `chattr +a`)

Para borrar todo manualmente:

```bash
sudo chattr -a /var/log/bot-comandos-torre/audit.log
sudo rm -rf /etc/bot-comandos-torre /var/lib/bot-comandos-torre /var/log/bot-comandos-torre
```
