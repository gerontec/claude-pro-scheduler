#!/usr/bin/env python3
"""
mqtt-listener.py — MQTT → Batch-Job Bridge

Lauscht auf ki/delegate, schreibt Jobs in claude_pro_batch,
publiziert Ack + (via Poller) Ergebnisse zurück.

Topics:
  ki/delegate          ← Eingang:  {"prompt":"…","model":"xiaomi","targetdate":"2026-04-04"}
  ki/job/ack           → Ausgang:  {"id":42,"status":"queued","model":"xiaomi"}
  ki/job/result/<id>   → Ausgang:  {"id":42,"status":"done","result":"…","cost_usd":0.0005}

Start:  python3 mqtt-listener.py
Daemon: systemctl start ki-mqtt-listener
"""

import json
import logging
import os
import signal
import sys
from datetime import date

import paho.mqtt.client as mqtt
import pymysql

BROKER      = "192.168.178.218"
PORT        = 1883
TOPIC_IN    = "ki/delegate"
TOPIC_ACK   = "ki/job/ack"
TOPIC_RES   = "ki/job/result"
CLIENT_ID   = "ki-batch-listener"
KEEPALIVE   = 60

DB_CFG = dict(host="localhost", user="gh", password="a12345", database="wagodb",
              charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)

VALID_MODELS = {"haiku", "sonnet", "opus", "xiaomi", "mimo-pro"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mqtt-listener] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def db_insert(prompt: str, model: str, targetdate: str, resume: int = 0) -> int:
    db = pymysql.connect(**DB_CFG)
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO claude_pro_batch (targetdate, model, resume_session, prompt) "
                "VALUES (%s, %s, %s, %s)",
                (targetdate, model, resume, prompt),
            )
            db.commit()
            return db.insert_id()
    finally:
        db.close()


def on_connect(client, userdata, flags, reason_code, props=None):
    if reason_code == 0:
        log.info(f"Verbunden mit {BROKER}:{PORT} — lausche auf {TOPIC_IN}")
        client.subscribe(TOPIC_IN, qos=1)
    else:
        log.error(f"Verbindung fehlgeschlagen: {reason_code}")


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except Exception as e:
        log.warning(f"Ungültiges JSON: {e} — {msg.payload[:100]}")
        return

    prompt  = str(payload.get("prompt", "")).strip()
    model   = payload.get("model", "xiaomi")
    today   = date.today().isoformat()
    tdate   = payload.get("targetdate", today)
    resume  = int(bool(payload.get("resume_session", False)))

    if not prompt:
        log.warning("Kein Prompt — ignoriert")
        return
    if model not in VALID_MODELS:
        log.warning(f"Unbekanntes Modell '{model}' → xiaomi")
        model = "xiaomi"

    try:
        job_id = db_insert(prompt, model, tdate, resume)
    except Exception as e:
        log.error(f"DB-Fehler: {e}")
        return

    ack = json.dumps({"id": job_id, "status": "queued", "model": model,
                      "targetdate": tdate})
    client.publish(TOPIC_ACK, ack, qos=1)
    log.info(f"Job #{job_id} queued [{model}] — '{prompt[:60]}'")


def main():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=CLIENT_ID,
        clean_session=True,
    )
    client.on_connect = on_connect
    client.on_message = on_message

    client.will_set("ki/listener/status", '{"status":"offline"}', qos=1, retain=True)

    client.connect(BROKER, PORT, KEEPALIVE)
    client.publish("ki/listener/status", '{"status":"online","topic":"ki/delegate"}',
                   qos=1, retain=True)

    def _shutdown(sig, frame):
        log.info("Beende…")
        client.publish("ki/listener/status", '{"status":"offline"}', qos=1, retain=True)
        client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    log.info(f"Listener gestartet — {BROKER}:{PORT} → DB wagodb")
    client.loop_forever()


if __name__ == "__main__":
    main()
