#!/usr/bin/env python3
"""
delegate.py — Sub-Agent Job einreichen ohne Passwort/Key-Kenntnis.

Verwendung:
  delegate "Analysiere das Netzwerk"
  delegate --model mimo-pro "Komplexe Aufgabe …"
  delegate --wait "Aufgabe …"          # wartet auf Ergebnis und gibt es aus
  delegate --list                       # letzte Jobs anzeigen
  delegate --status 42                  # Job-Status abfragen

Claude kann diesen Befehl direkt nutzen ohne API-Key zu kennen.
"""

import sys
import os
import json
import time
import argparse
import urllib.request
import urllib.error

API      = "http://192.168.5.23/api/batch/api.php"
API_KEY  = "2a61f527ded09cc2832cb49f8829f299"
HEADERS  = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def api_get(params: str) -> dict:
    url = f"{API}?{params}&apikey={API_KEY}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def api_post(body: dict) -> dict:
    data = json.dumps(body).encode()
    req  = urllib.request.Request(API, data=data, headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def submit(prompt: str, model: str = "xiaomi") -> dict:
    from datetime import date
    return api_post({"prompt": prompt, "model": model,
                     "targetdate": date.today().isoformat()})


def wait_for(job_id: int, poll: int = 4) -> dict:
    while True:
        j = api_get(f"id={job_id}&full=1")
        if j["status"] in ("done", "failed"):
            return j
        print(f"  … {j['status']} (Job #{job_id})", file=sys.stderr)
        time.sleep(poll)


def fmt_job(j: dict) -> str:
    cost  = f"${j['cost_usd']}" if j.get("cost_usd") else ""
    cache = f" cache={j['cache_tokens']}" if j.get("cache_tokens") else ""
    return (f"#{j['id']:4d}  {j['model']:10s}  {j['status']:7s}  "
            f"{cost:12s}{cache}  {j.get('created_at','')[:16]}")


def main():
    p = argparse.ArgumentParser(
        description="Sub-Agent delegieren — kein Passwort nötig",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("prompt", nargs="?", help="Aufgabe für den Sub-Agent")
    p.add_argument("--model", default="xiaomi",
                   choices=["xiaomi", "mimo-pro", "sonnet", "opus", "qwen"],
                   help="Modell (Standard: xiaomi)")
    p.add_argument("--wait", "-w", action="store_true",
                   help="Auf Ergebnis warten und ausgeben")
    p.add_argument("--list", "-l", action="store_true",
                   help="Letzte 10 Jobs anzeigen")
    p.add_argument("--status", "-s", type=int, metavar="ID",
                   help="Status eines Jobs abfragen")
    args = p.parse_args()

    # ── Liste ───────────────────────────────────────────────
    if args.list:
        jobs = api_get("list=1&limit=10")
        print(f"{'ID':>5}  {'Modell':10}  {'Status':7}  {'Kosten':12}  {'Erstellt'}")
        print("─" * 60)
        for j in jobs:
            print(fmt_job(j))
        return

    # ── Status ──────────────────────────────────────────────
    if args.status:
        j = api_get(f"id={args.status}&full=1")
        print(fmt_job(j))
        if j.get("result"):
            print("\n" + j["result"])
        if j.get("error_msg"):
            print("\nFEHLER:", j["error_msg"], file=sys.stderr)
        return

    # ── Submit ──────────────────────────────────────────────
    if not args.prompt:
        p.print_help()
        sys.exit(1)

    r = submit(args.prompt, args.model)
    job_id = r["id"]
    print(f"Job #{job_id} queued  [{args.model}]", flush=True)

    if args.wait:
        j = wait_for(job_id)
        print(f"\nStatus: {j['status']}  Kosten: ${j.get('cost_usd', 0)}")
        if j.get("result"):
            print("\n" + j["result"])
        if j.get("error_msg"):
            print("FEHLER:", j["error_msg"], file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Status:  delegate --status {job_id}")
        print(f"Warten:  delegate --wait \"{args.prompt[:40]}\"")


if __name__ == "__main__":
    main()
