#!/usr/bin/env bash
set -euo pipefail

MIDDLEBOX_IP="10.79.1.175"
SERVER_IP="10.79.1.208"
SSH_USER="bonsai"
KEY_PATH="$HOME/.ssh/id_ed25519_masterthesis"
EXPECTED_GO="/home/bonsai/MasterThesis/DC/go/bin/go"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [[ ! -f "$KEY_PATH" ]]; then
  echo "[SETUP] Genero chiave SSH dedicata: $KEY_PATH"
  ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "masterthesis-ssh"
else
  echo "[SETUP] Chiave gia presente: $KEY_PATH"
fi

chmod 600 "$KEY_PATH"
chmod 644 "$KEY_PATH.pub"

add_known_host() {
  local host="$1"
  echo "[SETUP] Aggiungo host key per $host"
  ssh-keyscan -H "$host" >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
}

install_key_on_host() {
  local host="$1"
  echo "[SETUP] Copio chiave pubblica su $SSH_USER@$host (inserisci password se richiesta)"
  ssh-copy-id -i "$KEY_PATH.pub" "$SSH_USER@$host"
}

verify_key_login() {
  local host="$1"
  echo "[SETUP] Verifico login key-based su $host"
  ssh -i "$KEY_PATH" -o BatchMode=yes -o ConnectTimeout=8 "$SSH_USER@$host" "echo ok" >/dev/null
}

ensure_go_path_middlebox() {
  echo "[SETUP] Verifico path Go richiesto sul middlebox: $EXPECTED_GO"
  ssh -i "$KEY_PATH" "$SSH_USER@$MIDDLEBOX_IP" 'bash -s' <<'EOS'
set -euo pipefail
EXPECTED_GO="/home/bonsai/MasterThesis/DC/go/bin/go"
if [[ -x "$EXPECTED_GO" ]]; then
  echo "[SETUP][MB] Go gia presente: $EXPECTED_GO"
  exit 0
fi

CANDIDATES=(
  "/home/bonsai/sdk/go1.23.11/bin/go"
  "/home/bonsai/go1.18/bin/go"
  "/home/bonsai/cfgo/bin/go"
  "/home/bonsai/gcc-rebuild/bin/go"
)

FOUND=""
for c in "${CANDIDATES[@]}"; do
  if [[ -x "$c" ]]; then
    FOUND="$c"
    break
  fi
done

if [[ -z "$FOUND" ]] && command -v go >/dev/null 2>&1; then
  FOUND="$(command -v go)"
fi

if [[ -z "$FOUND" ]]; then
  echo "[SETUP][MB][ERRORE] Nessun binario go disponibile per creare $EXPECTED_GO"
  exit 1
fi

mkdir -p "$(dirname "$EXPECTED_GO")"
ln -sf "$FOUND" "$EXPECTED_GO"
chmod +x "$EXPECTED_GO"
echo "[SETUP][MB] Creato link: $EXPECTED_GO -> $FOUND"
"$EXPECTED_GO" version
EOS
}

add_known_host "$MIDDLEBOX_IP"
add_known_host "$SERVER_IP"

install_key_on_host "$MIDDLEBOX_IP"
install_key_on_host "$SERVER_IP"

verify_key_login "$MIDDLEBOX_IP"
verify_key_login "$SERVER_IP"

ensure_go_path_middlebox

echo "[SETUP] Completato."
echo "[SETUP] Ora puoi lanciare: python3 TreMisure.py"
