import os
import smtplib
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

# Keep the worker unit test runnable without requiring the web application's
# optional environment-loader package to be installed in the test interpreter.
dotenv_stub = types.ModuleType("dotenv")
dotenv_stub.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", dotenv_stub)

import database
from config import Config
from config import normalized_smtp_username
from email_worker import CampaignManager, SMTPConnectionManager, personalize_text, resolve_personalization, unsupported_placeholders


class CampaignFailureIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.original_db = Config.DATABASE_URL
        self.original_retries = Config.MAX_EMAIL_RETRIES
        self.original_username = Config.SMTP_USERNAME
        self.original_password = Config.SMTP_PASSWORD
        Config.DATABASE_URL = f"sqlite:///{self.temp_db.name}"
        Config.MAX_EMAIL_RETRIES = 1
        Config.SMTP_USERNAME = "sender@example.test"
        Config.SMTP_PASSWORD = "test-password"
        database.init_database()

    def tearDown(self):
        Config.DATABASE_URL = self.original_db
        Config.MAX_EMAIL_RETRIES = self.original_retries
        Config.SMTP_USERNAME = self.original_username
        Config.SMTP_PASSWORD = self.original_password
        # The daemon worker can briefly retain SQLite's Windows file handle after
        # queue.join(); a failed cleanup must not invalidate the behavior test.
        try:
            os.unlink(self.temp_db.name)
        except PermissionError:
            pass

    def test_disconnect_for_one_recipient_does_not_block_the_next(self):
        campaign_id = database.create_campaign(
            ["first@example.test", "broken@example.test", "third@example.test"],
            "Subject", "Body", [],
        )
        recipient_ids = database.queued_recipient_ids(campaign_id)
        manager = CampaignManager()
        calls = []

        def fake_send(_, __, recipient, *args):
            calls.append(recipient)
            if recipient == "broken@example.test":
                raise smtplib.SMTPServerDisconnected("forced disconnect")

        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            worker = threading.Thread(target=manager._worker, daemon=True)
            worker.start()
            for recipient_id in recipient_ids:
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()

        campaign = database.get_campaign(campaign_id)
        self.assertEqual(campaign["sent"], 2)
        self.assertEqual(campaign["failed"], 1)
        self.assertEqual(campaign["status"], "partially_failed")
        self.assertEqual(calls.count("broken@example.test"), 2)
        self.assertIn("third@example.test", calls)

    def test_worker_sends_recipient_specific_subject_and_body(self):
        from email import policy
        from email.parser import BytesParser
        campaign_id = database.create_campaign(
            [
                {"email": "asha@example.test", "data": {"Name": "Asha", "Company": "Northwind"}},
                {"email": "dev@example.test", "data": {"Name": "Dev", "Company": "Contoso"}},
            ],
            "Hello {person_name}", "Opportunity at {company}", [], personalization_enabled=True,
        )
        manager, sent = CampaignManager(), {}
        def fake_send(_, message_bytes, recipient, *args):
            message = BytesParser(policy=policy.default).parsebytes(message_bytes)
            sent[recipient] = (str(message["Subject"]), message.get_body(preferencelist=("plain",)).get_content())
        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            threading.Thread(target=manager._worker, daemon=True).start()
            for recipient_id in database.queued_recipient_ids(campaign_id):
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()
        self.assertEqual(sent["asha@example.test"][0], "Hello Asha")
        self.assertIn("Opportunity at Northwind", sent["asha@example.test"][1])
        self.assertEqual(sent["dev@example.test"][0], "Hello Dev")
        self.assertIn("Opportunity at Contoso", sent["dev@example.test"][1])

    def test_actual_worker_mime_has_four_exact_fallback_results(self):
        from email import policy
        from email.parser import BytesParser
        template = 'Dear {person_name | default: "Hiring Team"},\n\nOpportunities at {company | default: "your organization"}.'
        recipients = [
            {"email": "rahul@example.test", "data": {"Person Name": "Rahul Sharma", "Company": "Acme Technologies"}},
            {"email": "priya@example.test", "data": {"Person Name": "", "Company": "Bright Solutions"}},
            {"email": "amit@example.test", "data": {"Person Name": "Amit Patil", "Company": ""}},
            {"email": "neha@example.test", "data": {"Person Name": None, "Company": None}},
        ]
        campaign_id = database.create_campaign(recipients, 'Application from {person_name | default: "Hiring Team"}', template, [], personalization_enabled=True)
        manager, sent = CampaignManager(), {}
        def fake_send(_, message_bytes, recipient, *args):
            message = BytesParser(policy=policy.default).parsebytes(message_bytes)
            sent[recipient] = (str(message["Subject"]), message.get_body(preferencelist=("plain",)).get_content())
        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            threading.Thread(target=manager._worker, daemon=True).start()
            for recipient_id in database.queued_recipient_ids(campaign_id):
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()
        expected = {
            "rahul@example.test": ("Application from Rahul Sharma", "Dear Rahul Sharma,\n\nOpportunities at Acme Technologies."),
            "priya@example.test": ("Application from Hiring Team", "Dear Hiring Team,\n\nOpportunities at Bright Solutions."),
            "amit@example.test": ("Application from Amit Patil", "Dear Amit Patil,\n\nOpportunities at your organization."),
            "neha@example.test": ("Application from Hiring Team", "Dear Hiring Team,\n\nOpportunities at your organization."),
        }
        for email, (expected_subject, expected_body) in expected.items():
            self.assertEqual(sent[email][0], expected_subject)
            self.assertEqual(sent[email][1].replace("\r\n", "\n").rstrip("\n"), expected_body)
            self.assertNotIn('{person_name | default: "Hiring Team"}', sent[email][0] + sent[email][1])
            self.assertNotIn('{company | default: "your organization"}', sent[email][0] + sent[email][1])

    def test_actual_worker_sends_100_recipient_specific_messages(self):
        from email import policy
        from email.parser import BytesParser
        recipients = [
            {"email": f"candidate{i}@example.test", "data": {"Person Name": f"Candidate {i}", "Company": f"Company {i}"}}
            for i in range(100)
        ]
        campaign_id = database.create_campaign(recipients, 'Hello {person_name | default: "Hiring Team"}', 'At {company | default: "your organization"}', [], personalization_enabled=True)
        manager, sent = CampaignManager(), {}
        def fake_send(_, message_bytes, recipient, *args):
            message = BytesParser(policy=policy.default).parsebytes(message_bytes)
            sent[recipient] = (str(message["Subject"]), message.get_body(preferencelist=("plain",)).get_content())
        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            threading.Thread(target=manager._worker, daemon=True).start()
            for recipient_id in database.queued_recipient_ids(campaign_id):
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()
        self.assertEqual(len(sent), 100)
        for index in range(100):
            subject, body = sent[f"candidate{index}@example.test"]
            self.assertEqual(subject, f"Hello Candidate {index}")
            self.assertEqual(body.replace("\r\n", "\n").rstrip("\n"), f"At Company {index}")

    def test_new_attachment_rows_store_only_filesystem_metadata(self):
        attachment = tempfile.NamedTemporaryFile(delete=False)
        attachment.write(b"campaign attachment")
        attachment.close()
        try:
            campaign_id = database.create_campaign(
                ["recipient@example.test"], "Subject", "Body",
                [{"filename": "attachment.txt", "file_path": attachment.name, "size": 19, "mime_type": "text/plain"}],
            )
            with database.connection() as db:
                columns = [row[1] for row in db.execute("PRAGMA table_info(attachments)")]
                row = db.execute("SELECT filename,file_path,size,mime_type FROM attachments WHERE campaign_id=?", (campaign_id,)).fetchone()
            self.assertNotIn("content", columns)
            self.assertEqual(dict(row)["file_path"], attachment.name)
        finally:
            os.unlink(attachment.name)

    def test_timeout_for_one_recipient_does_not_block_the_next(self):
        campaign_id = database.create_campaign(
            ["first@example.test", "timeout@example.test", "third@example.test"],
            "Subject", "Body", [],
        )
        manager, calls = CampaignManager(), []

        def fake_send(_, __, recipient, *args):
            calls.append(recipient)
            if recipient == "timeout@example.test":
                raise TimeoutError("forced timeout")

        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            threading.Thread(target=manager._worker, daemon=True).start()
            for recipient_id in database.queued_recipient_ids(campaign_id):
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()

        campaign = database.get_campaign(campaign_id)
        self.assertEqual((campaign["sent"], campaign["failed"], campaign["status"]), (2, 1, "partially_failed"))
        self.assertEqual(calls.count("timeout@example.test"), 2)
        self.assertIn("third@example.test", calls)

    def test_attachment_campaign_builds_and_sends_each_recipient(self):
        attachment = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        attachment.write(b"attachment content")
        attachment.close()
        try:
            campaign_id = database.create_campaign(
                ["one@example.test", "two@example.test", "three@example.test"], "Subject", "Body",
                [{"filename": "attachment.txt", "file_path": attachment.name, "size": 18, "mime_type": "text/plain"}],
            )
            manager, calls = CampaignManager(), []

            def fake_send(_, message_bytes, recipient, has_attachments=False):
                self.assertTrue(has_attachments)
                self.assertIn(b"attachment.txt", message_bytes)
                calls.append(recipient)

            with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
                threading.Thread(target=manager._worker, daemon=True).start()
                for recipient_id in database.queued_recipient_ids(campaign_id):
                    manager.recipient_queue.put(recipient_id)
                manager.recipient_queue.join()

            campaign = database.get_campaign(campaign_id)
            self.assertEqual((campaign["sent"], campaign["failed"], campaign["status"]), (3, 0, "completed"))
            self.assertEqual(calls, ["one@example.test", "two@example.test", "three@example.test"])
        finally:
            os.unlink(attachment.name)

    def test_sender_rejection_fails_campaign_without_retrying_each_recipient(self):
        campaign_id = database.create_campaign(
            ["one@example.test", "two@example.test", "three@example.test"], "Subject", "Body", [],
        )
        manager, calls = CampaignManager(), []

        def fake_send(_, __, recipient, *args):
            calls.append(recipient)
            raise smtplib.SMTPSenderRefused(501, b"syntax error", "sender@example.test")

        with patch("email_worker.SMTPConnectionManager.send_message", new=fake_send):
            threading.Thread(target=manager._worker, daemon=True).start()
            for recipient_id in database.queued_recipient_ids(campaign_id):
                manager.recipient_queue.put(recipient_id)
            manager.recipient_queue.join()

        campaign = database.get_campaign(campaign_id)
        self.assertEqual((campaign["sent"], campaign["failed"], campaign["status"]), (0, 3, "failed"))
        self.assertEqual(calls, ["one@example.test"])
        self.assertIn("SMTP sender address rejected", campaign["fatal_error"])

    def test_send_message_uses_bare_envelope_sender_and_correct_stages(self):
        class FakeSMTP:
            def __init__(self): self.sock = None; self.calls = []
            def has_extn(self, name): return False
            def mail(self, sender, options=()): self.calls.append(("MAIL_FROM", sender, options)); return 250, b"ok"
            def rcpt(self, recipient): self.calls.append(("RCPT_TO", recipient)); return 250, b"ok"
            def data(self, message): self.calls.append(("DATA", message)); return 250, b"ok"

        connection = SMTPConnectionManager()
        connection.smtp = FakeSMTP()
        connection.send_message(b"message", "recipient@example.test")
        self.assertEqual(connection.smtp.calls[0], ("MAIL_FROM", "sender@example.test", []))
        self.assertEqual([call[0] for call in connection.smtp.calls], ["MAIL_FROM", "RCPT_TO", "DATA"])

    def test_sender_rejection_is_identified_as_mail_from(self):
        class RejectingSMTP:
            sock = None
            def has_extn(self, name): return False
            def mail(self, sender, options=()): return 501, b"syntax error in parameters or arguments"

        connection = SMTPConnectionManager()
        connection.smtp = RejectingSMTP()
        with self.assertRaises(smtplib.SMTPSenderRefused):
            connection.send_message(b"message", "recipient@example.test")
        self.assertEqual(connection.stage, "MAIL_FROM")

    def test_smtp_username_rejects_display_name_and_control_characters(self):
        self.assertEqual(normalized_smtp_username(" sender@example.test "), "sender@example.test")
        for invalid in ('Name <sender@example.test>', 'sender@example.test,other@example.test', 'sender@\nexample.test'):
            with self.assertRaises(ValueError):
                normalized_smtp_username(invalid)


class PersonalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.original_db = Config.DATABASE_URL
        Config.DATABASE_URL = f"sqlite:///{self.temp_db.name}"
        database.init_database()

    def tearDown(self):
        Config.DATABASE_URL = self.original_db
        try:
            os.unlink(self.temp_db.name)
        except PermissionError:
            pass

    def test_supported_fields_and_exact_fallbacks(self):
        template = "Dear {person_name}, opportunities at {company}."
        self.assertEqual(personalize_text(template, {"Full Name": "Rahul Sharma", "Company Name": "Acme Technologies"}), "Dear Rahul Sharma, opportunities at Acme Technologies.")
        self.assertEqual(personalize_text(template, {"Name": "Rahul"}), "Dear Rahul, opportunities at your organization.")
        self.assertEqual(personalize_text(template, {"Company": "Acme"}), "Dear Hiring Team, opportunities at Acme.")
        self.assertEqual(personalize_text(template, {}), "Dear Hiring Team, opportunities at your organization.")

    def test_exact_default_syntax_renders_all_four_required_cases(self):
        template = 'Dear {person_name | default: "Hiring Team"},\n\nI’m reaching out to explore suitable opportunities in AI Engineering, Generative AI, Machine Learning, Forward Deployed Engineering (FDE), or related roles at {company | default: "your organization"}.'
        cases = [
            ({"Person Name": "Rahul Sharma", "Company": "Acme Technologies"}, "Dear Rahul Sharma,", "related roles at Acme Technologies."),
            ({"Person Name": "", "Company": "Bright Solutions"}, "Dear Hiring Team,", "related roles at Bright Solutions."),
            ({"Person Name": "Amit Patil", "Company": ""}, "Dear Amit Patil,", "related roles at your organization."),
            ({"Person Name": None, "Company": None}, "Dear Hiring Team,", "related roles at your organization."),
        ]
        for data, greeting, company_text in cases:
            rendered = personalize_text(template, data)
            self.assertTrue(rendered.startswith(greeting), rendered)
            self.assertIn(company_text, rendered)
            self.assertNotIn('{person_name | default: "Hiring Team"}', rendered)
            self.assertNotIn('{company | default: "your organization"}', rendered)

    def test_default_syntax_in_subject_and_custom_default(self):
        self.assertEqual(personalize_text('To {person_name | default: "Hiring Team"} at {company | default: "your organization"}', {"Name": "Amit Patil"}), "To Amit Patil at your organization")
        self.assertEqual(personalize_text('{person_name | default: "Recruitment Team"}', {}), "Recruitment Team")

    def test_unsupported_placeholders_are_reported_but_not_evaluated(self):
        template = 'Hi {person_name | default: "Hiring Team"} {unknown_token}'
        self.assertEqual(unsupported_placeholders(template, {"Name": "Asha"}), ["{unknown_token}"])
        self.assertEqual(personalize_text(template, {}), 'Hi Hiring Team {unknown_token}')

    def test_100_recipients_get_independent_default_template_content(self):
        template = 'Dear {person_name | default: "Hiring Team"}, at {company | default: "your organization"}'
        rendered = [personalize_text(template, {"Person Name": f"Person {i}", "Company": f"Company {i}"}) for i in range(100)]
        self.assertEqual(len(set(rendered)), 100)
        for i, message in enumerate(rendered):
            self.assertEqual(message, f"Dear Person {i}, at Company {i}")

    def test_missing_markers_and_normalized_headers(self):
        for value in (None, "", "   ", "nan", "NaN", "None", "null", "NaT", "N/A", "<NA>", float("nan")):
            self.assertEqual(resolve_personalization({"Person-Name": value, "Company_Name": value}), {"person_name": "Hiring Team", "company": "your organization"})
        self.assertEqual(resolve_personalization({"FIRST_name": "  Ana ", "last-name": "Rao", "ORGaniZation!!!": "Example Ltd"}), {"person_name": "Ana Rao", "company": "Example Ltd"})

    def test_each_recipient_renders_its_own_content_and_disabled_mode_is_unchanged(self):
        template = "Hello {person_name} at {company}"
        first = personalize_text(template, {"Name": "Asha", "Company": "Northwind"})
        second = personalize_text(template, {"Name": "Dev", "Company": "Contoso"})
        self.assertEqual(first, "Hello Asha at Northwind")
        self.assertEqual(second, "Hello Dev at Contoso")
        self.assertNotEqual(first, second)
        # Worker bypasses personalize_text when disabled; source template stays literal.
        self.assertEqual(template, "Hello {person_name} at {company}")

    def test_full_name_preferred_and_blank_alias_does_not_override_value(self):
        self.assertEqual(resolve_personalization({"Name": "", "Full Name": "Priya Shah", "First Name": "Wrong", "Last Name": "Person"})["person_name"], "Priya Shah")
        self.assertEqual(resolve_personalization({"Name": "None", "First Name": "Ravi", "Last Name": "Patil"})["person_name"], "Ravi Patil")

    def test_unrelated_braces_remain_and_html_like_values_are_not_injected(self):
        from email_worker import EmailMessageBuilder
        rendered = personalize_text("Literal {unmatched_token} {person_name}", {"Name": "<script>alert(1)</script>"})
        self.assertEqual(rendered, "Literal {unmatched_token} <script>alert(1)</script>")
        builder = EmailMessageBuilder()
        message = builder.build("sender@example.test", "recipient@example.test", "Subject", rendered, [])
        self.assertIn(b"&lt;script&gt;alert(1)&lt;/script&gt;", message.as_bytes())

    def test_personalization_enabled_is_persisted_and_retry_keeps_it(self):
        campaign_id = database.create_campaign([{"email": "person@example.test", "data": {"Name": "Jamie"}}], "Hi {person_name}", "At {company}", [], personalization_enabled=True)
        with database.connection() as db:
            row = db.execute("SELECT personalization_enabled,personalization FROM campaigns JOIN recipients ON campaigns.id=recipients.campaign_id WHERE campaigns.id=?", (campaign_id,)).fetchone()
            db.execute("UPDATE recipients SET status='failed' WHERE campaign_id=?", (campaign_id,))
        self.assertEqual(row["personalization_enabled"], 1)
        retry_id = database.retry_failed_campaign(campaign_id)
        with database.connection() as db:
            retry = db.execute("SELECT personalization_enabled FROM campaigns WHERE id=?", (retry_id,)).fetchone()
            recipient = db.execute("SELECT personalization FROM recipients WHERE campaign_id=?", (retry_id,)).fetchone()
        self.assertEqual(retry["personalization_enabled"], 1)
        self.assertIn("Jamie", recipient["personalization"])


if __name__ == "__main__":
    unittest.main()
