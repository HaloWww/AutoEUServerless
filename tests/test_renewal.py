import contextlib
import importlib
import io
import unittest
from unittest.mock import Mock, patch


MODULES = [importlib.import_module(name) for name in ("main", "Github_Action")]


class RenewalTests(unittest.TestCase):
    def test_old_pin_is_ignored_until_new_mail_arrives(self):
        old = {"id": "old-mail", "pin": "123456"}
        new = {"id": "new-mail", "pin": "654321"}
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(module, "get_mailparser_records", side_effect=[[old], [new, old]]))
                sleep = stack.enter_context(patch.object(module.time, "sleep"))
                stack.enter_context(patch.object(module.time, "monotonic", side_effect=[0, 1]))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(module.get_pin_from_mailparser("download-id", {module.mailparser_record_id(old)}), "654321")
                sleep.assert_called_once()

    def test_new_mail_may_have_same_pin_as_previous_mail(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), patch.object(module, "get_mailparser_records", return_value=[{"id": "new", "pin": "123456"}]), contextlib.redirect_stdout(io.StringIO()):
                previous = {module.mailparser_record_id({"id": "old", "pin": "123456"})}
                self.assertEqual(module.get_pin_from_mailparser("download-id", previous), "123456")

    def test_stale_pin_times_out_without_being_submitted(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                old = {"pin": "123456"}
                stack.enter_context(patch.object(module, "get_mailparser_records", return_value=[old]))
                stack.enter_context(patch.object(module.time, "monotonic", side_effect=[0, module.PIN_MAX_WAIT_TIME + 1]))
                with self.assertRaisesRegex(RuntimeError, "新 PIN"):
                    module.get_pin_from_mailparser("download-id", {module.mailparser_record_id(old)})

    def test_confirmation_and_empty_pages_are_failures(self):
        for module in MODULES:
            for html in ("Confirm or change your customer data here", "<html>Login</html>", '<div id="kc2_order_customer_orders_tab_content_1"></div>'):
                with self.subTest(module=module.__name__, html=html):
                    session = Mock()
                    session.get.return_value.text = html
                    with self.assertRaises(RuntimeError):
                        module.get_servers("session", session)

    def test_server_status_explicitly_leaves_contract_details_page(self):
        html = '''<div id="kc2_order_customer_orders_tab_content_1">
        <table class="kc2_order_table kc2_content_table"><tr>
        <td class="td-z1-sp1-kc"> order </td><td class="td-z1-sp2-kc">
        <div class="kc2_order_action_container">Contract extension possible from tomorrow</div>
        </td></tr></table></div>'''
        for module in MODULES:
            with self.subTest(module=module.__name__):
                session = Mock()
                def fetch(**kwargs):
                    response = Mock()
                    response.text = html if kwargs.get("params", {}).get("action") == "show_default" else "Contract details"
                    return response
                session.get.side_effect = fetch
                self.assertEqual(module.get_servers("session", session), {"order": False})

    def test_renewal_must_be_confirmed_by_server_status(self):
        for module in MODULES:
            for after, expected in (({"order": True}, False), ({"other": False}, False), ({"order": False}, True)):
                with self.subTest(module=module.__name__, after=after), contextlib.ExitStack() as stack:
                    session = Mock()
                    session.post.return_value.json.return_value = {"rs": "success", "token": {"value": "token"}}
                    stack.enter_context(patch.object(module, "get_mailparser_records", return_value=[]))
                    stack.enter_context(patch.object(module, "get_pin_from_mailparser", return_value="654321"))
                    stack.enter_context(patch.object(module, "get_servers", return_value=after))
                    stack.enter_context(patch.object(module.time, "sleep"))
                    self.assertIs(module.renew("session", session, "password", "order", "download-id"), expected)

    def test_pin_failure_is_not_overwritten_by_later_successful_check(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                for name, value in (("USERNAME", "user"), ("PASSWORD", "password"), ("MAILPARSER_DOWNLOAD_URL_ID", "download-id"), ("TG_BOT_TOKEN", "")):
                    stack.enter_context(patch.object(module, name, value))
                session = Mock()
                stack.enter_context(patch.object(module, "login", return_value=("session", session)))
                stack.enter_context(patch.object(module, "get_servers", return_value={"order": True}))
                stack.enter_context(patch.object(module, "renew", return_value=False))
                stack.enter_context(patch.object(module, "check", return_value=True))
                stack.enter_context(patch.object(module.time, "sleep"))
                output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertFalse(module.main_handler(None, None))
                self.assertNotIn("所有工作完成", output.getvalue())
                session.close.assert_called_once()

    def test_account_exception_still_notifies_and_fails(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), contextlib.ExitStack() as stack:
                for name, value in (("USERNAME", "user"), ("PASSWORD", "password"), ("MAILPARSER_DOWNLOAD_URL_ID", "download-id"), ("TG_BOT_TOKEN", "token"), ("TG_USER_ID", "user-id")):
                    stack.enter_context(patch.object(module, name, value))
                stack.enter_context(patch.object(module, "login", side_effect=RuntimeError("Mailparser 未收到新 PIN")))
                notify = stack.enter_context(patch.object(module, "telegram"))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertFalse(module.main_handler(None, None))
                notify.assert_called_once()

    def test_check_rejects_missing_orders(self):
        for module in MODULES:
            with self.subTest(module=module.__name__), patch.object(module, "get_servers", return_value={"other": False}), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "不完整"):
                    module.check("session", Mock(), {"order": True})


if __name__ == "__main__":
    unittest.main()
