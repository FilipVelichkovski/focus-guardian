# Focus Guardian

![AI-Generated](https://img.shields.io/badge/AI--Generated-LLM%20Code-blue)

> [!NOTE]
> **AI-Generated Code:** This repository and its codebase were generated with an LLM.

Lightweight companion app that connects to **Google Calendar** & **Google Tasks** and checks your last 5 minutes of tracked activity from [activity-tracker](https://github.com/FilipVelichkovski/activity-tracker) via its local HTTP API (`http://127.0.0.1:7878`, with direct SQLite fallback if `~/.local/share/activity-tracker/activity.db` is unencrypted).

## Requirements

- **[activity-tracker](https://github.com/FilipVelichkovski/activity-tracker)** (required) — must be installed and running to serve the local HTTP API (`http://127.0.0.1:7878`), since `activity.db` is SQLCipher-encrypted.

## Behavior
- **Every 5 minutes** (`300s`): checks if you are working on an immediate not-done Google Task **or** learning **Analysis** / **Discrete Mathematics**.
- **Distraction Notification**: sends a `notify-send` alert if you are active and neither working on a pending task nor studying Analysis / Discrete Mathematics.
- **Suppresses Notifications Automatically When:**
  - A Google Calendar event is currently ongoing (classes, seminars, tutorials, etc.)
  - Laptop is **not plugged into a charger** (saves battery)
  - Laptop is **offline**
  - During **quiet hours** (`21:00` – `08:00`)
  - You were away / locked / minimized (`< 45s` active time in the last 5 minutes)

## Setup

1. Create a Google Cloud Desktop OAuth2 client (`calendar.readonly` + `tasks.readonly`), download the JSON, and save it to:
   `~/.config/focus-guardian/credentials.json`

2. Authorize once:
   ```bash
   python3 /var/home/silver/repos/focus-guardian/cli.py auth
   ```

3. Check status or test once:
   ```bash
   python3 /var/home/silver/repos/focus-guardian/cli.py status
   python3 /var/home/silver/repos/focus-guardian/cli.py check-once --notify
   ```

4. Install & start as a `systemd --user` service:
   ```bash
   python3 /var/home/silver/repos/focus-guardian/cli.py install-service
   ```
