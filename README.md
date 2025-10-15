# SRE Self-Healing Monitoring Daemon

A lightweight Python daemon that watches your server's health, automatically fixes common problems using Ansible, and pings you via Discord or Signal when something goes wrong.

---

## Why I Built This

I run a Jellyfin media server and a Nextcloud instance, along with a few other self-hosted services. Managing them manually got old fast — you're either staring at dashboards all day or finding out a service crashed hours after the fact. I wanted something that could sit in the background, catch problems early, and actually *do something* about them instead of just sending an email.

This daemon watches CPU, RAM, and disk usage in real time, tails log files for known error patterns, and when a service dies or the disk fills up it fires off an Ansible playbook to fix it. Everything gets reported to your phone so there's always a record of what happened and when.

---

## Features

- **Metric monitoring** — CPU, RAM, and disk usage checked on a configurable interval
- **Service watchdog** — detects crashed processes and restarts them via Ansible
- **Log scanning** — tails log files and alerts on regex pattern matches
- **Auto-remediation** — triggers log rotation when disk is full, restarts services when they go down
- **Discord or Signal alerts** — your choice of notification provider

---

## Project Structure

```
sre-monitor/
├── monitor.py                  # Main monitoring daemon
├── config.yaml                 # All your settings live here
└── playbooks/
    ├── restart_service.yml     # Ansible: restart a downed systemd service
    └── rotate_logs.yml         # Ansible: free disk space via log rotation
```

---

## Requirements

- Python 3.8+
- Ansible installed on the VPS (`apt install ansible`)
- A Discord webhook URL **or** a Signal account + CallMeBot API key

Install Python dependencies:

```bash
pip install psutil requests pyyaml
```

---

## Setup

### 1. Copy the project to your VPS

```bash
scp -r sre-monitor/ user@your-vps-ip:/opt/sre-monitor
ssh user@your-vps-ip
cd /opt/sre-monitor
pip install psutil requests pyyaml
```

### 2. Set up notifications (pick one)

#### Option A — Discord

1. Open your Discord server
2. Go to **Server Settings → Integrations → Webhooks**
3. Click **New Webhook**, pick a channel, copy the URL
4. In `config.yaml`, set `notification: "discord"` and paste the URL into `discord_webhook`

#### Option B — Signal (via CallMeBot)

CallMeBot is a free service that lets you send Signal messages via a simple HTTP call — no extra software needed on the server.

1. Open Signal on your phone
2. Send a message to **+34 603 21 25 62** with the text:
   ```
   I allow callmebot to send me messages
   ```
3. CallMeBot will reply with your API key
4. In `config.yaml`, set:
   ```yaml
   notification: "signal"
   signal_phone:  "+1XXXXXXXXXX"   # your number, international format
   signal_apikey: "XXXXXXXX"       # the key CallMeBot sent you
   ```

### 3. Edit `config.yaml`

```yaml
notification: "signal"    # or "discord"

check_interval_seconds: 60

thresholds:
  cpu_percent: 85
  memory_percent: 85
  disk_percent: 80

services:
  - jellyfin
  - nginx

log_checks:
  - file: "/var/log/syslog"
    pattern: "Out of memory"
```

> **Note on service names:** The names in `services` must match exactly what appears in `ps aux`. For Jellyfin that's usually `jellyfin`. For Nextcloud it depends on your setup — could be `php-fpm8.2`, `apache2`, or `nginx`. Run `ps aux | grep nextcloud` on your server to check.

### 4. Run it

```bash
python3 monitor.py
```

You'll get a startup notification on whichever provider you configured within a few seconds.

---

## Running as a Background Service (systemd)

To keep it running after you log out, create a systemd unit:

```ini
# /etc/systemd/system/sre-monitor.service

[Unit]
Description=SRE Self-Healing Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/sre-monitor/monitor.py
WorkingDirectory=/opt/sre-monitor
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sre-monitor
sudo systemctl status sre-monitor
```

---

## How Remediation Works

| Problem | What triggers | What happens |
|---|---|---|
| Disk over threshold | `check_disk()` | Runs `rotate_logs.yml` playbook |
| Service process missing | `check_services()` | Runs `restart_service.yml` with the service name |
| CPU / RAM over threshold | `check_cpu()` / `check_memory()` | Alert only (killing random processes is risky) |
| Log pattern matched | `check_logs()` | Alert with the matching line |

---

## Switching Notification Providers

Just change the `notification` field in `config.yaml` and restart the daemon. No code changes needed.

```yaml
notification: "signal"   # switch from discord to signal
```

---

## Notes

- The daemon needs to run as root (or a user with sudo/Ansible privileges) to restart systemd services and write to `/var/log`.
- Log file patterns support full Python regex syntax.
- CallMeBot is a free third-party service. For a fully self-hosted Signal solution, look into `signal-cli`, though it requires more setup.
