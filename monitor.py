#!/usr/bin/env python3
"""
monitor.py - SRE Self-Healing Monitoring Daemon

Watches system metrics (CPU, RAM, Disk) and scans log files for error patterns.
When something bad happens, it automatically runs Ansible playbooks to fix it
and sends an alert via Discord or Signal so you know what went down.
"""

import os
import re
import time
import subprocess
from datetime import datetime

import psutil
import requests
import yaml


# ──────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────

def load_config(path="config.yaml"):
    """Read the YAML config file and return it as a dict."""
    with open(path, "r") as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────
#  Alerting (Discord or Signal)
# ──────────────────────────────────────────────

def send_alert(config, message, color=0xFF4444):
    """
    Route the alert to whichever provider is configured.
    Set 'notification' in config.yaml to either 'discord' or 'signal'.

    color is only used for Discord embeds.
    """
    provider = config.get("notification", "discord")

    if provider == "signal":
        _send_signal(config, message)
    else:
        _send_discord(config, message, color)


def _send_discord(config, message, color=0xFF4444):
    """
    Send a formatted embed message to Discord via webhook.

    color codes:
      0xFF4444 = red   (critical / failure)
      0xFFAA00 = amber (warning)
      0x44FF88 = green (resolved / success)
      0x4488FF = blue  (info / startup)
    """
    webhook_url = config["discord_webhook"]
    payload = {
        "embeds": [{
            "title": "SRE Monitor",
            "description": message,
            "color": color,
            "timestamp": datetime.utcnow().isoformat()
        }]
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Could not send Discord alert: {e}")


def _send_signal(config, message):
    """
    Send a plain-text message via Signal using the CallMeBot API.

    Setup: text +34 603 21 25 62 on Signal with the message:
      I allow callmebot to send me messages
    CallMeBot will reply with your API key.
    Docs: https://www.callmebot.com/blog/free-api-signal-send-messages/
    """
    phone  = config["signal_phone"]    # your number in international format, e.g. +12025550100
    apikey = config["signal_apikey"]   # key you get from CallMeBot

    # CallMeBot uses a simple GET request with URL params
    params = {
        "phone":  phone,
        "apikey": apikey,
        "text":   message,
    }

    try:
        resp = requests.get("https://api.callmebot.com/signal/send.php", params=params, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[ERROR] Could not send Signal alert: {e}")


# ──────────────────────────────────────────────
#  Ansible Playbook Runner
# ──────────────────────────────────────────────

def run_playbook(playbook_path, extra_vars=None):
    """
    Execute an Ansible playbook via subprocess.
    Returns True if it succeeded, False if it failed.

    extra_vars is an optional dict like {"service_name": "nginx"}
    """
    cmd = ["ansible-playbook", playbook_path]

    if extra_vars:
        # Ansible expects --extra-vars "key=value key2=value2"
        vars_str = " ".join(f"{k}={v}" for k, v in extra_vars.items())
        cmd += ["--extra-vars", vars_str]

    print(f"[INFO] Running playbook: {playbook_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"[ERROR] Playbook failed:\n{result.stderr}")
        return False

    print(f"[INFO] Playbook succeeded.")
    return True


# ──────────────────────────────────────────────
#  Metric Checks
# ──────────────────────────────────────────────

def check_cpu(config):
    """Alert if CPU usage is over the configured threshold."""
    threshold = config["thresholds"]["cpu_percent"]
    # interval=1 tells psutil to measure over 1 second for accuracy
    usage = psutil.cpu_percent(interval=1)

    if usage >= threshold:
        msg = f"🚨 High CPU usage: {usage:.1f}% (limit: {threshold}%)"
        print(f"[WARN] {msg}")
        send_alert(config, msg, color=0xFFAA00)

    return usage


def check_memory(config):
    """Alert if RAM usage is over the configured threshold."""
    threshold = config["thresholds"]["memory_percent"]
    mem = psutil.virtual_memory()
    usage = mem.percent

    if usage >= threshold:
        msg = f"🚨 High memory usage: {usage:.1f}% (limit: {threshold}%)"
        print(f"[WARN] {msg}")
        send_alert(config, msg, color=0xFFAA00)

    return usage


def check_disk(config):
    """
    Check disk usage on the configured path.
    If over threshold, trigger the log-rotation playbook automatically.
    """
    threshold = config["thresholds"]["disk_percent"]
    path = config.get("disk_path", "/")
    usage = psutil.disk_usage(path).percent

    if usage >= threshold:
        msg = (
            f"⚠️ Disk usage critical on {path}: {usage:.1f}% "
            f"(limit: {threshold}%) — triggering log rotation..."
        )
        print(f"[WARN] Disk at {usage:.1f}% on {path} — running rotate_logs.yml")
        send_alert(config, msg, color=0xFF4444)

        # Auto-remediation: rotate logs to reclaim space
        success = run_playbook("playbooks/rotate_logs.yml")
        if success:
            send_alert(config, "✅ Log rotation completed.", color=0x44FF88)
        else:
            send_alert(config, "❌ Log rotation failed. Manual intervention needed.", color=0xFF4444)

    return usage


def check_services(config):
    """
    Check whether each monitored service process is running.
    If a service is down, trigger the restart playbook.
    """
    for service in config.get("services", []):
        # Build a set of running process names to check against
        running_names = {p.name() for p in psutil.process_iter(["name"])}
        is_running = service in running_names

        if not is_running:
            msg = f"🔴 Service '{service}' is DOWN. Attempting restart..."
            print(f"[WARN] {service} not found in process list")
            send_alert(config, msg, color=0xFF4444)

            success = run_playbook(
                "playbooks/restart_service.yml",
                extra_vars={"service_name": service}
            )

            if success:
                send_alert(config, f"✅ '{service}' restarted successfully.", color=0x44FF88)
            else:
                send_alert(config, f"❌ Could not restart '{service}'. Check the server now!", color=0xFF4444)


def check_logs(config):
    """
    Scan the tail of each configured log file for error patterns.
    Sends an alert with the last matching line if anything is found.
    """
    log_checks = config.get("log_checks", [])

    for check in log_checks:
        log_file = check["file"]
        pattern = check["pattern"]

        if not os.path.exists(log_file):
            continue

        recent_lines = tail_file(log_file, lines=50)
        matches = [line for line in recent_lines if re.search(pattern, line, re.IGNORECASE)]

        if matches:
            # Show the most recent matching line as a sample
            sample = matches[-1].strip()
            msg = f"🔍 Pattern '{pattern}' matched in {log_file}:\n{sample}"
            print(f"[WARN] Pattern '{pattern}' found in {log_file}")
            send_alert(config, msg, color=0xFFAA00)


def tail_file(filepath, lines=50):
    """Return the last N lines of a file as a list of strings."""
    try:
        with open(filepath, "r", errors="replace") as f:
            return f.readlines()[-lines:]
    except Exception as e:
        print(f"[ERROR] Could not read {filepath}: {e}")
        return []


# ──────────────────────────────────────────────
#  Main Loop
# ──────────────────────────────────────────────

def main():
    print("[INFO] Starting SRE Monitoring Daemon...")
    config = load_config()

    interval = config.get("check_interval_seconds", 60)
    provider = config.get("notification", "discord")
    print(f"[INFO] Notification provider: {provider}")

    send_alert(config, "🔵 SRE Monitor is online and watching.", color=0x4488FF)

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] Running checks...")

        cpu  = check_cpu(config)
        mem  = check_memory(config)
        disk = check_disk(config)

        print(f"  CPU: {cpu:.1f}%  |  RAM: {mem:.1f}%  |  Disk: {disk:.1f}%")

        check_services(config)
        check_logs(config)

        time.sleep(interval)


if __name__ == "__main__":
    main()
