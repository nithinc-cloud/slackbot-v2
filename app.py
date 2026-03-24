import os
import sys
import re
import json
import logging
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------- CONFIGURATION ---------------- #
LOG_FILE = os.environ.get("LOG_FILE", "/appz/log/slackbot.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")]
)
logger = logging.getLogger(__name__)

# ---------------- ENVIRONMENT ---------------- #
REQUIRED_ENVS = ["APP_TOKEN", "BOT_TOKEN", "TARGET_CHANNEL_ID", "CHANNEL_IDS"]
env = {k: os.environ.get(k) for k in REQUIRED_ENVS}

app_token         = env["APP_TOKEN"]
bot_token         = env["BOT_TOKEN"]
target_channel_id = env["TARGET_CHANNEL_ID"]
channel_ids       = [c.strip() for c in env["CHANNEL_IDS"].split(",") if c.strip()]
patterns_path     = os.environ.get("PATTERNS_PATH", "/appz/scripts/webapps/patterns.json")

if not all([app_token, bot_token, target_channel_id, channel_ids]):
    logger.error("Missing required environment variables. Aborting.")
    sys.exit(1)

logger.info("Environment loaded successfully")

# ---------------- SLACK APP ---------------- #
app = App(token=bot_token)

# ---------------- CONSTANTS ---------------- #
# Only incident lifecycle states — Warn is not forwarded
VALID_STATES = {"Triggered", "Re-Triggered", "Recovered"}

TIME_WINDOWS = {
    "Triggered":    timedelta(minutes=60),
    "Re-Triggered": timedelta(minutes=60),
    "Recovered":    timedelta(minutes=5),
}

STATE_COLORS = {
    "Triggered":    "#E01E5A",
    "Re-Triggered": "#E01E5A",
    "Recovered":    "#2EB67D",
}

# Zabbix-only emojis — Datadog alerts get no emoji
ZABBIX_EMOJIS = {
    "Triggered": "🔴",
    "Recovered":  "✅",
}

ALERT_REGEX = re.compile(
    r'(?i)(Triggered|Recovered|Re-Triggered):\s*(?:\[[^\]]+\]\s*)*(.+)'
)

ZABBIX_REGEX = re.compile(
    r"(?is)"
    r"(Issue\s+(started|resolved).+?)\n"
    r"Name:\s*(.+?)\n"
    r"Server:\s*(.+?)(?:\n|$)"
)

# ---------------- PATTERNS ---------------- #
def load_filter_patterns(path):
    try:
        with open(path) as f:
            data = json.load(f)
            logger.info("patterns.json loaded")
            return data.get("include_patterns", []), data.get("exclude_patterns", [])
    except Exception as e:
        logger.error(f"Failed to load patterns.json: {e}")
        sys.exit(1)

include_patterns, exclude_patterns = load_filter_patterns(patterns_path)

# ---------------- HELPERS ---------------- #
def now_utc():
    return datetime.utcnow()

def get_permalink(channel_id, message_ts):
    try:
        r = app.client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return r["permalink"]
    except Exception as e:
        logger.error(f"Permalink error: {e}")
        return f"slack://channel?id={channel_id}&message={message_ts}"

def format_sources(alert_name):
    chans = recent_messages_cache.get(alert_name, {}).get("channels", set())
    return ", ".join(f"<#{c}>" for c in sorted(chans))

# ---------------- ALERT PARSING ---------------- #
def extract_alert(text):
    """Parse alert text. Returns (state, alert_name, show_link, emoji) or (None, None, True, "")."""

    # Standard alert format: Triggered/Re-Triggered/Recovered: [tag] alert name
    # No emoji for Datadog alerts
    match = ALERT_REGEX.search(text)
    if match:
        state      = match.group(1).title()
        alert_name = match.group(2).split("\n")[0].strip()
        logger.info("Standard alert parsed | state=%s alert=%s", state, alert_name)
        return state, alert_name, True, ""

    # Zabbix alert format: Issue started/resolved
    return _extract_zabbix_alert(text)

def _extract_zabbix_alert(text):
    match = ZABBIX_REGEX.search(text)
    if not match:
        return None, None, True, ""

    lifecycle  = match.group(2).lower()
    alert_name = match.group(3).strip()
    server     = match.group(4).strip()

    if lifecycle == "started":
        state     = "Triggered"
        show_link = False
    elif lifecycle == "resolved":
        state     = "Recovered"
        show_link = False
    else:
        return None, None, True, ""

    full_alert_name = f"{alert_name} ({server})"
    emoji = ZABBIX_EMOJIS.get(state, "")
    logger.info(
        "Zabbix alert parsed | state=%s alert=%s server=%s show_link=%s",
        state, alert_name, server, show_link,
    )
    return state, full_alert_name, show_link, emoji

