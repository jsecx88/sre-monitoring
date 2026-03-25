#!/usr/bin/env python3
"""
monitor.py - SRE Self-Healing Monitoring Daemon

Watches system metrics (CPU, RAM, Disk) and scans log files for error patterns.
When something bad happens, it automatically runs Ansible playbooks to fix it
and sends an alert via Discord or Signal so you know what went down.

Usage:
    python monitor.py
    python monitor.py --config /etc/sre-monitor/config.yaml
"""

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

import psutil
import requests
import yaml


# ──────────────────────────────────────────────
#  Logging setup
# ──────────────────────────────────────────────

# Configure the root logger once here so every part of the script
# shares the same format and destination.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Graceful shutdown
# ──────────────────────────────────────────────

# The main loop checks this flag each cycle. Signal handlers flip it to False
# so the daemon exits cleanly instead of dying mid-check.
_running = True


def _handle_shutdown(signum, frame):
    """Called when SIGINT (Ctrl+C) or SIGTERM is received."""
    global _running
    log.info("Shutdown signal received — finishing current cycle then exiting.")
    _running = False


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


# ──────────────────────────────────────────────
#  Alert cooldown
# ──────────────────────────────────────────────

# Tracks the last time each alert key was fired, keyed by a short string
# like "cpu", "disk", "service:nginx". This prevents the same alert from
# spamming every 60 seconds during a sustained issue.
_last_alerted: dict = {}


def _should_alert(key: str, config: dict) -> bool:
    """
    Return True if enough time has passed since the last alert for this key.

    The cooldown window is read from config['alert_cooldown_seconds'].
    Defaults to 300 seconds (5 minutes) if not set.
    """
    cooldown = config.get("alert_cooldown_seconds", 300)
    now = time.time()

    if now - _last_alerted.get(key, 0) >= cooldown:
        _last_alerted[key] = now
        return True

    return False


# ──────────────────────────────────────────────
#  Config
# ──────────────────────────────────────────────

# Keys that must be present in the YAML file for the daemon to start.
_REQUIRED_CONFIG_KEYS = ["thresholds", "notification"]


def load_config(path="config.yaml"):
    """
    Read the YAML config file and return it as a dict.
    Raises ValueError with a clear message if required keys are missing.
    """
    with open(path, "r") as f:
        config = yaml.safe_load(f)

    # Catch missing keys early so the error is obvious, not a cryptic KeyError
    # buried inside a check function.
    for key in _REQUIRED_CONFIG_KEYS:
        if key not in config:
            raise ValueError(f"Missing required config key: '{key}' — check {path}")

    return config


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="SRE Self-Healing Monitoring Daemon"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML config file (default: config.yaml)",
    )
    return parser.parse_args()


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
        log.error("Could not send Discord alert: %s", e)


def _send_signal(config, message):
    """
    Send a plain-text message via Signal using the CallMeBot API.

    Setup: text +34 644 52 74 88 on Signal with the message:
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
        log.error("Could not send Signal alert: %s", e)


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

    log.info("Running playbook: %s", playbook_path)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        log.error("Playbook failed:\n%s", result.stderr)
        return False

    log.info("Playbook succeeded: %s", playbook_path)
    return True


# ──────────────────────────────────────────────
#  Metric Checks
# ──────────────────────────────────────────────

def check_cpu(config):
    """
    Alert if CPU usage is over the configured threshold.
    Respects the alert cooldown so repeated highs don't flood notifications.
    """
    threshold = config["thresholds"]["cpu_percent"]
    # interval=1 tells psutil to measure over 1 second for accuracy
    usage = psutil.cpu_percent(interval=1)

    if usage >= threshold and _should_alert("cpu", config):
        msg = f"🚨 High CPU usage: {usage:.1f}% (limit: {threshold}%)"
        log.warning(msg)
        send_alert(config, msg, color=0xFFAA00)

    return usage


def check_memory(config):
    """
    Alert if RAM usage is over the configured threshold.
    Respects the alert cooldown so repeated highs don't flood notifications.
    """
    threshold = config["thresholds"]["memory_percent"]
    mem = psutil.virtual_memory()
    usage = mem.percent

    if usage >= threshold and _should_alert("memory", config):
        msg = f"🚨 High memory usage: {usage:.1f}% (limit: {threshold}%)"
        log.warning(msg)
        send_alert(config, msg, color=0xFFAA00)

    return usage


def check_disk(config):
    """
    Check disk usage on the configured path.
    If over threshold, trigger the log-rotation playbook automatically.
    Respects the alert cooldown so repeated highs don't flood notifications.
    """
    threshold = config["thresholds"]["disk_percent"]
    path = config.get("disk_path", "/")
    usage = psutil.disk_usage(path).percent

    if usage >= threshold and _should_alert("disk", config):
        msg = (
            f"⚠️ Disk usage critical on {path}: {usage:.1f}% "
            f"(limit: {threshold}%) — triggering log rotation..."
        )
        log.warning("Disk at %.1f%% on %s — running rotate_logs.yml", usage, path)
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
    Each service has its own cooldown key so alerts don't interfere with each other.
    """
    for service in config.get("services", []):
        # Build a set of running process names to check against
        running_names = {p.name() for p in psutil.process_iter(["name"])}
        is_running = service in running_names

        if not is_running and _should_alert(f"service:{service}", config):
            msg = f"🔴 Service '{service}' is DOWN. Attempting restart..."
            log.warning("'%s' not found in process list", service)
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
    Each log/pattern combo has its own cooldown key.
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
            # Use a composite key so different log files don't share a cooldown
            alert_key = f"log:{log_file}:{pattern}"

            if _should_alert(alert_key, config):
                # Show the most recent matching line as a sample
                sample = matches[-1].strip()
                msg = f"🔍 Pattern '{pattern}' matched in {log_file}:\n{sample}"
                log.warning("Pattern '%s' found in %s", pattern, log_file)
                send_alert(config, msg, color=0xFFAA00)


def tail_file(filepath, lines=50):
    """Return the last N lines of a file as a list of strings."""
    try:
        with open(filepath, "r", errors="replace") as f:
            return f.readlines()[-lines:]
    except Exception as e:
        log.error("Could not read %s: %s", filepath, e)
        return []


# ──────────────────────────────────────────────
#  Main Loop
# ──────────────────────────────────────────────

def main():
    args = parse_args()

    log.info("Starting SRE Monitoring Daemon...")

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as e:
        log.error("Failed to load config: %s", e)
        sys.exit(1)

    interval = config.get("check_interval_seconds", 60)
    provider = config.get("notification", "discord")
    cooldown = config.get("alert_cooldown_seconds", 300)

    log.info("Config loaded from: %s", args.config)
    log.info(
        "Notification provider: %s | Check interval: %ds | Alert cooldown: %ds",
        provider, interval, cooldown
    )

    send_alert(config, "🔵 SRE Monitor is online and watching.", color=0x4488FF)

    while _running:
        log.info("Running checks...")

        cpu  = check_cpu(config)
        mem  = check_memory(config)
        disk = check_disk(config)

        log.info("CPU: %.1f%%  |  RAM: %.1f%%  |  Disk: %.1f%%", cpu, mem, disk)

        check_services(config)
        check_logs(config)

        # Sleep in 1-second increments so a SIGTERM is handled quickly
        # instead of blocking for the full interval.
        for _ in range(interval):
            if not _running:
                break
            time.sleep(1)

    # Clean exit — let the team know the daemon stopped intentionally
    log.info("SRE Monitor stopped.")
    send_alert(config, "🔴 SRE Monitor has gone offline.", color=0xFF4444)


if __name__ == "__main__":
    main()
