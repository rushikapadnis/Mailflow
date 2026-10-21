# MailFlow

Copy `.env.example` to `.env`, add your Zoho SMTP credentials, then run `python app.py`.

Campaigns are persisted in SQLite. Sending uses a bounded worker pool, while the browser polls campaign status. Scheduled campaigns are stored in SQLite and checked every five seconds by the local scheduler process.

Optional email-signature settings in `.env`:

```
SMTP_USE_TLS=true
SMTP_TIMEOUT=30
MAX_EMAIL_RETRIES=3
RETRY_BASE_DELAY=1
SMTP_MAX_MESSAGE_SIZE=52428800
# Optional override; the bundled static/company-logo.png is used by default.
COMPANY_LOGO_PATH=C:/absolute/path/to/logo.png

```

The supplied Celeritas logo is bundled as `static/company-logo.png` and is used by default. `COMPANY_LOGO_PATH` is only needed to override it. MailFlow embeds the image as a CID inline image rather than a downloadable attachment. The configured SMTP message-size limit must include MIME/base64 overhead and must not exceed your Zoho plan's actual allowance.

py -3.11 -m venv .venv311
.\.venv311\Scripts\Activate.ps1

python -m pip install -r requirements.txt

python -m unittest discover -s tests -v

python app.py
