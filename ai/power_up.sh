#!/bin/bash
# Start a new tmux session named 'odin'
tmux new-session -d -s odin

# Window 0: Ollama Engine
tmux rename-window -t odin:0 'Ollama'
tmux send-keys -t odin:0 'ollama serve' C-m

# Window 1: Muninn Daemon & Logs
tmux new-window -t odin:1 -n 'Daemon'
tmux send-keys -t odin:1 'python odin_daemon.py' C-m

# Window 2: Active User Interface
tmux new-window -t odin:2 -n 'Console'
tmux send-keys -t odin:2 'python odin_chat.py' C-m

# Attach to the workspace console
tmux select-window -t odin:2
tmux attach-session -t odin
