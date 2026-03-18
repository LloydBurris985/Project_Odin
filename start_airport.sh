#!/bin/bash
nohup python3 airport.py > airport.log 2>&1 &
echo $! > airport.pid
echo "✅ Airport started in background (PID saved). Check airport.log"
