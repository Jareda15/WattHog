#!/usr/bin/env bash

set -e

echo "================================================="
echo " WattHog - Kompletní instalace (udev + zástupce)"
echo "================================================="

# Ochrana: musí běžet pod rootem
if [[ $EUID -ne 0 ]]; then
   echo "❌ Tento skript musí být spuštěn jako root (sudo)."
   echo "Příklad: sudo ./install.sh"
   exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
WRAPPER_PATH="/usr/local/bin/watthog"
DESKTOP_PATH="/usr/share/applications/watthog.desktop"
RULE_FILE="/etc/udev/rules.d/99-watthog-rapl.rules"

echo "1) Vytvářím udev pravidlo pro přístup k RAPL (intel-rapl) bez sudo..."

cat << 'EOF' > "$RULE_FILE"
# WattHog: Allow users to read Intel RAPL energy counters
ACTION=="add", SUBSYSTEM=="powercap", KERNEL=="intel-rapl:*", ATTR{energy_uj}="0444"
EOF

echo "Načítám nová udev pravidla..."
udevadm control --reload-rules && udevadm trigger 2>/dev/null || true

echo "⚠️  POZOR: Pokud WattHog po instalaci stále hlásí chybu oprávnění,"
echo "je nutné RESTARTOVAT PC, aby jádro správně aplikovalo práva na senzory."

echo ""
echo "2) Vytvářím spouštěč (wrapper) v $WRAPPER_PATH..."

cat << EOF > "$WRAPPER_PATH"
#!/usr/bin/env bash
# Wrapper pro spuštění WattHog z grafického i terminálového rozhraní

cd "$APP_DIR"
if [ -f "$APP_DIR/.venv/bin/activate" ]; then
    source "$APP_DIR/.venv/bin/activate"
fi
exec python3 app.py "\$@"
EOF

chmod +x "$WRAPPER_PATH"

echo ""
echo "3) Vytvářím zástupce aplikace v $DESKTOP_PATH..."

cat << EOF > "$DESKTOP_PATH"
[Desktop Entry]
Version=1.0
Type=Application
Name=WattHog
Comment=Sledování zátěže a spotřeby procesů
Exec=$WRAPPER_PATH
Icon=utilities-system-monitor
Terminal=true
Categories=System;Monitor;
EOF

chmod 644 "$DESKTOP_PATH"

echo "Aktualizuji databázi aplikací (pokud je dostupná)..."
if command -v update-desktop-database &> /dev/null; then
    update-desktop-database 2>/dev/null || true
fi

echo ""
echo "🎉 Instalace byla úspěšná!"
echo "- Můžeš spustit WattHog z menu aplikací (jako 'WattHog')."
echo "- Nebo napsáním 'watthog' do terminálu u libovolného uživatele."
