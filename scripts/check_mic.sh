#!/usr/bin/env bash
# Шаг 0: убедиться, что микрофон вообще пишет звук, и получить строку для config.toml.
#
#   ./scripts/check_mic.sh            — проверить микрофон по умолчанию
#   ./scripts/check_mic.sh --bt       — сначала перевести Bluetooth-гарнитуру в режим микрофона
#   ./scripts/check_mic.sh --source X — проверить конкретный источник
set -uo pipefail

SECS=10
SOURCE=""
WANT_BT=0

while [ $# -gt 0 ]; do
    case "$1" in
        --bt)      WANT_BT=1; shift ;;
        --source)  SOURCE="${2:-}"; shift 2 ;;
        --secs)    SECS="${2:-10}"; shift 2 ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)         echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
err() { printf '\033[31m%s\033[0m\n' "$*" >&2; }

for cmd in pactl pw-record pw-play; do
    command -v "$cmd" >/dev/null || { err "нет команды $cmd — нужен PipeWire"; exit 1; }
done

# ── Bluetooth: перевести карту в профиль с микрофоном ────────────────────────
if [ "$WANT_BT" = 1 ]; then
    say "Ищу Bluetooth-аудиокарту…"
    card=$(pactl list short cards | awk '/bluez_card/ {print $2; exit}')
    if [ -z "$card" ]; then
        err "Bluetooth-карта не найдена."
        echo "Наушники не подключены к этой машине. Спарь их:"
        echo "  GNOME Settings → Bluetooth, или:  bluetoothctl scan on / pair / connect"
        exit 1
    fi
    echo "карта: $card"

    # У BT-гарнитур микрофон есть только в HFP/HSP, в A2DP его нет вовсе.
    profile=$(pactl list cards | awk -v c="$card" '
        $0 ~ "Name: "c {f=1} f && /Профили|Profiles/ {p=1; next}
        p && /headset-head-unit/ {gsub(/^[ \t]+/,""); split($0,a,":"); print a[1]; exit}')
    profile="${profile:-headset-head-unit-msbc}"

    echo "переключаю в профиль: $profile"
    if ! pactl set-card-profile "$card" "$profile" 2>/dev/null; then
        for p in headset-head-unit-msbc headset-head-unit-cvsd headset-head-unit; do
            pactl set-card-profile "$card" "$p" 2>/dev/null && { profile=$p; break; }
        done
    fi
    echo "текущий профиль: $(pactl list cards | awk -v c="$card" '$0 ~ "Name: "c {f=1} f && /Active Profile|Активный профиль/ {print $NF; exit}')"
    warn "Пока активен режим микрофона, звук В наушниках будет телефонного качества."
    warn "Так работает HFP у всех Bluetooth-гарнитур, включая AirPods. Это не баг."
    sleep 1
fi

# ── Выбор источника ─────────────────────────────────────────────────────────
say "Доступные источники записи:"
pactl list short sources | grep -v '\.monitor' | awk '{printf "  %s\n", $2}'
monitors_only=1
pactl list short sources | grep -qv '\.monitor' && monitors_only=0

if [ "$monitors_only" = 1 ]; then
    err "Настоящих микрофонов нет — только .monitor (это петля вывода, не микрофон)."
    echo "Подключи AirPods (--bt) или воткни USB-микрофон."
    exit 1
fi

if [ -z "$SOURCE" ]; then
    # Сначала то, что уже прописано в конфиге — иначе проверять будем одно
    # устройство, а демон писать с другого.
    cfg="$(dirname "$(readlink -f "$0")")/../config.toml"
    if [ -f "$cfg" ]; then
        SOURCE=$(awk -F'"' '/^[[:space:]]*source[[:space:]]*=/ {print $2; exit}' "$cfg")
        [ "$SOURCE" = "auto" ] && SOURCE=""
        [ -n "$SOURCE" ] && echo && echo "источник взят из config.toml"
    fi
fi

if [ -z "$SOURCE" ]; then
    SOURCE=$(pactl get-default-source 2>/dev/null)
    case "$SOURCE" in
        ""|*.monitor)
            SOURCE=$(pactl list short sources | grep -v '\.monitor' | awk 'NR==1{print $2}') ;;
    esac
fi
echo
echo "использую источник: $SOURCE"

# ── Запись ──────────────────────────────────────────────────────────────────
out=$(mktemp /tmp/voicetype-miccheck-XXXXXX.wav)
trap 'rm -f "$out"' EXIT

say "Говори ${SECS} секунд — что-нибудь рабочее, со сленгом."
echo "например: «заребейзь эту ветку на мейн и запушь форсом»"
echo
for i in 3 2 1; do printf '\r  старт через %s…' "$i"; sleep 1; done
printf '\r  \033[31m● ЗАПИСЬ\033[0m           \n'

pw-record --target "$SOURCE" --rate 16000 --channels 1 --format s16 "$out" &
rec_pid=$!
sleep "$SECS"
kill "$rec_pid" 2>/dev/null
wait "$rec_pid" 2>/dev/null

size=$(stat -c%s "$out" 2>/dev/null || echo 0)
echo "  готово, файл $size байт"

if [ "$size" -lt 10000 ]; then
    err "Данных почти нет — источник ничего не отдаёт."
    echo "Проверь, что выбран правильный источник и он не заглушён (pavucontrol / pactl set-source-mute)."
    exit 1
fi

# Грубая оценка громкости: если пик близок к нулю, микрофон молчит.
peak=$(ffmpeg -hide_banner -nostats -i "$out" -af volumedetect -f null - 2>&1 \
       | awk -F': ' '/max_volume/ {print $2}')
echo "  пиковая громкость: ${peak:-неизвестно}"
case "$peak" in
    "-0.0 dB"|"0.0 dB") warn "  сигнал упирается в потолок — возможны искажения, убавь усиление" ;;
    -[6-9][0-9].*)      warn "  очень тихо — модель может ничего не расслышать" ;;
esac

say "Воспроизвожу запись…"
pw-play "$out"

say "Если себя разобрал — микрофон годится. Впиши в config.toml:"
printf '\n  [audio]\n  source = "%s"\n\n' "$SOURCE"
