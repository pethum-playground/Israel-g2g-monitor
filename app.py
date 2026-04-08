import os
import json
import time
import wave
import math
import struct
import signal
import logging
import threading
import subprocess
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response, session, redirect, url_for

# ─── Config ──────────────────────────────────────────────────────
SLBFE_URL = "https://services.slbfe.lk/Israel/WebPortal"
SELECT_ID = "JobSector_ID"
SCRIPT_DIR = Path(__file__).parent
HORN_FILE = SCRIPT_DIR / "horn.wav"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "admin")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("app")

# ─── Global State ────────────────────────────────────────────────
state = {
    "monitoring": False,
    "alert_active": False,
    "countdown": 0,
    "submitting": False,
    "submitted": False,
    "form_data": {},
    "target_sector": "",
    "target_job_category": "",
    "check_interval": 5,
    "auto_submit_enabled": False,
    "auto_submit_delay": 60,
    "matched_sector_value": None,
}

events = []  # SSE event queue
monitor_thread = None
horn_process = None
cancel_flag = threading.Event()
stop_flag = threading.Event()


# ─── Horn Sound ──────────────────────────────────────────────────
def generate_horn_sound():
    if HORN_FILE.exists():
        return
    log.info("Generating horn sound...")
    sample_rate = 44100
    duration = 1.5
    num_samples = int(sample_rate * duration)
    frequencies = [150, 200, 250, 300]
    amplitude = 30000
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        if t < 0.05:
            envelope = t / 0.05
        elif t > duration - 0.1:
            envelope = (duration - t) / 0.1
        else:
            envelope = 1.0
        value = sum(math.sin(2 * math.pi * f * t) for f in frequencies)
        value = value / len(frequencies) * amplitude * envelope
        samples.append(int(value))
    with wave.open(str(HORN_FILE), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(struct.pack(f"<{num_samples}h", *samples))
    log.info("Horn sound generated.")


def start_horn():
    global horn_process

    def loop():
        global horn_process
        while state["alert_active"] and not cancel_flag.is_set():
            try:
                horn_process = subprocess.Popen(["afplay", str(HORN_FILE)])
                horn_process.wait()
            except Exception:
                break

    threading.Thread(target=loop, daemon=True).start()


def stop_horn():
    global horn_process
    state["alert_active"] = False
    if horn_process:
        try:
            horn_process.terminate()
            horn_process.kill()
        except Exception:
            pass
        horn_process = None


# ─── Page Scraping ───────────────────────────────────────────────
def fetch_sectors():
    response = requests.get(SLBFE_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    select = soup.find("select", {"id": SELECT_ID})
    if not select:
        raise ValueError(f"Could not find #{SELECT_ID}")
    sectors = {}
    for option in select.find_all("option"):
        val = option.get("value", "").strip()
        text = option.get_text(strip=True)
        if val and not text.startswith("-"):
            sectors[val] = text
    return sectors


def check_for_target(sectors, target):
    target_lower = target.lower()
    for val, text in sectors.items():
        if target_lower in text.lower():
            return val, text
    return None, None


# ─── Selenium Auto-Submit ────────────────────────────────────────
def auto_submit(form_data, sector_value, job_category):
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import Select, WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    push_event("status", "Opening browser for auto-submit...")
    state["submitting"] = True

    driver = None
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--start-maximized")
        driver = webdriver.Chrome(options=options)
        driver.get(SLBFE_URL)

        wait = WebDriverWait(driver, 15)
        wait.until(EC.presence_of_element_located((By.NAME, "nic")))

        time.sleep(1)

        # Fill text fields
        text_fields = {
            "nic": form_data.get("nic", ""),
            "pp_no": form_data.get("pp_no", ""),
            "dob": form_data.get("dob", ""),
            "lname": form_data.get("lname", ""),
            "fname": form_data.get("fname", ""),
            "farthers_name": form_data.get("farthers_name", ""),
            "pp_expire_date": form_data.get("pp_expire_date", ""),
            "mobile1": form_data.get("mobile1", ""),
            "mobile2": form_data.get("mobile2", ""),
            "add1": form_data.get("add1", ""),
            "add2": form_data.get("add2", ""),
            "town": form_data.get("town", ""),
        }

        for name, value in text_fields.items():
            if value:
                el = driver.find_element(By.NAME, name)
                el.clear()
                el.send_keys(value)

        # Select dropdowns
        def select_dropdown(name, value):
            if value:
                el = driver.find_element(By.NAME, name)
                Select(el).select_by_value(value)

        select_dropdown("gender", form_data.get("gender", ""))
        select_dropdown("civil_status", form_data.get("civil_status", ""))
        select_dropdown("district", form_data.get("district", ""))

        # Partner details if married
        if form_data.get("civil_status") == "M":
            time.sleep(0.5)
            partner_fields = {
                "Partner_nic": form_data.get("Partner_nic", ""),
                "Partner_ppno": form_data.get("Partner_ppno", ""),
                "Partner_dob": form_data.get("Partner_dob", ""),
                "Partner_lname": form_data.get("Partner_lname", ""),
                "Partner_fname": form_data.get("Partner_fname", ""),
            }
            for name, value in partner_fields.items():
                if value:
                    el = driver.find_element(By.NAME, name)
                    el.clear()
                    el.send_keys(value)

        # Select job sector
        select_dropdown("Sector", sector_value)
        push_event("status", "Waiting for job categories to load...")

        # Wait for job categories to load
        time.sleep(3)
        wait.until(lambda d: len(Select(d.find_element(By.NAME, "JobCate")).options) > 1)

        # Select job category
        if job_category:
            job_select = Select(driver.find_element(By.NAME, "JobCate"))
            for opt in job_select.options:
                if job_category.lower() in opt.text.lower():
                    job_select.select_by_value(opt.get_attribute("value"))
                    break

        push_event("status", "Submitting form...")
        time.sleep(1)

        # Click submit
        submit_btn = driver.find_element(By.ID, "Submit_btn")
        submit_btn.click()

        # Wait for submission to complete
        time.sleep(5)
        push_event("alert", "Form submitted successfully!")
        state["submitted"] = True
        log.info("Form submitted successfully via Selenium.")

        # Keep browser open for user to see result
        time.sleep(30)

    except Exception as e:
        push_event("error", f"Auto-submit failed: {str(e)}")
        log.error(f"Auto-submit failed: {e}")
    finally:
        state["submitting"] = False
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


# ─── SSE Events ──────────────────────────────────────────────────
def push_event(event_type, message):
    timestamp = datetime.now().strftime("%H:%M:%S")
    events.append({"type": event_type, "message": message, "time": timestamp})
    log.info(f"[{event_type}] {message}")


# ─── Monitoring Thread ───────────────────────────────────────────
def monitoring_loop():
    target = state["target_sector"]
    interval = state["check_interval"]
    delay = state["auto_submit_delay"]
    auto_submit_on = state["auto_submit_enabled"]

    push_event("status", f"Monitoring started. Watching for: '{target}' (case-insensitive)")
    push_event("status", f"Check interval: {interval}s | Auto-submit: {'ON' if auto_submit_on else 'OFF'}")

    while state["monitoring"] and not stop_flag.is_set():
        try:
            sectors = fetch_sectors()
            sector_names = list(sectors.values())
            push_event("check", f"Current sectors: {', '.join(sector_names)}")

            matched_value, matched_text = check_for_target(sectors, target)

            if matched_value:
                push_event("found", f"SECTOR FOUND: '{matched_text}' (value: {matched_value})")
                state["alert_active"] = True
                state["matched_sector_value"] = matched_value
                cancel_flag.clear()

                # Start horn
                start_horn()

                # Countdown
                state["countdown"] = delay
                while state["countdown"] > 0 and not cancel_flag.is_set():
                    push_event("countdown", str(state["countdown"]))
                    time.sleep(1)
                    state["countdown"] -= 1

                if cancel_flag.is_set():
                    stop_horn()
                    push_event("status", "Alert cancelled. Resuming monitoring...")
                    cancel_flag.clear()
                    continue

                # Countdown finished
                stop_horn()

                if auto_submit_on:
                    push_event("status", "Countdown finished. Auto-submitting...")
                    auto_submit(
                        state["form_data"],
                        matched_value,
                        state["target_job_category"],
                    )
                    state["monitoring"] = False
                    push_event("status", "Monitoring stopped after submission.")
                    return
                else:
                    push_event("alert", f"Sector '{matched_text}' is available! (Auto-submit disabled)")
                    push_event("status", "Horn stopped. Sector is available - go apply manually!")
                    state["monitoring"] = False
                    return
            else:
                push_event("check", "Target sector not found yet.")

        except Exception as e:
            push_event("error", f"Check failed: {str(e)}")

        # Wait for next check, but respond to stop quickly
        for _ in range(interval):
            if stop_flag.is_set():
                break
            time.sleep(1)

    push_event("status", "Monitoring stopped.")
    state["monitoring"] = False


# ─── Auth ────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ─── Flask Routes ────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == APP_USERNAME and password == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Invalid username or password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
@login_required
def start_monitoring():
    global monitor_thread

    if state["monitoring"]:
        return jsonify({"error": "Already monitoring"}), 400

    data = request.json
    state["form_data"] = data
    state["target_sector"] = data.get("target_sector", "hotel")
    state["target_job_category"] = data.get("target_job_category", "")
    state["check_interval"] = int(data.get("check_interval", 5))
    state["auto_submit_enabled"] = data.get("auto_submit_enabled", False)
    state["auto_submit_delay"] = int(data.get("auto_submit_delay", 60))
    state["monitoring"] = True
    state["submitted"] = False
    state["matched_sector_value"] = None

    stop_flag.clear()
    cancel_flag.clear()
    events.clear()

    monitor_thread = threading.Thread(target=monitoring_loop, daemon=True)
    monitor_thread.start()

    return jsonify({"status": "started"})


@app.route("/cancel", methods=["POST"])
@login_required
def cancel_alert():
    cancel_flag.set()
    stop_horn()
    state["countdown"] = 0
    return jsonify({"status": "cancelled"})


@app.route("/stop", methods=["POST"])
@login_required
def stop_monitoring():
    stop_flag.set()
    cancel_flag.set()
    stop_horn()
    state["monitoring"] = False
    state["countdown"] = 0
    push_event("status", "Monitoring stopped by user.")
    return jsonify({"status": "stopped"})


@app.route("/status")
@login_required
def status_stream():
    def generate():
        last_index = 0
        while True:
            while last_index < len(events):
                evt = events[last_index]
                data = json.dumps(evt)
                yield f"event: {evt['type']}\ndata: {data}\n\n"
                last_index += 1

            # Send heartbeat with current state
            heartbeat = json.dumps({
                "monitoring": state["monitoring"],
                "alert_active": state["alert_active"],
                "countdown": state["countdown"],
                "submitting": state["submitting"],
                "submitted": state["submitted"],
            })
            yield f"event: heartbeat\ndata: {heartbeat}\n\n"
            time.sleep(1)

    return Response(generate(), mimetype="text/event-stream")


# ─── Main ────────────────────────────────────────────────────────
if __name__ == "__main__":
    generate_horn_sound()
    log.info("Starting SLBFE Monitor Web App on http://localhost:5000")
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, threaded=True)
