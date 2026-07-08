FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── FIX (июль 2026) ────────────────────────────────────────────────────────────
# Instagram поменял внутренний API в конце июня 2026, и стабильный релиз yt-dlp
# перестал качать посты ("Instagram sent an empty media response", issue #17074).
# Фикс уже влит в master (PR #17075), но в стабильный релиз ещё не попал.
# Ставим nightly-сборку (собирается ежедневно из master) поверх requirements —
# --upgrade --pre перекрывает любую версию, закреплённую в requirements.txt.
RUN pip install --no-cache-dir --upgrade --pre "yt-dlp[default]"
# Альтернатива — тянуть прямо с master (раскомментируй, если nightly не спасёт):
# RUN pip install --no-cache-dir --force-reinstall \
#     "yt-dlp[default] @ git+https://github.com/yt-dlp/yt-dlp.git"

COPY instagram_bot.py .

CMD ["python", "-u", "instagram_bot.py"]