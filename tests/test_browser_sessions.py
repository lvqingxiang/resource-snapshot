import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from snapshot import browser as service


class BrowserSessionTests(unittest.TestCase):
    def open_session(self, guest_mode, headless):
        playwright = MagicMock()
        with patch.object(service, '_apply_chinese_locale'), patch.object(
            service, '_install_high_quality_hls_routes'
        ), patch.object(service, '_configure_page'):
            session = service._open_capture_session(
                playwright, Path('/tmp/test-profile'), headless=headless,
                dark_mode=True, wait_timeout_ms=30000, guest_mode=guest_mode,
            )
        return playwright, session

    def test_background_session_reuses_profile(self):
        playwright, session = self.open_session(False, True)
        launch = playwright.chromium.launch_persistent_context
        self.assertEqual(launch.call_args.args, ('/tmp/test-profile',))
        self.assertTrue(launch.call_args.kwargs['headless'])
        playwright.chromium.launch.assert_not_called()
        session.close()
        launch.return_value.close.assert_called_once()

    def test_anonymous_visibility_is_independent(self):
        for headless in (True, False):
            with self.subTest(headless=headless):
                playwright, session = self.open_session(True, headless)
                self.assertEqual(playwright.chromium.launch.call_args.kwargs['headless'], headless)
                playwright.chromium.launch_persistent_context.assert_not_called()
                session.close()
                playwright.chromium.launch.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
