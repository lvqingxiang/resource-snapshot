from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import MagicMock, patch

from snapshot import service, translation


class TranslationSpeedTests(unittest.TestCase):
    def test_paragraphs_overlap_and_keep_original_order(self):
        barrier = Barrier(2)

        def translate(text, lang):
            barrier.wait(timeout=2)  # A serial implementation cannot pass.
            return '中文：' + text

        blocks = [
            {'index': 0, 'text': 'Main text', 'lang': 'en'},
            {'index': 1, 'text': 'Quoted text', 'lang': 'en'},
            {'index': 2, 'text': 'Main text', 'lang': 'en'},
        ]
        with patch.object(translation, '_translate_text_to_chinese', side_effect=translate) as network:
            items = translation._build_translation_items(blocks)
        self.assertEqual(network.call_count, 2)
        self.assertEqual([item['index'] for item in items], [0, 1, 2])
        self.assertEqual([item['translation'] for item in items],
                         ['中文：Main text', '中文：Quoted text', '中文：Main text'])

    def test_manual_overrides_skip_network_and_use_block_id(self):
        with patch.object(translation, '_translate_text_to_chinese') as network:
            items = translation._build_translation_items(
                [{'index': 4, 'text': 'Main text', 'lang': 'en'}],
                translation_overrides={4: ''},
            )
        network.assert_not_called()
        self.assertEqual(items[0]['translation'], '')

    def test_public_originals_never_need_oembed(self):
        blocks = [{'index': 0, 'text': '中文原帖', 'lang': ''}]
        with patch.object(translation, '_extract_translatable_text_blocks', return_value=blocks), patch.object(
            translation, '_fetch_oembed_tweet_body'
        ) as network, patch.object(translation, '_dismiss_x_auto_translation') as dismiss:
            self.assertEqual(translation._collect_translation_text_blocks(
                MagicMock(), 'public-api://fixture/status/123'), blocks)
        network.assert_not_called()
        dismiss.assert_not_called()

    def test_failed_original_is_only_requested_once_per_preview(self):
        blocks = [{'index': 0, 'text': '中文内容', 'lang': 'zh'}]
        with patch.object(translation, '_extract_translatable_text_blocks', return_value=blocks), patch.object(
            translation, '_fetch_oembed_tweet_body', return_value=(None, None)
        ) as network, patch.object(translation, '_dismiss_x_auto_translation'), patch.object(
            translation, '_extract_quoted_status_urls', return_value=[]
        ):
            translation._collect_translation_text_blocks(MagicMock(), 'https://x.com/a/status/123')
        network.assert_called_once()

    def test_preview_prefers_public_and_skips_media_wait(self):
        session = MagicMock()
        with TemporaryDirectory() as temp, patch.object(service, 'sync_playwright'), patch.object(
            service, '_open_capture_session', return_value=session
        ), patch.object(service, '_fetch_public_x_status', return_value=({'text': 'Original'}, 'fixture')), patch.object(
            service, '_load_public_fallback_tweet_card',
            return_value=(MagicMock(), 'public-api://fixture/status/123', 'public_api_fallback')
        ), patch.object(service, '_prepare_public_fallback_card'), patch.object(
            service, '_collect_translation_text_blocks', return_value=[]
        ), patch.object(service, '_wait_for_tweet_assets') as wait:
            result = service.preview_tweet_translations('https://x.com/a/status/123', Path(temp))
        self.assertEqual(result.capture_mode, 'public_api_fallback')
        session.page.goto.assert_not_called()
        wait.assert_not_called()
        session.close.assert_called_once()
        route_handler = session.page.route.call_args.args[1]
        for resource in ('image', 'media', 'font', 'document', 'xhr'):
            route = MagicMock()
            route.request.resource_type = resource
            route_handler(route)
            self.assertEqual(route.abort.called, resource in {'image', 'media', 'font'})

    def test_public_miss_keeps_detail_page_fallback(self):
        page = MagicMock()
        card = MagicMock()
        with patch.object(service, '_fetch_public_x_status', return_value=(None, '')), patch.object(
            service, '_wait_for_tweet_card', return_value=card
        ), patch.object(service, '_dismiss_common_overlays'), patch.object(
            service, '_expand_tweet_text'
        ), patch.object(service, '_hide_non_primary_columns'):
            result = service._load_tweet_card(
                page, 'https://x.com/a/status/123', 'a', '123',
                dark_mode=True, wait_timeout_ms=30000, prefer_public=True,
            )
        self.assertEqual(result[2], 'detail_page')
        page.goto.assert_called_once()

    def test_blocked_detail_page_skips_card_wait(self):
        page = MagicMock()
        page.goto.return_value.status = 403
        card = MagicMock()
        with patch.object(
            service, '_fetch_public_x_status', return_value=({'text': 'Original'}, 'fixture')
        ), patch.object(service, '_wait_for_tweet_card') as wait, patch.object(
            service, '_load_public_fallback_tweet_card',
            return_value=(card, 'public-api://fixture/status/123', 'public_api_fallback'),
        ), patch.object(service, '_prepare_public_fallback_card'):
            result = service._load_tweet_card(
                page, 'https://x.com/a/status/123', 'a', '123',
                dark_mode=True, wait_timeout_ms=30000,
            )
        self.assertIs(result[0], card)
        self.assertEqual(result[2], 'public_api_fallback')
        wait.assert_not_called()
        self.assertEqual(page.goto.call_count, 1)


if __name__ == '__main__':
    unittest.main()
