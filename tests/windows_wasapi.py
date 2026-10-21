"""Запись через настоящий WASAPI на виртуальной звуковой карте (VB-CABLE) — в GitHub Actions.

1. Исходная soundcard: воспроизводится ли «invalid argument».
2. С исправлением формата (audio_win._fix_format): открываются ли микрофон и «петля» — в формате
   устройства каждым способом его описать (audio_win.STYLES) и в других форматах.
3. Настоящая запись: в виртуальный динамик играет тон, микрофон (CABLE Output) и звук
   собеседников («петля» с CABLE Input) пишутся одновременно, как во время созвона.
4. Кнопка «Проверить запись» из настроек (audio.check_recording) на тех же устройствах.
"""

import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

failures: list[str] = []


def say(*args):
    print(*args, flush=True)


import soundcard  # noqa: E402 — сначала без исправления
from soundcard import mediafoundation as mf  # noqa: E402

mics, speakers = soundcard.all_microphones(), soundcard.all_speakers()
say("микрофоны:", [m.name for m in mics])
say("выходы:", [s.name for s in speakers])
if not mics or not speakers:
    say("ИТОГ: звуковых устройств нет — проверка невозможна")
    sys.exit(1)


def mix_info(src) -> str:
    client = src._audio_client()
    try:
        pp = mf._ffi.new("WAVEFORMATEXTENSIBLE**")
        mf._com.check_error(client[0][0].lpVtbl.GetMixFormat(client[0], pp))
        raw = bytes(mf._ffi.buffer(pp[0], 40))
        mf._ole32.CoTaskMemFree(pp[0])
    finally:
        mf._com.release(client)
    _tag, ch, rate, _avg, _align, bits, _cb = struct.unpack_from("<HHIIHHH", raw)
    return f"{ch} кан., {rate} Гц, {bits} бит, маска каналов {struct.unpack_from('<I', raw, 20)[0]:#x}"


def attempt(src, ch, rate, **style) -> str:
    try:
        with src.recorder(samplerate=rate, channels=ch, blocksize=rate // 20, **style) as r:
            d = r.record(numframes=rate // 4)
            note = getattr(r, "format_note", "")
        return f"ok, {d.shape[0]} кадров × {d.shape[1] if d.ndim > 1 else 1}" + (f" ({note})" if note else "")
    except Exception as e:  # noqa: BLE001
        return f"ошибка: {e or e.__class__.__name__} ({getattr(e, 'format_note', '')})"


sources = [("микрофон", soundcard.default_microphone()),
           ("петля", soundcard.get_microphone(soundcard.default_speaker().id, include_loopback=True))]
say("== исходная soundcard", getattr(soundcard, "__version__", ""))
for name, src in sources:
    say(f"  {name} «{src.name}»: формат смешивания {mix_info(src)}; запись: {attempt(src, src.channels, 48000)}")

from callsum import audio, audio_win  # noqa: E402

audio_win._sc()  # применяет исправление формата
say("== с исправлением формата")
for name, src in sources:
    client = src._audio_client()
    try:
        rate, ch, _mask, _float = audio_win._mix_info(audio_win._mix_bytes(mf, client))
    finally:
        mf._com.release(client)
    for style in audio_win.STYLES:  # формат устройства — всеми способами его описать
        result = attempt(src, ch, rate, style=style)
        say(f"  {name}, {style}: {result}")
        if not result.startswith("ok"):
            failures.append(f"{name}, {style}: {result}")
    for ch, rate in ((1, 48000), (2, 44100), (src.channels, 16000)):
        say(f"     {ch} кан., {rate} Гц: {attempt(src, ch, rate)}")

say("== настоящая запись двух дорожек, в динамик играет тон 440 Гц")
folder = Path(tempfile.mkdtemp()) / "Проверка записи"
folder.mkdir()


def play():
    t = np.arange(48000 * 7) / 48000
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    try:
        with soundcard.default_speaker().player(samplerate=48000, channels=2) as p:
            p.play(np.stack([tone, tone], axis=1))
    except Exception as e:  # noqa: BLE001
        say("  проигрывание тона:", e)


player = threading.Thread(target=play, daemon=True)
player.start()
time.sleep(0.5)
tracks = [audio_win.TrackRecorder(soundcard.default_microphone().id, folder / "mic.ogg", "Микрофон", "mic"),
          audio_win.TrackRecorder(soundcard.default_speaker().id, folder / "system.ogg", "Собеседники", "system")]
for t in tracks:
    t.start()
time.sleep(4.0)
errors = [(t.label, t.error()) for t in tracks]
for t in tracks:
    t.stop()
player.join(timeout=10)
for t, (label, err) in zip(tracks, errors, strict=True):
    secs = audio.probe_duration(t.out_path) if t.out_path.exists() else 0.0
    say(f"  {label}: формат {t.format or '—'}, записано {secs:.1f} с, громкость {t.peak_seen:.2f}, ошибка: {err or 'нет'}")
    if err or secs < 3.0:
        failures.append(f"{label}: {err or f'записано только {secs:.1f} с'}")

say("== «Проверить запись» из настроек, в динамик снова играет тон")
player = threading.Thread(target=play, daemon=True)
player.start()
time.sleep(0.5)
report = audio.check_recording(soundcard.default_microphone().id, soundcard.default_speaker().id)
player.join(timeout=10)
say(" ", report)
if report != "Микрофон и собеседники: работает, звук есть.":
    failures.append(f"проверка записи: {report}")

say()
say("ИТОГ:", "всё в порядке" if not failures else "не прошли: " + "; ".join(failures))
sys.exit(1 if failures else 0)
