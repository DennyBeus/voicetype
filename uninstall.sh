#!/usr/bin/env bash
# Убрать voicetype из системы. Личные файлы (.env, config.toml,
# keywords.txt) остаются — их удаляй сам, если нужно.
set -uo pipefail

PROJECT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

systemctl --user stop voicetype.service 2>/dev/null
systemctl --user disable voicetype.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/voicetype.service"
systemctl --user daemon-reload
echo "✓ systemd unit удалён"

rm -rf "$PROJECT/.venv" "$PROJECT/.state"
echo "✓ окружение и состояние удалены"

echo
echo "Остались нетронутыми: .env, config.toml, keywords.txt"
echo "Удалить проект целиком:  rm -rf $PROJECT"
