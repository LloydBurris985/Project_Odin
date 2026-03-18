#!/bin/bash
if [ -f airport.pid ]; then
    kill $(cat airport.pid)
    rm airport.pid
    echo "✅ Airport stopped"
else
    echo "No PID file — airport not running"
fi
