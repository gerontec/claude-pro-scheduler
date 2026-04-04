#!/bin/bash
# Startet bis zu 9 batch-poller Instanzen mit kleinem Versatz.
# Der Versatz (0.3s) stellt sicher, dass jede Instanz den flock
# einzeln acquirieren kann, bevor die nächste versucht es.
for i in $(seq 1 9); do
    /usr/bin/python3 /home/gh/batch-poller.py &
    sleep 0.3
done
wait
