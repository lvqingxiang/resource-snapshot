import unittest
from unittest.mock import patch
import screenshot_service as service


class PublicQuoteTests(unittest.TestCase):
    def fetch(self, vx, fx_quote=None):
        status = {'text': 'Main', 'views': 123}
        if fx_quote is not None:
            status['quote'] = fx_quote

        def response(url, timeout):
            if 'vxtwitter' in url:
                return vx
            return {'status': status} if '/2/' in url else {'tweet': {'text': 'Main', 'views': 123}}

        with patch.object(service, '_fetch_public_x_json', side_effect=response):
            return service._fetch_public_x_status('123')[0]

    def test_backup_recovers_missing_or_unavailable_quote(self):
        vx = {'text': 'Main', 'qrt': {
            'tweetID': '456', 'text': 'Quoted text', 'user_screen_name': 'author',
            'media_extended': [{'type': 'video', 'url': 'https://video.twimg.com/test.mp4',
                                'thumbnail_url': 'https://pbs.twimg.com/test.jpg'}]}}
        for quote in [None, {'type': 'tombstone', 'id': '456'}]:
            with self.subTest(quote=quote):
                result = self.fetch(vx, quote)
                self.assertEqual(result['views'], 123)
                self.assertEqual(result['quote']['text'], 'Quoted text')
                self.assertEqual(result['quote']['author']['screen_name'], 'author')
                media = service._collect_node_media_items(result['quote'])
                self.assertEqual(media[0]['poster'], 'https://pbs.twimg.com/test.jpg')

    def test_full_quote_survives_empty_backup(self):
        quote = {'text': 'Full quote', 'author': {'screen_name': 'author'}}
        self.assertEqual(self.fetch({'text': 'Main'}, quote)['quote'], quote)

    def test_quote_absence_and_unavailable_quote(self):
        self.assertNotIn('quote', self.fetch({'text': 'Main'}))
        result = self.fetch({'text': 'Main', 'qrtURL': 'https://x.com/i/status/456'})
        self.assertEqual(result['quote']['type'], 'tombstone')

    def test_backup_failure_keeps_primary(self):
        self.assertEqual(self.fetch(None)['views'], 123)


if __name__ == '__main__':
    unittest.main()
