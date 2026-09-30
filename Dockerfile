# Only needed if you want the SCRAPER to work directly on the server (Google Maps / Yelp /
# Instagram scraping). If you're fine scraping on your own computer and importing the CSV
# (the tested, recommended path -- see README), you don't need this file at all: deploy with
# render.yaml / requirements.txt as normal and skip Docker entirely.
#
# Why this is needed: Render's regular ("native") Python service can install Python packages
# in its build step, but can't install operating-system packages -- and a real browser needs
# OS-level libraries (Render's own support confirms this: "native environments don't currently
# support installing OS-level packages, for that you would need to use Docker"). The official
# Playwright image below already has Chromium AND those OS libraries baked in, which is exactly
# what sidesteps that limitation.
#
# IMPORTANT -- keep the tag below and the "playwright==" line further down EQUAL to each other,
# and equal to whatever playwright version ends up installed from requirements.txt. A mismatch
# between the Python package version and the browser baked into this image is the #1 cause of
# "Executable doesn't exist" / browser-launch errors with Playwright+Docker. Check the current
# matching tag at https://playwright.dev/python/docs/docker before your first build.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-cloud.txt \
    && pip install --no-cache-dir "playwright==1.48.0"

COPY . .

ENV PORT=5000 \
    HOST=0.0.0.0 \
    SCRAPER_HEADLESS=true

EXPOSE 5000
CMD ["sh", "-c", "gunicorn app:app --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT}"]