# ---------------- CACHE / DEDUP ---------------- #
recent_messages_cache = {}

def should_forward_alert(alert_name, state, channel_id):
    """Returns True if the alert should be forwarded based on dedup and lifecycle rules."""
    now   = now_utc()
    entry = recent_messages_cache.setdefault(
        alert_name,
        {"states": {}, "channels": set(), "incident_active": False, "first_seen": now},
    )

    entry["channels"].add(channel_id)

    # Drop recoveries that have no matching active incident
    if state == "Recovered" and not entry["incident_active"]:
        logger.info("Alert suppressed | reason=recovery_without_active_incident alert=%s", alert_name)
        return False

    # Drop duplicates within the state's time window
    last_seen = entry["states"].get(state)
    window    = TIME_WINDOWS.get(state)
    if last_seen and window and (now - last_seen) <= window:
        logger.info("Alert suppressed | reason=time_window state=%s alert=%s", state, alert_name)
        return False

    # Passed all checks — update state tracking
    entry["states"][state] = now
    if state in ("Triggered", "Re-Triggered"):
        entry["incident_active"] = True

    return True

# ---------------- SEND MESSAGE ---------------- #
def send_to_target(original_message, channel_id, message_ts, state, alert_name, show_link, emoji=""):
    logger.info(
        "Forwarding alert | state=%s alert=%s source_channel=%s show_link=%s",
        state, alert_name, channel_id, show_link,
    )

    sources = format_sources(alert_name)
    prefix  = f"{emoji} " if emoji else ""

    if show_link:
        # Datadog / standard alerts — full message as a bold permalink
        permalink = get_permalink(channel_id, message_ts)
        text = f"{prefix}*<{permalink}|{original_message}>*\nSources: {sources}"
    else:
        lines      = original_message.split("\n")
        first_line = f"*{lines[0].strip()}*"
        rest       = "\n".join(lines[1:]).strip()
        body       = f"{first_line}\n{rest}" if rest else first_line
        text       = f"{prefix}{body}\nSources: {sources}"

    try:
        app.client.chat_postMessage(
            channel=target_channel_id,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
            attachments=[{"color": STATE_COLORS.get(state, "#CCCCCC")}],
            unfurl_links=False,
        )
        logger.info("Alert sent successfully | state=%s alert=%s", state, alert_name)
    except Exception as e:
        logger.error(f"Send failed: {e}")

# ---------------- CORE HANDLER ---------------- #
def handle_alert(original_message, channel_id, message_ts):
    logger.info("Message received | channel=%s ts=%s", channel_id, message_ts)

    state, alert_name, show_link, emoji = extract_alert(original_message)

    if not state or not alert_name:
        logger.info("Message ignored | reason=not_an_alert")
        return

    if state not in VALID_STATES:
        logger.info("Message ignored | reason=unsupported_state state=%s", state)
        return

    if channel_id not in channel_ids:
        logger.info("Alert ignored | reason=channel_not_allowed channel=%s", channel_id)
        return

    if any(re.search(p, original_message, re.IGNORECASE) for p in exclude_patterns):
        logger.info("Alert ignored | reason=exclude_pattern alert=%s", alert_name)
        return

    if not should_forward_alert(alert_name, state, channel_id):
        return

    send_to_target(original_message, channel_id, message_ts, state, alert_name, show_link, emoji)

    if state == "Recovered":
        recent_messages_cache.pop(alert_name, None)
        logger.info("Incident cleared | alert=%s", alert_name)

# ---------------- MESSAGE HANDLERS ---------------- #
@app.message(re.compile("|".join(include_patterns), re.IGNORECASE))
def handle_plain_messages(message, say):
    handle_alert(message.get("text", ""), message["channel"], message["ts"])

@app.event("message")
def handle_attachment_messages(event, say):
    if "attachments" not in event or event.get("subtype") == "message_deleted":
        return

    channel_id = event["channel"]
    if channel_id not in channel_ids:
        return

    for att in event.get("attachments", []):
        alert_text = att.get("fallback") or att.get("title") or att.get("text")
        if not alert_text:
            continue
        if any(re.search(p, alert_text, re.IGNORECASE) for p in exclude_patterns):
            continue
        if any(re.search(p, alert_text, re.IGNORECASE) for p in include_patterns):
            handle_alert(alert_text, channel_id, event["ts"])

# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    logger.info("Starting Slackbot with lifecycle-based alert forwarding")
    SocketModeHandler(app, app_token).start()
