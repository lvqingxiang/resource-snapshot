"""Opt-in, offline Chromium integration checks: RUN_BROWSER_TESTS=1 python -m unittest discover -s tests -v."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from snapshot.dom import _wait_for_tweet_card
from snapshot import service
from screenshot_service import capture_tweet_page


@unittest.skipUnless(os.getenv('RUN_BROWSER_TESTS') == '1', 'Set RUN_BROWSER_TESTS=1 for Chromium checks')
class LocalBrowserTests(unittest.TestCase):
    def test_card_readiness_and_id_boundaries(self):
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content('<article><a href="/author/status/1234">Wrong post</a></article>')
            self.assertIsNone(_wait_for_tweet_card(page, '123', 200))
            page.evaluate("""() => setTimeout(() => {
                document.body.insertAdjacentHTML('beforeend',
                    '<article id="target"><a href="/author/status/123?ref=test">Target post</a></article>');
            }, 100)""")
            start = monotonic()
            card = _wait_for_tweet_card(page, '123', 2000)
            self.assertIsNotNone(card)
            self.assertEqual(card.get_attribute('id'), 'target')
            self.assertLess(monotonic() - start, 2)
            page.set_content('<article style="display:none" data-tweet-id="123">Hidden</article>'
                             '<article data-tweet-id="123">Visible</article>')
            self.assertEqual(_wait_for_tweet_card(page, '123', 1000).inner_text(), 'Visible')
            browser.close()

    def test_public_preview_translates_text_without_media(self):
        status = {
            'id': '123', 'text': 'Main preview text',
            'author': {'name': 'Fixture', 'screen_name': 'fixture'},
            'quote': {'text': 'Quoted preview text', 'author': {'screen_name': 'quoted'}},
            'media': {'photos': [{'url': 'https://example.invalid/slow-image.png'}]},
        }
        from snapshot import translation
        with TemporaryDirectory() as temp, patch.object(
            service, '_fetch_public_x_status', return_value=(status, 'fixture')
        ), patch.object(
            translation, '_translate_text_to_chinese', side_effect=lambda text, lang: '中文：' + text
        ), patch.object(translation, '_fetch_oembed_tweet_body') as oembed:
            result = service.preview_tweet_translations(
                'https://x.com/fixture/status/123', Path(temp) / 'profile'
            )
        self.assertEqual(result.capture_mode, 'public_api_fallback')
        self.assertEqual([item.original_text for item in result.items],
                         ['Main preview text', 'Quoted preview text'])
        oembed.assert_not_called()

    def test_public_capture_pipeline(self):
        status = {
            'id': '123', 'text': 'Offline capture fixture',
            'author': {'name': 'Test Author', 'screen_name': 'fixture'},
            'quote': {'text': 'Quoted fixture', 'author': {'screen_name': 'quoted'}},
            'likes': 12, 'views': 100,
        }
        with TemporaryDirectory() as temp, patch.object(
            service, '_fetch_public_x_status', return_value=(status, 'fixture')
        ):
            result = capture_tweet_page(
                'https://x.com/fixture/status/123',
                Path(temp) / 'screenshots', Path(temp) / 'profile', anonymous=True,
                translate_body=True, translation_overrides={0: '离线截图验证', 1: '引用内容'},
            )
            self.assertEqual(result.capture_mode, 'public_api_fallback')
            self.assertEqual(result.file_path.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
            self.assertGreater(result.file_path.stat().st_size, 1000)


if __name__ == '__main__':
    unittest.main()
