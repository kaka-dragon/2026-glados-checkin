import os
import unittest
from unittest import mock

import checkin


class FakeClient:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    def checkin(self):
        self.calls += 1
        return next(self.results)


class CheckinResultTests(unittest.TestCase):
    def test_current_observation_message_is_normal(self):
        result = {
            'code': 1,
            'message': "Today's observation logged. Return tomorrow for more points.",
        }
        self.assertTrue(checkin.is_normal_checkin_result(result))

    def test_historic_success_message_is_normal(self):
        self.assertTrue(
            checkin.is_normal_checkin_result({'code': 0, 'message': 'Checkin! Got 15 Points'})
        )

    def test_unknown_error_is_failure(self):
        self.assertFalse(checkin.is_normal_checkin_result({'code': 2, 'message': 'Cookie expired'}))

    @mock.patch('checkin.time.sleep')
    def test_retry_stops_after_success(self, sleep):
        client = FakeClient([
            None,
            {'code': 0, 'message': 'Checkin! Got 15 Points'},
        ])

        result, success = checkin.checkin_with_retry(client, attempts=3, delay_seconds=1)

        self.assertTrue(success)
        self.assertEqual(result['code'], 0)
        self.assertEqual(client.calls, 2)
        sleep.assert_called_once_with(1)


class CookieTests(unittest.TestCase):
    def test_json_token_uses_real_cookie_name(self):
        self.assertEqual(checkin.extract_cookie('{"token":"abc"}'), 'koa:sess=abc')

    def test_missing_configuration_returns_no_accounts(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(checkin.get_cookies(), [])


class FakeGLaDOS:
    def __init__(self, points, exchange_response):
        self.points = points
        self.exchange_response = exchange_response
        self.refresh_calls = 0

    def exchange(self, plan):
        self.plan_sent = plan
        return self.exchange_response

    def get_status(self):
        self.refresh_calls += 1

    def get_points(self):
        self.refresh_calls += 1


class ExchangePlanTests(unittest.TestCase):
    def test_defaults_to_plan500(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(checkin.get_exchange_plan(), 'plan500')

    def test_off_disables_exchange(self):
        with mock.patch.dict(os.environ, {'EXCHANGE_PLAN': 'off'}):
            self.assertIsNone(checkin.get_exchange_plan())

    def test_invalid_value_disables_exchange(self):
        with mock.patch.dict(os.environ, {'EXCHANGE_PLAN': 'plan999'}):
            self.assertIsNone(checkin.get_exchange_plan())


class AutoExchangeTests(unittest.TestCase):
    def test_exchanges_and_refreshes_when_points_reach_threshold(self):
        client = FakeGLaDOS('500', {'code': 0, 'message': 'ok'})

        result = checkin.auto_exchange(client, 'plan500')

        self.assertEqual(client.plan_sent, 'plan500')
        self.assertIn('兑换成功', result)
        self.assertIn('+100天', result)
        self.assertEqual(client.refresh_calls, 2)

    def test_skips_when_points_below_threshold(self):
        client = FakeGLaDOS('499', {'code': 0, 'message': 'ok'})

        result = checkin.auto_exchange(client, 'plan500')

        self.assertFalse(hasattr(client, 'plan_sent'))
        self.assertIn('积分不足', result)

    def test_reports_failure_without_refresh(self):
        client = FakeGLaDOS('600', {'code': 1, 'message': 'denied'})

        result = checkin.auto_exchange(client, 'plan500')

        self.assertIn('兑换失败', result)
        self.assertIn('denied', result)
        self.assertEqual(client.refresh_calls, 0)

    def test_unreadable_points_is_reported(self):
        client = FakeGLaDOS('?', {'code': 0, 'message': 'ok'})

        result = checkin.auto_exchange(client, 'plan500')

        self.assertIn('积分查询失败', result)
        self.assertFalse(hasattr(client, 'plan_sent'))


if __name__ == '__main__':
    unittest.main()
