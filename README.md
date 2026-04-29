# Windrose Server Player Monitor

A lightweight tool for Windrose dedicated server owners to track who's online in real time. It tails your server's `R5.log`, shows a live web dashboard, and can post join/leave notifications to a Discord channel.

**No external dependencies — pure Python standard library.**

![Dashboard preview showing online players and recently seen players]

---

## Features

- Live web dashboard showing who's online right now
- "Recently seen" list of players who logged off
- Server info: name, player cap, uptime, password status
- Optional Discord notifications when players join or leave
- JSON API endpoint for custom integrations
- Pirate-flavored join/leave messages (yarr!)

---

## Requirements

- Python 3.8 or newer
- Access to your Windrose dedicated server's `R5.log` file

That's it. No pip installs needed.

---

## Quick Start

1. **Copy the script** to any folder on the machine running your Windrose server (or a machine that can see the log file).

2. **Run it**, pointing it at your log file:

   ```bash
   python windrose_monitor.py --log "C:\path\to\R5.log"
   ```

3. **Open your browser** and go to:

   ```
   http://localhost:8080
   ```

   You'll see the live player dashboard. It refreshes automatically every 10 seconds. Others on your network can reach it at your machine's IP address on port 8080.

> **Exposing to the internet:** The easiest way is a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) — no router port forwarding needed. Cloudflare gives you a public HTTPS URL that tunnels straight to your local port 8080. Free to use.

---

## Discord Notifications

Set the `DISCORD_WEBHOOK_URL` environment variable to your Discord webhook URL before running the script.

**Windows (Command Prompt):**
```cmd
set DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK
python windrose_monitor.py --log "C:\path\to\R5.log"
```

**Windows (PowerShell):**
```powershell
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/YOUR/WEBHOOK"
python windrose_monitor.py --log "C:\path\to\R5.log"
```

**Linux / macOS:**
```bash
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/YOUR/WEBHOOK" python windrose_monitor.py --log /path/to/R5.log
```

To get a webhook URL: open your Discord server → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.

---

## All Options

```
python windrose_monitor.py --help
```

| Option | Default | Description |
|---|---|---|
| `--log <path>` | `R5.log` | Path to your Windrose server log file |
| `--host <ip>` | `0.0.0.0` | IP address to listen on (`127.0.0.1` for local-only) |
| `--port <port>` | `8080` | Port for the web dashboard |
| `--no-replay` | off | Skip reading existing log history; only process new events |
| `-v` / `--verbose` | off | Enable detailed debug logging |

---

## API Endpoint

The monitor also serves a JSON API at `/api/players`:

```
http://127.0.0.1:8080/api/players
```

Response example:
```json
{
  "online_count": 2,
  "online": [...],
  "recently_seen": [...],
  "server_info": {
    "server_name": "My Windrose Server",
    "max_players": 10,
    "password_protected": false
  }
}
```

---

## Running as a Background Service

### Windows — Task Scheduler

1. Open **Task Scheduler** → Create Basic Task
2. Set trigger to "When the computer starts"
3. Action: Start a program → `python.exe`, arguments: `C:\path\to\windrose_monitor.py --log "C:\path\to\R5.log" --host 0.0.0.0`
4. Check "Run whether user is logged on or not"

### Linux — systemd

Create `/etc/systemd/system/windrose-monitor.service`:

```ini
[Unit]
Description=Windrose Server Player Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/windrose/windrose_monitor.py --log /path/to/R5.log --host 0.0.0.0
Environment=DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable windrose-monitor
sudo systemctl start windrose-monitor
```

---

## Troubleshooting

**The dashboard is empty / shows no players**
- Make sure `--log` points to the correct `R5.log` file.
- The monitor reads from the last player dump in the log on startup. If the server just started, wait a minute for the first dump to appear.

**Discord notifications aren't arriving**
- Double-check your webhook URL is correct.
- Check the terminal output for any warning messages about the webhook.

**I can't access the dashboard from another computer**
- Make sure your firewall allows inbound traffic on port `8080`.
- For internet access without touching your router, use a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) — it's free and takes about 5 minutes to set up.
- Use `--host 127.0.0.1` if you want to restrict access to the local machine only.

---

## How It Works

Windrose's dedicated server writes periodic player state dumps to `R5.log`. The monitor tails this file, parses the `Connected Accounts` / `Reserved Accounts` / `Disconnected Accounts` sections, and maintains a live roster. When the script starts, it fast-seeks to the most recent dump so startup is instant even on large log files.

---

## License

MIT — do whatever you like with it.
