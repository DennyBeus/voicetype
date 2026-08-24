#!/usr/bin/env bash
# Установка voicetype. Идемпотентна: повторный запуск ничего не затирает.
set -euo pipefail

PROJECT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$PROJECT"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
note() { printf '  \033[33m•\033[0m %s\n' "$*"; }

command -v uv >/dev/null || {
    echo "нужен uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
}

# Права на исполняемые файлы — git хранит бит выполнения, но при копировании
# папки или распаковке архива он теряется. Восстанавливаем всегда.
chmod +x bin/voicetype scripts/*.sh hooks/pre-commit uninstall.sh install.sh 2>/dev/null || true

# ── окружение ───────────────────────────────────────────────────────────
say "Python-окружение"

# Интерпретатор выбираем по качеству его Tk, а не по удобству установки.
# Сборка Python от uv несёт tkinter в себе, но собрана без Xft: такой Tk не
# видит fontconfig и рисует окно древним `nimbus sans l` без сглаживания —
# читать надиктованный текст в нём тяжело. Системный python3-tk линкуется с
# libXft и видит все шрифты системы, поэтому при наличии он предпочтительнее.
probe_tk() {   # $1 = python; печатает none | poor | good
    "$1" - <<'PYEOF' 2>/dev/null || echo none
import sys
try:
    import tkinter, tkinter.font as tkfont
    root = tkinter.Tk(); root.withdraw()
    fams = {f.lower() for f in tkfont.families()}
    root.destroy()
except Exception:
    print("none"); sys.exit(0)
good = {"ubuntu sans", "cantarell", "noto sans", "dejavu sans", "liberation sans"}
print("good" if good & fams else "poor")
PYEOF
}

uv python install 3.12 >/dev/null 2>&1
managed_py="$(uv python find 3.12 --managed-python 2>/dev/null || true)"
system_py="$(command -v python3 || true)"

chosen=""; chosen_kind=""
for cand in "$system_py" "$managed_py"; do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    verdict="$(probe_tk "$cand")"
    case "$verdict" in
        good) chosen="$cand"; chosen_kind="good"; break ;;
        poor) [ -z "$chosen" ] && { chosen="$cand"; chosen_kind="poor"; } ;;
    esac
done

if [ -z "$chosen" ]; then
    cat >&2 <<'EOF'
  ✗ ни один Python не даёт рабочего tkinter — окно правки не откроется.
    Поставь системный:  sudo apt install python3-tk
    и запусти install.sh заново.
EOF
    exit 1
fi

# Пересоздаём venv, если он собран не тем интерпретатором, что выбран сейчас.
venv_flag="--allow-existing"
if [ -x .venv/bin/python ]; then
    current="$(.venv/bin/python -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
    wanted="$("$chosen" -c 'import sys; print(sys.base_prefix)' 2>/dev/null || true)"
    if [ "$current" != "$wanted" ]; then
        note "venv собран другим Python — пересоздаю"
        venv_flag="--clear"
    fi
fi

uv venv --python "$chosen" $venv_flag >/dev/null
uv pip install -q -e . >/dev/null
ok "venv готов ($(./.venv/bin/python -V), $chosen)"

if [ "$chosen_kind" = "good" ]; then
    ok "tkinter видит системные шрифты"
else
    note "tkinter есть, но без поддержки fontconfig — шрифт в окне будет плохой"
    note "лечится так:  sudo apt install python3-tk && bash install.sh"
fi

# ── системные утилиты ───────────────────────────────────────────────────
say "Системные зависимости (ничего не устанавливаем, только проверяем)"
for tool in pw-record pw-play ffmpeg; do
    command -v "$tool" >/dev/null && ok "$tool" || note "$tool не найден — понадобится"
done

# ── личные файлы ────────────────────────────────────────────────────────
say "Настройки"
for pair in ".env.example:.env" "config.example.toml:config.toml" "keywords.example.txt:keywords.txt"; do
    src="${pair%%:*}"; dst="${pair##*:}"
    if [ -e "$dst" ]; then
        ok "$dst уже есть — не трогаю"
    else
        cp "$src" "$dst"
        ok "создан $dst из $src"
    fi
done
chmod 600 .env
ok ".env доступен только тебе (chmod 600)"

# ── защита от утечки ключей ─────────────────────────────────────────────
say "Защита секретов"
if [ -d .git ]; then
    hooks_dir="$(git rev-parse --git-path hooks)"
    ln -sf "$PROJECT/hooks/pre-commit" "$hooks_dir/pre-commit"
    ok "pre-commit хук установлен — .env и ключи sk-... не пройдут в коммит"
    git check-ignore -q .env && ok ".env в .gitignore" || note ".env НЕ игнорируется — проверь .gitignore"
else
    note "не git-репозиторий, хук не ставлю"
fi

# ── systemd ─────────────────────────────────────────────────────────────
say "systemd"
unit_dir="$HOME/.config/systemd/user"
mkdir -p "$unit_dir"
ln -sf "$PROJECT/systemd/voicetype.service" "$unit_dir/voicetype.service"
systemctl --user daemon-reload
ok "unit установлен"
# НАМЕРЕННО не вызываем `systemctl --user enable`.
# Именно enable включает автозапуск при загрузке машины, а требование —
# чтобы программа поднималась только вручную. Не добавляй эту команду сюда.
ok "автозапуск НЕ включён — демон поднимается только командой voicetype start"

# ── PATH ────────────────────────────────────────────────────────────────
say "Готово"
if ! command -v voicetype >/dev/null 2>&1; then
    echo "  Добавь в ~/.bashrc, чтобы команда была под рукой:"
    echo "      export PATH=\"\$PATH:$PROJECT/bin\""
    echo
fi
cat <<EOF
  Дальше:
    1. вписать ключ OpenAI в $PROJECT/.env
       (DEEPSEEK_API_KEY можно оставить пустым — вычитка просто не включится)
    2. проверить микрофон:   ./scripts/check_mic.sh --bt
    3. запустить:            ./bin/voicetype start
EOF
