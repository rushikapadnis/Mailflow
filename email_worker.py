"""Background campaign workers, MIME construction, and SMTP lifecycle management."""
import html
import json
import logging
import mimetypes
import os
import queue
import smtplib
import socket
import threading
import time
import re
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr

from config import Config
import database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


class SMTPConfigurationError(ValueError):
    """A non-retryable error shared by every recipient in a campaign."""


def estimate_message_size(body, attachments, logo_size=0):
    """Conservative estimate including base64 expansion and MIME headers."""
    encoded_files = sum(((item["size"] + 2) // 3) * 4 + 512 for item in attachments)
    encoded_logo = ((logo_size + 2) // 3) * 4 + 512 if logo_size else 0
    return len(body.encode("utf-8")) * 2 + encoded_files + encoded_logo + 4096


class SMTPConnectionManager:
    """Persistent worker-local SMTP connection; never shared between threads."""
    def __init__(self):
        self.smtp = None
        self.stage = "CONNECT"

    def connect(self):
        self.discard()
        sender = Config.smtp_username()
        logger.info("[SMTP] server=%s port=%s username=%r", Config.SMTP_SERVER, Config.SMTP_PORT, sender)
        started = time.monotonic()
        smtp = None
        try:
            self.stage = "CONNECT"
            smtp = smtplib.SMTP(Config.SMTP_SERVER, Config.SMTP_PORT, timeout=Config.SMTP_CONNECT_TIMEOUT)
            connected = time.monotonic()
            self.stage = "EHLO"
            smtp.ehlo()
            if Config.SMTP_USE_TLS:
                self.stage = "STARTTLS"
                smtp.starttls()
                self.stage = "EHLO"
                smtp.ehlo()
            tls_ready = time.monotonic()
            self.stage = "LOGIN"
            smtp.login(sender, Config.SMTP_PASSWORD)
            if smtp.sock:
                smtp.sock.settimeout(Config.SMTP_TIMEOUT)
            self.smtp = smtp
            logger.info("[PERF] SMTP connect=%.2fs TLS/EHLO=%.2fs login=%.2fs", connected - started, tls_ready - connected, time.monotonic() - tls_ready)
            return smtp
        except Exception:
            if smtp:
                try:
                    smtp.close()
                except OSError:
                    pass
            raise

    def is_connection_alive(self):
        if not self.smtp:
            return False
        try:
            status, _ = self.smtp.noop()
            return 200 <= status < 300
        except (smtplib.SMTPException, OSError, socket.timeout):
            self.disconnect()
            return False

    def _set_socket_timeout(self, timeout):
        if self.smtp and self.smtp.sock:
            self.smtp.sock.settimeout(timeout)

    def send_message(self, message_bytes, recipient, has_attachments=False):
        # A dead socket is discarded by the retry controller. Do not issue NOOP
        # for every recipient: it adds a round trip and can itself block.
        if not self.smtp:
            self.connect()
        data_timeout = Config.SMTP_ATTACHMENT_TIMEOUT if has_attachments else Config.SMTP_TIMEOUT
        self._set_socket_timeout(data_timeout)
        mail_options = []
        # SIZE lets servers with a lower policy limit reject at MAIL FROM,
        # before an attachment is uploaded during DATA.
        if self.smtp.has_extn("size"):
            mail_options.append(f"SIZE={len(message_bytes)}")
        sender = Config.smtp_username()
        try:
            # Use the explicit SMTP sequence so diagnostics always name the
            # command that failed.  sendmail() obscures this as a generic call.
            self.stage = "MAIL_FROM"
            code, response = self.smtp.mail(sender, options=mail_options)
            if not 200 <= code < 300:
                raise smtplib.SMTPSenderRefused(code, response, sender)
            self.stage = "RCPT_TO"
            code, response = self.smtp.rcpt(recipient)
            if not 200 <= code < 300:
                raise smtplib.SMTPRecipientsRefused({recipient: (code, response)})
            self.stage = "DATA"
            logger.info("[SMTP] DATA start bytes=%s timeout=%ss", len(message_bytes), data_timeout)
            code, response = self.smtp.data(message_bytes)
            if not 200 <= code < 300:
                raise smtplib.SMTPDataError(code, response)
        finally:
            # A healthy persistent connection returns to the fast text timeout.
            self._set_socket_timeout(Config.SMTP_TIMEOUT)

    def discard(self):
        """Drop a failed connection without SMTP QUIT, which can block on a dead peer."""
        if self.smtp:
            try:
                self.smtp.close()
            except OSError:
                pass
            finally:
                self.smtp = None

    def disconnect(self):
        """Gracefully close a known-good connection when a worker is shut down."""
        if self.smtp:
            try:
                self.smtp.quit()
            except (smtplib.SMTPException, OSError, socket.timeout):
                self.discard()
            else:
                self.smtp = None


def _missing_personalization_value(value):
    if value is None:
        return True
    text = str(value).strip()
    return not text or text.casefold() in {"nan", "none", "null", "nat", "n/a", "na", "<na>", "missing", "undefined"}


def _normalized_field_name(value):
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def resolve_personalization(data, apply_defaults=True):
    """Resolve canonical person/company values deterministically from contact columns."""
    data = data if isinstance(data, dict) else {}
    normalized = {}
    for key, value in data.items():
        normalized.setdefault(_normalized_field_name(key), []).append(value)

    def first_value(aliases):
        for alias in aliases:
            for value in normalized.get(_normalized_field_name(alias), []):
                if not _missing_personalization_value(value):
                    return str(value).strip()
        return None

    # Prefer full-name fields; only combine first/last name if no usable full name exists.
    person = first_value(("Full Name", "Person Name", "Recipient Name", "Contact Name", "Name"))
    if not person:
        first = first_value(("First Name", "Given Name"))
        last = first_value(("Last Name", "Surname", "Family Name"))
        person = " ".join(x for x in (first, last) if x) or None
    company = first_value(("Company Name", "Company", "Organization", "Organisation", "Employer", "Business Name", "Firm"))
    if apply_defaults:
        return {"person_name": person or "Hiring Team", "company": company or "your organization"}
    return {"person_name": person, "company": company}


def _placeholder_tokens(template):
    """Return brace-delimited template tokens without evaluating expressions."""
    return [match.group(0) for match in re.finditer(r"\{([^{}]+)\}", str(template))]


def _supported_default_placeholder(token):
    """Parse only the documented field/default syntax; never evaluate expressions."""
    match = re.fullmatch(
        r'\{\s*(person_name|company)\s*(?:\|\s*default:\s*"((?:[^"\\]|\\.)*)"\s*)?\}',
        token,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    field = match.group(1).casefold()
    default = match.group(2)
    return field, default


def unsupported_placeholders(template, data=None):
    """List unresolved placeholders so the UI/API can warn before a campaign starts."""
    data = data if isinstance(data, dict) else {}
    known_fields = {_normalized_field_name(key) for key in data}
    unsupported = []
    for token in _placeholder_tokens(template):
        parsed = _supported_default_placeholder(token)
        if parsed:
            continue
        key = token[1:-1].strip()
        alias = _normalized_field_name(key)
        if alias in {"personname", "company"} or alias in known_fields:
            # Plain legacy placeholders and known contact merge fields remain supported.
            continue
        unsupported.append(token)
    return sorted(set(unsupported))


def personalize_text(template, data):
    """Render supported fields/defaults deterministically for one recipient."""
    data = data if isinstance(data, dict) else {}
    canonical = resolve_personalization(data, apply_defaults=False)
    values = {str(k).strip(): "" if _missing_personalization_value(v) else str(v).strip() for k, v in data.items()}
    normalized = {}
    for key, value in values.items():
        normalized.setdefault(_normalized_field_name(key), value)

    pattern = re.compile(r"\{([^{}]+)\}")

    def replace(match):
        token = match.group(0)
        parsed = _supported_default_placeholder(token)
        if parsed:
            field, explicit_default = parsed
            value = canonical[field]
            if _missing_personalization_value(value):
                value = explicit_default if explicit_default is not None else ("Hiring Team" if field == "person_name" else "your organization")
            return value

        key = match.group(1).strip()
        alias = _normalized_field_name(key)
        if alias == "personname":
            return canonical["person_name"] or "Hiring Team"
        if alias == "company":
            return canonical["company"] or "your organization"
        if key in values:
            return values[key]
        if alias in normalized:
            return normalized[alias]
        # Keep unsupported/literal braces intact; campaign validation reports them.
        return token

    return pattern.sub(replace, str(template))


class EmailMessageBuilder:
    """Builds an independent multipart message for each recipient."""
    def __init__(self):
        self._logo = self._load_logo()

    @staticmethod
    def _load_logo():
        # Logo has been disabled for outgoing emails.
        return None

    @property
    def logo_size(self):
        return len(self._logo["content"]) if self._logo else 0

    def _signature_html(self):
        return "<br><br><strong style='color: #000000;'>Warm regards,</strong><br><br><strong style='color: #000000;'>Rushi Kapadnis</strong>"

    def build(self, sender, recipient, subject, body, attachments):
        if any("\r" in value or "\n" in value for value in (recipient, subject)):
            raise ValueError("Recipient and subject must not contain line breaks.")
        message = EmailMessage(policy=SMTP)
        # Display formatting belongs only in the MIME header, never MAIL FROM.
        message["From"], message["To"], message["Subject"] = formataddr(("Rushi", sender)), recipient, subject
        message.set_content(body)
        body_html = html.escape(body).replace("\n", "<br>\n")
        message.add_alternative(f"<html><body>{body_html}{self._signature_html()}</body></html>", subtype="html")
        if self._logo:
            html_part = message.get_payload()[-1]
            html_part.add_related(self._logo["content"], maintype=self._logo["maintype"], subtype=self._logo["subtype"],
                                  cid=f"<{Config.COMPANY_LOGO_CID}>", disposition="inline", filename=self._logo["filename"])
        for attachment in attachments:
            mime_type = mimetypes.guess_type(attachment["filename"])[0] or "application/octet-stream"
            maintype, subtype = mime_type.split("/", 1)
            message.add_attachment(attachment["content"], maintype=maintype, subtype=subtype, filename=attachment["filename"])
        return message


class CampaignManager:
    def __init__(self):
        self.campaign_queue = queue.Queue(maxsize=100)
        self.recipient_queue = queue.Queue(maxsize=Config.EMAIL_QUEUE_SIZE)
        self.enqueued, self.lock = set(), threading.Lock()
        self.rate_lock, self.last_send, self.started = threading.Lock(), 0, False
        self.payload_cache, self.payload_lock = {}, threading.Lock()

    def start(self):
        if self.started:
            logger.info("[STARTUP] CampaignManager.start() called again; manager already started")
            return

        self.started = True
        logger.info(
            "[STARTUP] Starting MailFlow campaign manager: smtp_workers=%s recipient_queue_size=%s",
            Config.EMAIL_WORKERS,
            Config.EMAIL_QUEUE_SIZE,
        )

        # Start the dispatcher before SMTP workers so queued campaigns can be
        # observed immediately. Every thread is daemonized because Gunicorn
        # owns the web-process lifecycle.
        threading.Thread(target=self._dispatcher, name="mailflow-dispatcher", daemon=True).start()
        for index in range(Config.EMAIL_WORKERS):
            threading.Thread(target=self._worker, name=f"mailflow-smtp-{index + 1}", daemon=True).start()
        threading.Thread(target=self._scheduler, name="mailflow-scheduler", daemon=True).start()

        try:
            recovered = database.recover_campaigns()
            logger.info("[STARTUP] Recovery returned %s campaign(s)", len(recovered))
            for campaign_id in recovered:
                self.enqueue(campaign_id)
        except Exception:
            # A recovery-query failure must be visible in Render logs; it must
            # not silently prevent the long-running worker threads from starting.
            logger.exception("[STARTUP] Campaign recovery failed")

    def enqueue(self, campaign_id):
        if not campaign_id:
            raise ValueError("campaign_id is required")
        with self.lock:
            if campaign_id in self.enqueued:
                logger.info("[QUEUE] campaign_id=%s is already queued for dispatch", campaign_id)
                return
            self.enqueued.add(campaign_id)
        try:
            self.campaign_queue.put_nowait(campaign_id)
            logger.info("[QUEUE] Enqueued campaign_id=%s campaign_queue_size=%s", campaign_id, self.campaign_queue.qsize())
        except queue.Full as exc:
            with self.lock:
                self.enqueued.discard(campaign_id)
            logger.exception("[QUEUE] Campaign queue is full for campaign_id=%s", campaign_id)
            raise RuntimeError("Campaign queue is busy; please try again shortly.") from exc

    def _dispatcher(self):
        logger.info("[DISPATCHER] Thread started")
        while True:
            campaign_id = self.campaign_queue.get()
            should_retry = False
            try:
                logger.info("[DISPATCHER] Loading queued recipients for campaign_id=%s", campaign_id)
                recipient_ids = database.queued_recipient_ids(campaign_id)
                logger.info(
                    "[DISPATCHER] campaign_id=%s queued_recipients=%s recipient_queue_size=%s",
                    campaign_id, len(recipient_ids), self.recipient_queue.qsize(),
                )
                # Keep campaign ownership with each queue item. This prevents a
                # stale recipient ID from being claimed under another campaign.
                # The queue is bounded; put() intentionally applies backpressure
                # while SMTP workers consume recipients.
                for recipient_id in recipient_ids:
                    self.recipient_queue.put((campaign_id, recipient_id))
                logger.info(
                    "[DISPATCHER] Dispatch complete for campaign_id=%s; recipient_queue_size=%s",
                    campaign_id, self.recipient_queue.qsize(),
                )
            except Exception:
                # Without this guard, one SQLite/query exception could terminate
                # the dispatcher forever while the UI remains stuck at Queued.
                logger.exception("[DISPATCHER] Failed to dispatch campaign_id=%s; will retry", campaign_id)
                should_retry = True
            finally:
                with self.lock:
                    self.enqueued.discard(campaign_id)
                self.campaign_queue.task_done()
            if should_retry:
                time.sleep(2)
                try:
                    self.enqueue(campaign_id)
                except Exception:
                    logger.exception("[DISPATCHER] Could not requeue campaign_id=%s", campaign_id)

    def _scheduler(self):
        logger.info("[SCHEDULER] Thread started")
        while True:
            try:
                due_campaigns = database.activate_due_campaigns()
                for campaign_id in due_campaigns:
                    logger.info("[SCHEDULER] Activating due campaign_id=%s", campaign_id)
                    self.enqueue(campaign_id)

                # Some database versions also expose retry/resume activation.
                # Keep this worker compatible with versions that do not.
                activate_retries = getattr(database, "activate_due_retries_and_resumes", None)
                if callable(activate_retries):
                    for campaign_id in activate_retries() or []:
                        logger.info("[SCHEDULER] Activating retry/resume campaign_id=%s", campaign_id)
                        self.enqueue(campaign_id)

                cleanup = getattr(database, "cleanup_expired_attachment_files", None)
                if callable(cleanup):
                    cleanup(Config.ATTACHMENT_RETENTION_SECONDS)
            except Exception:
                logger.exception("[SCHEDULER] Scheduled campaign check failed")
            time.sleep(5)

    def _payload(self, campaign_id):
        with self.payload_lock:
            if campaign_id not in self.payload_cache:
                campaign, attachments = database.campaign_payload(campaign_id)
                prepared = []
                for attachment in attachments:
                    if "content" in attachment:
                        prepared.append(attachment)
                        continue
                    try:
                        with open(attachment["file_path"], "rb") as source:
                            prepared.append({**attachment, "content": source.read()})
                    except OSError as exc:
                        raise RuntimeError(f"Attachment '{attachment['filename']}' cannot be read: {exc}") from exc
                self.payload_cache[campaign_id] = campaign, prepared
            return self.payload_cache[campaign_id]

    def _release_payload_if_finished(self, campaign_id):
        campaign = database.get_campaign(campaign_id, include_failed=False)
        if campaign and campaign["status"] in database.TERMINAL_CAMPAIGN_STATUSES:
            with self.payload_lock:
                self.payload_cache.pop(campaign_id, None)

    def _wait_for_rate_limit(self):
        if not Config.EMAIL_RATE_LIMIT:
            return
        with self.rate_lock:
            wait = self.last_send + 1 / Config.EMAIL_RATE_LIMIT - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last_send = time.monotonic()

    @staticmethod
    def _error_details(exc):
        if isinstance(exc, smtplib.SMTPAuthenticationError): return "SMTP authentication failed", False, True
        if isinstance(exc, smtplib.SMTPRecipientsRefused): return "Recipient rejected", False, False
        if isinstance(exc, smtplib.SMTPSenderRefused): return "SMTP sender address rejected. Verify the authenticated Zoho sender address.", False, True
        if isinstance(exc, smtplib.SMTPDataError):
            return ("Message exceeds SMTP size limit", False, False) if exc.smtp_code in (552, 554) else (f"SMTP data rejected ({exc.smtp_code})", exc.smtp_code in (421, 450, 451, 452), False)
        if isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError, socket.timeout, TimeoutError, ConnectionError, OSError)):
            return "SMTP connection lost or timed out", True, False
        if isinstance(exc, smtplib.SMTPResponseException): return f"SMTP server rejected message ({exc.smtp_code})", exc.smtp_code in (421, 450, 451, 452), False
        return "SMTP server error", True, False

    def _worker(self):
        connection, builder = SMTPConnectionManager(), EmailMessageBuilder()
        logger.info("[WORKER] SMTP worker started; smtp_server=%s port=%s", Config.SMTP_SERVER, Config.SMTP_PORT)
        while True:
            queue_item = self.recipient_queue.get()
            claimed = None
            campaign_id = None
            recipient_id = queue_item
            if isinstance(queue_item, (tuple, list)) and len(queue_item) == 2:
                campaign_id, recipient_id = queue_item
            try:
                logger.info(
                    "[WORKER] Picked campaign_id=%s recipient_id=%s recipient_queue_size=%s",
                    campaign_id, recipient_id, self.recipient_queue.qsize(),
                )
                # Prefer campaign-scoped claiming when supported by the installed
                # database.py; retain compatibility with its older one-argument API.
                try:
                    claimed = database.claim_recipient(recipient_id, campaign_id=campaign_id)
                except TypeError as exc:
                    if "campaign_id" not in str(exc):
                        raise
                    claimed = database.claim_recipient(recipient_id)
                if not claimed:
                    logger.warning("[WORKER] Could not claim recipient_id=%s; recipient may no longer be queued or campaign may be inactive", recipient_id)
                    continue
                logger.info("[WORKER] Claimed recipient_id=%s campaign_id=%s email=%s", recipient_id, claimed.get("campaign_id"), claimed.get("email"))
                campaign, attachments = self._payload(claimed["campaign_id"])
                if estimate_message_size(campaign["body"], attachments, builder.logo_size) > Config.SMTP_MAX_MESSAGE_SIZE:
                    database.recipient_result(recipient_id, "failed", "Message exceeds configured SMTP size limit")
                    continue
                build_started = time.monotonic()
                sender = Config.smtp_username()
                try:
                    personalization = json.loads(claimed.get("personalization") or "{}")
                except (json.JSONDecodeError, TypeError):
                    personalization = {"Email": claimed["email"]}
                # Rendering is safe for every campaign: text without placeholders
                # remains unchanged, while known recipient fields are substituted.
                # This also works with database schemas that do not persist a
                # separate personalization_enabled campaign flag.
                personalized_subject = personalize_text(campaign["subject"], personalization)
                personalized_body = personalize_text(campaign["body"], personalization)
                if any("\r" in value or "\n" in value for value in (personalized_subject,)):
                    raise ValueError("Personalized subject must not contain line breaks.")
                message = builder.build(sender, claimed["email"], personalized_subject, personalized_body, attachments)
                message_bytes = message.as_bytes()
                build_elapsed = time.monotonic() - build_started
                if len(message_bytes) > Config.SMTP_MAX_MESSAGE_SIZE:
                    database.recipient_result(recipient_id, "failed", "Generated MIME message exceeds configured SMTP size limit")
                    continue
                logger.info("[PERF] recipient_id=%s build_and_encode=%.2fs size=%.2fMB", recipient_id, build_elapsed, len(message_bytes) / 1024 / 1024)
                for attempt in range(1, Config.MAX_EMAIL_RETRIES + 2):
                    database.record_attempt(recipient_id)
                    send_started = time.monotonic()
                    try:
                        logger.info("[SMTP] Starting recipient_id=%s attempt=%s", recipient_id, attempt)
                        self._wait_for_rate_limit(); connection.send_message(message_bytes, claimed["email"], bool(attachments))
                        database.recipient_result(recipient_id, "sent")
                        logger.info("[PERF] recipient_id=%s attempt=%s send=%.2fs total=%.2fs SUCCESS", recipient_id, attempt, time.monotonic() - send_started, time.monotonic() - build_started)
                        break
                    except (smtplib.SMTPException, OSError, socket.timeout, TimeoutError, ConnectionError) as exc:
                        reason, retryable, fatal_auth = self._error_details(exc)
                        logger.warning("recipient_id=%s attempt=%s stage=%s elapsed=%.2fs error=%s reason=%s", recipient_id, attempt, connection.stage, time.monotonic() - send_started, type(exc).__name__, reason)
                        connection.discard()
                        if fatal_auth:
                            database.fail_campaign_configuration(claimed["campaign_id"], reason)
                            break
                        if not retryable or attempt > Config.MAX_EMAIL_RETRIES:
                            database.recipient_result(recipient_id, "failed", reason)
                            break
                        logger.info("recipient_id=%s retrying once after %.2fs", recipient_id, Config.RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                        time.sleep(Config.RETRY_BASE_DELAY * (2 ** (attempt - 1)))
                    except Exception:
                        logger.exception("recipient_id=%s | unexpected send failure", recipient_id)
                        database.recipient_result(recipient_id, "failed", "Unexpected application error")
                        break
            except (SMTPConfigurationError, ValueError) as exc:
                logger.error("SMTP configuration error: %s", exc)
                if claimed:
                    database.fail_campaign_configuration(claimed["campaign_id"], f"SMTP sender configuration error: {exc}")
            except Exception as exc:
                # Payload/file failures occur before the send retry loop. They
                # still belong to this recipient and must never strand it in
                # 'sending' or stop later recipients.
                logger.exception("recipient_id=%s could not be prepared", recipient_id)
                if claimed:
                    database.recipient_result(recipient_id, "failed", f"Attachment or campaign preparation failed: {exc}")
            finally:
                if claimed:
                    self._release_payload_if_finished(claimed["campaign_id"])
                self.recipient_queue.task_done()


campaign_manager = CampaignManager()
