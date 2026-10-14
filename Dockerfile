# Easytop bot for Cloud Run: Telegram polling, the phone login page, the nudge timer, all in one process.
# amd64 only: Google Chrome for Linux has no arm64 build, so build it with Cloud Build, not on the Mac.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    EASYTOP_LOG_FORMAT=json \
    TZ=Asia/Jerusalem

# Installed Google Chrome (the login uses it: a bundled Chromium made the captcha take many tries), Hebrew fonts
# and zone files. Chrome comes from Google's own apt repository.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata fonts-noto-core fonts-noto-color-emoji \
 && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-chrome-stable \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev

# What went into the image (the Cloud Build log shows it): no .env, no spike_data, no cookies, no Memory.md.
RUN ls -a /app

RUN useradd --create-home --uid 10001 easytop
USER easytop

# No --login-save-dir and no --login-trace in production. The port comes from $PORT (Cloud Run sets 8080).
CMD ["uv", "run", "--no-sync", "python", "-m", "easytop.bot", "--multi", "--store", "supabase", \
     "--serve-login", "--login-chrome", "--login-host", "0.0.0.0"]
