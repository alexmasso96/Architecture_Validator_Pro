$ErrorActionPreference = "Stop"

# Temporarily add Node.js to PATH for this script session
$Env:Path = "$env:LOCALAPPDATA\NodeJS\node-v20.18.0-win-x64;" + $Env:Path

Write-Host "Setting up Python virtual environment..."
python -m venv venv

Write-Host "Activating venv and installing Python dependencies..."
# Use call operator to execute pip within the virtual environment directly
& .\venv\Scripts\pip.exe install -r requirements.txt

Write-Host "Installing Node.js dependencies..."
Push-Location src\frontend
npm ci
Pop-Location

Write-Host "Building React frontend..."
Push-Location src\frontend
npm run build
Pop-Location

Write-Host "Development environment setup complete!"
