# Creates the GitHub repo "Coin" and pushes this folder to it (Windows PowerShell).
# Prereqs: git, and GitHub CLI (https://cli.github.com) logged in with:  gh auth login
param([ValidateSet("public","private")][string]$Visibility = "public")
$ErrorActionPreference = "Stop"
if (-not (Test-Path .git)) { git init -b main; git add -A; git commit -m "Initial commit: Coin Board" }
gh repo create Coin --$Visibility --source . --remote origin --push
