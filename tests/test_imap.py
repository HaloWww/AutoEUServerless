import contextlib
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
import imaplib
import io
import unittest
from unittest.mock import Mock, patch

from test_renewal import MODULES


REQUESTED = 1793072209


def mail(sender="EUserv Support <no-reply@euserv.com>", recipient="customer@gmail.com", sent=REQUESTED + 1, html=False):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Date"] = format_datetime(datetime.fromtimestamp(sent, timezone.utc))
    message["Subject"] = "EUserv - PIN for the Confirmation of a Security Check"
    message.set_content("<p>PIN:<br>123456</p>" if html else "Dear customer,\nPIN:\n123456\nPLEASE NOTE: session specific.", subtype="html" if html else "plain")
    return message.as_bytes()


class ImapTests(unittest.TestCase):
    def test_plain_and_html_messages(self):
        for module in MODULES:
            for html in (False, True):
                with self.subTest(module=module.__name__, html=html):
                    self.assertEqual(module.parse_euserv_pin(mail(html=html), "customer@gmail.com", REQUESTED, REQUESTED + 2), "123456")

    def test_rejects_wrong_sender_recipient_and_old_mail(self):
        for module in MODULES:
            for raw, received in ((mail(sender="other@example.com"), REQUESTED + 1),
                                  (mail(recipient="someone@gmail.com"), REQUESTED + 1),
                                  (mail(sent=REQUESTED - 86400), REQUESTED + 1),
                                  (mail(), REQUESTED - 86400)):
                with self.subTest(module=module.__name__, received=received):
                    self.assertIsNone(module.parse_euserv_pin(raw, "customer@gmail.com", REQUESTED, received))

    def test_archived_mail_uses_all_mail_folder_read_only(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                client = Mock()
                client.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"'])
                client.select.return_value = ("OK", [b"10"])
                stack.enter_context(patch.object(module.imaplib, "IMAP4_SSL", return_value=client))
                stack.enter_context(patch.object(module, "IMAP_FOLDER", ""))
                stack.enter_context(patch.object(module, "IMAP_USERNAME", "customer@gmail.com"))
                stack.enter_context(patch.object(module, "IMAP_APP_PASSWORD", "test app password"))
                with module.imap_mailbox():
                    pass
                client.select.assert_called_once_with('"[Gmail]/All Mail"', readonly=True)
                client.login.assert_called_once_with("customer@gmail.com", "testapppassword")
                client.logout.assert_called_once()

    def test_authentication_error_does_not_expose_credentials(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                client = Mock()
                client.login.side_effect = imaplib.IMAP4.error("private diagnostic")
                stack.enter_context(patch.object(module.imaplib, "IMAP4_SSL", return_value=client))
                stack.enter_context(patch.object(module, "IMAP_APP_PASSWORD", "test-password"))
                with self.assertRaisesRegex(RuntimeError, "IMAP 登录失败") as caught:
                    with module.imap_mailbox():
                        self.fail("Authentication should fail")
                self.assertNotIn("private diagnostic", str(caught.exception))
                client.logout.assert_called_once()

    def test_new_uid_is_read_without_changing_read_state(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                client = Mock()
                metadata = b'200 (INTERNALDATE ' + imaplib.Time2Internaldate(REQUESTED + 1).encode() + b' BODY[] {123})'
                client.uid.side_effect = [("OK", [b"199 200"]), ("OK", [(metadata, mail()), b")"])]
                mailbox = stack.enter_context(patch.object(module, "imap_mailbox"))
                mailbox.return_value.__enter__.return_value = client
                stack.enter_context(patch.object(module, "imap_state", return_value=(7, 201)))
                output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(module.get_pin_from_imap((7, 200), REQUESTED, "customer@gmail.com"), "123456")
                self.assertEqual(client.uid.call_args_list[-1].args, ("fetch", "200", "(INTERNALDATE BODY.PEEK[])"))
                self.assertNotIn("123456", output.getvalue())

    def test_old_uid_returned_by_range_is_never_used(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                client = Mock()
                client.uid.return_value = ("OK", [b"199"])
                mailbox = stack.enter_context(patch.object(module, "imap_mailbox"))
                mailbox.return_value.__enter__.return_value = client
                stack.enter_context(patch.object(module, "imap_state", return_value=(7, 200)))
                stack.enter_context(patch.object(module.time, "monotonic", side_effect=[0, module.PIN_MAX_WAIT_TIME + 1]))
                with self.assertRaisesRegex(RuntimeError, "未收到"):
                    module.get_pin_from_imap((7, 200), REQUESTED, "customer@gmail.com")
                self.assertEqual(client.uid.call_count, 1)

    def test_changed_uid_validity_requires_new_request(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                mailbox = stack.enter_context(patch.object(module, "imap_mailbox"))
                stack.enter_context(patch.object(module, "imap_state", return_value=(8, 300)))
                with self.assertRaisesRegex(RuntimeError, "UID 已改变"):
                    module.get_pin_from_imap((7, 200), REQUESTED, "customer@gmail.com")
                mailbox.return_value.__enter__.return_value.uid.assert_not_called()

    def test_imap_renewal_does_not_contact_mailparser(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(module, "PIN_SOURCE", "imap"))
                stack.enter_context(patch.object(module, "IMAP_PIN_RECIPIENT", "customer@gmail.com"))
                stack.enter_context(patch.object(module, "get_imap_snapshot", return_value=(7, 200)))
                get_pin = stack.enter_context(patch.object(module, "get_pin_from_imap", return_value="123456"))
                parser = stack.enter_context(patch.object(module, "get_mailparser_records"))
                stack.enter_context(patch.object(module, "get_servers", return_value={"order": False}))
                stack.enter_context(patch.object(module.time, "sleep"))
                session = Mock()
                session.post.return_value.json.return_value = {"rs": "success", "token": {"value": "test-token"}}
                self.assertTrue(module.renew("session", session, "password", "order", ""))
                self.assertEqual(get_pin.call_args.args[0], (7, 200))
                self.assertEqual(get_pin.call_args.args[2], "customer@gmail.com")
                parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
