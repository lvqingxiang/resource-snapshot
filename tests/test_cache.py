import unittest
from unittest.mock import patch

from snapshot.cache import ttl_cache
from snapshot import public_data, translation


class CacheTests(unittest.TestCase):
    def test_expiry_capacity_and_mutation_isolation(self):
        calls = []

        @ttl_cache(ttl=10, maxsize=2)
        def fetch(key):
            calls.append(key)
            return {'items': [key]}

        with patch('snapshot.cache.monotonic', return_value=0):
            fetch('a')['items'].append('mutated')
            self.assertEqual(fetch('a'), {'items': ['a']})
            fetch('b')
            fetch('a')  # Keep a as the most recently used entry.
            fetch('c')
            fetch('b')  # b was evicted.
        self.assertEqual(calls, ['a', 'b', 'c', 'b'])
        with patch('snapshot.cache.monotonic', return_value=11):
            fetch('b')
        self.assertEqual(calls[-2:], ['b', 'b'])

    def test_failures_are_retried(self):
        calls = []

        @ttl_cache(ttl=10)
        def fetch():
            calls.append(1)
            return None if len(calls) == 1 else 'recovered'

        self.assertIsNone(fetch())
        self.assertEqual(fetch(), 'recovered')
        self.assertEqual(fetch(), 'recovered')
        self.assertEqual(len(calls), 2)

    def test_public_data_reuse_avoids_all_mirror_requests(self):
        public_data._fetch_public_x_status.cache_clear()
        self.addCleanup(public_data._fetch_public_x_status.cache_clear)
        with patch.object(public_data, '_fetch_public_x_json', return_value={
            'status': {'text': 'Cached post'},
            'tweet': {'text': 'Cached post'},
        }) as network:
            first = public_data._fetch_public_x_status('123')
            initial_calls = network.call_count
            first[0]['text'] = 'Local edit'
            second = public_data._fetch_public_x_status('123')
        self.assertEqual(initial_calls, 3)
        self.assertEqual(network.call_count, initial_calls)
        self.assertEqual(second[0]['text'], 'Cached post')

    def test_translation_success_is_reused_but_failure_is_not(self):
        translation._translate_text_to_chinese.cache_clear()
        self.addCleanup(translation._translate_text_to_chinese.cache_clear)
        with patch.object(translation, '_translate_text_to_chinese_via_google',
                          side_effect=[None, '你好']) as google, patch.object(
            translation, '_translate_text_to_chinese_via_mymemory', return_value=None
        ):
            self.assertIsNone(translation._translate_text_to_chinese('Hello', 'en'))
            self.assertEqual(translation._translate_text_to_chinese('Hello', 'en'), '你好')
            self.assertEqual(translation._translate_text_to_chinese('Hello', 'en'), '你好')
        self.assertEqual(google.call_count, 2)

    def test_oembed_success_is_reused(self):
        translation._fetch_oembed_tweet_body.cache_clear()
        self.addCleanup(translation._fetch_oembed_tweet_body.cache_clear)
        with patch.object(translation, '_fetch_translation_payload',
                          return_value={'html': '<p lang="en">Hello world</p>'}) as network:
            for _ in range(2):
                self.assertEqual(translation._fetch_oembed_tweet_body('https://x.com/a/status/123'),
                                 ('Hello world', 'en'))
        network.assert_called_once()


if __name__ == '__main__':
    unittest.main()
