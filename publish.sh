#!/usr/bin/env bash
# Creates the GitHub repo "Coin" and pushes this folder to it (Mac/Linux/Git Bash).
# Prereqs: git, and GitHub CLI (https://cli.github.com) logged in with:  gh auth login
set -euo pipefail
VIS="${1:-public}"   # public | private
if [ ! -d .git ]; then git init -b main; git add -A; git commit -m "Initial commit: Coin Board"; fi
gh repo create Coin --"$VIS" --source . --remote origin --push
