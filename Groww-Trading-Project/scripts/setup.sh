#!/usr/bin/env bash
# One-time setup on Ubuntu/Debian or macOS (Homebrew). Run from the project folder.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v psql >/dev/null 2>&1; then
  echo ">> Installing PostgreSQL"
  if command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y postgresql
  elif command -v brew >/dev/null 2>&1; then brew install postgresql@16 && brew services start postgresql@16
  else echo "Install PostgreSQL manually, then re-run"; exit 1; fi
fi
if command -v service >/dev/null 2>&1; then sudo service postgresql start || true; fi

echo ">> Creating database user/db (groww / groww_trading)"
PSQL="psql"; command -v sudo >/dev/null && id postgres >/dev/null 2>&1 && PSQL="sudo -u postgres psql"
$PSQL -tc "SELECT 1 FROM pg_roles WHERE rolname='groww'" | grep -q 1 || $PSQL -c "CREATE USER groww WITH PASSWORD 'groww' CREATEDB;"
$PSQL -tc "SELECT 1 FROM pg_database WHERE datname='groww_trading'" | grep -q 1 || $PSQL -c "CREATE DATABASE groww_trading OWNER groww;"
$PSQL -tc "SELECT 1 FROM pg_database WHERE datname='groww_trading_test'" | grep -q 1 || $PSQL -c "CREATE DATABASE groww_trading_test OWNER groww;"

echo ">> Python virtualenv"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

[ -f keys.txt ] || { cp keys.txt.example keys.txt; chmod 600 keys.txt; echo ">> Created keys.txt - put your Groww TOTP key + secret in it"; }
echo ">> Done. Start with:  .venv/bin/python run.py   and open http://127.0.0.1:5000"
