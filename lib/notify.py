#!/usr/bin/env python3
"""
notify.py — Send a Discord message to Pi's home channel.
Usage:
    python3 /root/supernova/lib/notify.py "Your message here"
Or import:
    from notify import send
    send("Temperature alert: 72°C")
"""

import os, sys, requests

def get_credentials():
    env_path = os.path.expanduser("~/.hermes/.env")
    creds = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k] = v
    token = creds.get("DISCORD_BOT_TOKEN")
    channel = creds.get("DISCORD_HOME_CHANNEL")
    if not token or not channel:
        raise RuntimeError("Missing DISCORD_BOT_TOKEN or DISCORD_HOME_CHANNEL in ~/.hermes/.env")
    return token, channel

def send(message: str) -> bool:
    token, channel_id = get_credentials()
    r = requests.post(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        json={"content": message},
        timeout=10
    )
    r.raise_for_status()
    return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 notify.py 'message'")
        sys.exit(1)
    send(" ".join(sys.argv[1:]))
    print("Sent.")