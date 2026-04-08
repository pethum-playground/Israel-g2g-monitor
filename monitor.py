import requests
from bs4 import BeautifulSoup
import json
import time
import subprocess
import logging
import sys
import os
import wave
import struct
import math
from pathlib import Path
from datetime import datetime

# ─── Configuration ───────────────────────────────────────────────
URL = "https://services.slbfe.lk/Israel/WebPortal"
SELECT_ID = "JobSector_ID"
CHECK_INTERVAL = 5  # seconds (5 minutes)
WATCH_KEYWORD = "hotel"
HORN_REPEAT = 3

SCRIPT_DIR = Path(__file__).parent
KNOWN_OPTIONS_FILE = SCRIPT_DIR / "known_options.json"
HORN_FILE = SCRIPT_DIR / "horn.wav"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ─── Logging ─────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("monitor")


# ─── Horn Sound Generator ───────────────────────────────────────
def generate_horn_sound():
    """Generate a loud traffic horn WAV file using stdlib only."""
    if HORN_FILE.exists():
        return

    log.info("Generating horn sound...")
    sample_rate = 44100
    duration = 1.5  # seconds
    num_samples = int(sample_rate * duration)

    # Layer multiple frequencies for a thick air-horn sound
    frequencies = [150, 200, 250, 300]
    amplitude = 30000

    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        # Envelope: quick attack, sustain, short decay
        if t < 0.05:
            envelope = t / 0.05
        elif t > duration - 0.1:
            envelope = (duration - t) / 0.1
        else:
            envelope = 1.0

        value = 0
        for freq in frequencies:
            value += math.sin(2 * math.pi * freq * t)
        value = value / len(frequencies) * amplitude * envelope
        samples.append(int(value))

    with wave.open(str(HORN_FILE), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(struct.pack(f"<{num_samples}h", *samples))

    log.info(f"Horn sound saved to {HORN_FILE}")


def play_horn():
    """Play the horn sound on loop until Ctrl+C is pressed."""
    log.warning("\033[1;31m  HORN BLASTING — Press Ctrl+C to stop!\033[0m")
    try:
        while True:
            subprocess.run(["afplay", str(HORN_FILE)])
    except KeyboardInterrupt:
        log.info("Horn stopped.")


# ─── Options Persistence ────────────────────────────────────────
def load_known_options():
    if KNOWN_OPTIONS_FILE.exists():
        with open(KNOWN_OPTIONS_FILE) as f:
            return set(json.load(f))
    return set()


def save_known_options(options):
    with open(KNOWN_OPTIONS_FILE, "w") as f:
        json.dump(sorted(options), f, indent=2)


# ─── Page Scraping ───────────────────────────────────────────────
def fetch_options():
    """Fetch the page and extract Job Sector dropdown options."""
    response = requests.get(URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    select = soup.find("select", {"id": SELECT_ID})

    if not select:
        raise ValueError(f"Could not find select element with id '{SELECT_ID}'")

    options = set()
    for option in select.find_all("option"):
        text = option.get_text(strip=True)
        # Skip the placeholder
        if text and not text.startswith("-"):
            options.add(text)

    return options


# ─── Alerting ────────────────────────────────────────────────────
def send_alert(new_options):
    """Blast the horn and log the new options."""
    names = ", ".join(new_options)

    # Bold ANSI terminal output
    log.warning(f"\033[1;31m{'='*50}\033[0m")
    log.warning(f"\033[1;31m  NEW JOB SECTOR(S) DETECTED: {names}\033[0m")
    log.warning(f"\033[1;31m{'='*50}\033[0m")

    # Check for keyword match
    for opt in new_options:
        if WATCH_KEYWORD.lower() in opt.lower():
            log.warning(f"\033[1;33m  MATCH: '{WATCH_KEYWORD}' sector is now available!\033[0m")

    # BLAST THE HORN
    play_horn()


# ─── Main Loop ───────────────────────────────────────────────────
def main():
    log.info(f"Monitoring: {URL}")
    log.info(f"Check interval: {CHECK_INTERVAL}s | Watching for: '{WATCH_KEYWORD}'")

    generate_horn_sound()

    known = load_known_options()

    if not known:
        log.info("First run - seeding known options...")
        try:
            known = fetch_options()
            save_known_options(known)
            log.info(f"Seeded {len(known)} options: {known}")
        except Exception as e:
            log.error(f"Failed to seed options: {e}")
            sys.exit(1)
    else:
        log.info(f"Loaded {len(known)} known options: {known}")

    try:
        while True:
            try:
                current = fetch_options()
                new = current - known

                if new:
                    send_alert(new)
                    known = current
                    save_known_options(known)
                else:
                    log.info(f"No changes. Current options: {current}")

            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error(f"Check failed: {e}")

            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
