# Setup for animated-subtitle-renderer-v2 (Windows PowerShell).
# Installs all Python deps incl. python-dotenv (for .env key loading).
# Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
$SkillRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
python -m pip install --upgrade pip
python -m pip install -r (Join-Path $SkillRoot "requirements.txt")
Write-Output "Setup OK. Copy ..\\.env.example to ..\\.env and fill GROQ_API_KEY."
