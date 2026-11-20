import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
import checkin

COOKIE = 'gld:sess=fake; gld:sess.sig=fake'
FIXED_NOW = datetime(2026, 11, 14, 16, 0, 0, tzinfo=timezone(timedelta(hours=8)))
TODAY_MS = int(datetime(2026, 11, 14, 9, 30, 0, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


class PointsHistoryTests(unittest.TestCase):
    def test_history_timestamp_is_explicit_and_resets_when_history_empty(self):
        client = checkin.GLaDOS(COOKIE)
        client.req = mock.Mock(return_value={'points': '15', 'history': [
            {'change': '14.0', 'time': 1794499249195}], 'plans': {}})
        self.assertTrue(client.get_points())
        self.assertEqual(client.points_change, '+14')
        self.assertIn('2026-11-13 00:00:49', client.points_change_time)
        client.req.return_value['history'] = []
        client.get_points()
        self.assertEqual(client.points_change, '?')

    def test_decimal_points_and_changes_are_not_truncated(self):
        client = checkin.GLaDOS(COOKIE)
        client.req = mock.Mock(return_value={
            'points': '15.5',
            'history': [{'change': '1.5', 'time': TODAY_MS}],
            'plans': {'plan100': {'points': 100, 'days': 10}},
        })
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW):
            self.assertTrue(client.get_points())
        self.assertEqual(client.points, '15.5')
        self.assertEqual(client.points_change, '+1.5')
        self.assertIn('差84.5分', client.exchange_info)

    def test_invalid_time_is_not_echoed(self):
        for value in ['secret', float('inf'), True, 10**18, None]:
            client = checkin.GLaDOS(COOKIE)
            client.req = mock.Mock(return_value={'points': '15', 'history': [
                {'change': '14', 'time': value}], 'plans': {}})
            client.get_points()
            self.assertEqual(client.points_change_time, '未知')

    def test_failure_and_repeat_notification_do_not_present_old_gain_as_current(self):
        for result, expected_exit in [
            ({'code': 5, 'reason': 'turnstile'}, 1),
            ({'code': 1, 'message': "Today's observation logged."}, 0),
            ({'code': 0, 'message': 'Checkin! Got 14 Points'}, 0),
        ]:
            client = checkin.GLaDOS(COOKIE)
            client.checkin = mock.Mock(return_value=result)
            client.get_status = mock.Mock(return_value=True)
            client.req = mock.Mock(return_value={'points': '15', 'history': [
                {'change': '14', 'time': 1794499249195}], 'plans': {}})
            with mock.patch('checkin.beijing_now', return_value=FIXED_NOW), \
                    mock.patch.dict(os.environ, {'GLADOS_COOKIE': COOKIE,
                    'EXCHANGE_PLAN': 'off', 'CHECKIN_MAX_ATTEMPTS': '1',
                    'GLADOS_BROWSER_CHECKIN': '0',
                    'PUSHPLUS_TOKEN': 'fake-token', 'PUSH_LEVEL': 'always'}, clear=True), \
                    mock.patch('checkin.GLaDOS', return_value=client), \
                    mock.patch('checkin.pushplus') as push, \
                    mock.patch('checkin.log'), \
                    mock.patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
                self.assertEqual(checkin.main(), expected_exit)
                content = push.call_args.args[2]
                self.assertIn('历史记录，不代表本次新增', content)
                self.assertIn('2026-11-13 00:00:49', content)
                self.assertNotIn('(+14)', content)
                self.assertNotIn('fake-token', content)

    @mock.patch('checkin.time.sleep')
    def test_turnstile_stops_retries_immediately_and_skips_auto_exchange(self, sleep):
        client = checkin.GLaDOS(COOKIE)
        client.checkin = mock.Mock(return_value={
            'code': 5,
            'reason': 'turnstile',
            'message': 'Cloudflare verification failed. Nothing was recorded — please retry.',
        })
        client.get_status = mock.Mock(return_value=True)
        client.req = mock.Mock(return_value={
            'points': '520',
            'history': [{'change': '14', 'time': 1794499249195}],
            'plans': {},
        })
        client.exchange = mock.Mock()
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW), \
                mock.patch.dict(os.environ, {
                    'GLADOS_COOKIE': COOKIE,
                    'EXCHANGE_PLAN': 'plan500',
                    'CHECKIN_MAX_ATTEMPTS': '3',
                    'GLADOS_BROWSER_CHECKIN': '0',
                    'PUSHPLUS_TOKEN': 'fake-token',
                    'PUSH_LEVEL': 'fail_only',
                }, clear=True), \
                mock.patch('checkin.GLaDOS', return_value=client), \
                mock.patch('checkin.pushplus') as push, \
                mock.patch('checkin.log'):
            self.assertEqual(checkin.main(), 1)

        client.checkin.assert_called_once()
        sleep.assert_not_called()
        client.exchange.assert_not_called()
        content = push.call_args.args[2]
        self.assertIn('reason=turnstile', content)
        self.assertIn('未确认（最终请求未成功；本轮是否入账未知，请核对完整流水）', content)
        self.assertIn('本次签到未成功，跳过兑换', content)

    def test_cross_check_distinguishes_matched_capped_mismatched_and_repeat_gains(self):
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW):
            # 1. Exact match between checkin response and today's ledger
            client = checkin.GLaDOS(COOKIE)
            client.req = mock.Mock(return_value={
                'points': '15',
                'history': [{'change': '14.0', 'time': TODAY_MS}],
                'plans': {},
            })
            client.get_points()
            summary = checkin.summarize_current_run_gain(
                {'code': 0, 'message': 'Checkin! Got 14 Points', 'points': 14}, True, client,
            )
            self.assertIn('接口声称 +14 分（本次入账归属未确认）', summary)
            self.assertIn('未关联本次请求', summary)

            # 2. Breakdown sum (+2.7) capped at +2 (Free cap max 2)
            client.req.return_value = {
                'points': '2',
                'history': [{'change': '2', 'time': TODAY_MS}],
                'plans': {},
            }
            client.get_points()
            summary = checkin.summarize_current_run_gain(
                {'code': 0, 'message': 'Checkin! Got 2 Points', 'points': 2, 'breakdown': {'base': 2.7}},
                True, client,
            )
            self.assertIn('+2 分', summary)
            self.assertIn('分项合计 +2.7 分', summary)

            # 3. Declared points mismatch actual ledger entry
            summary = checkin.summarize_current_run_gain(
                {'code': 0, 'message': 'Checkin! Got 7 Points', 'points': 7}, True, client,
            )
            self.assertIn('一笔正变动 +2 分', summary)
            self.assertIn('接口声称 +7 分', summary)
            self.assertIn('本次入账归属未确认', summary)

            # 4. Repeat check-in after manual browser check-in earlier today
            client.req.return_value = {
                'points': '155',
                'history': [{'change': '12', 'time': TODAY_MS}],
                'plans': {},
            }
            client.get_points()
            summary = checkin.summarize_current_run_gain(
                {'code': 1, 'message': "Today's observation logged. Return tomorrow for more points."},
                True, client,
            )
            self.assertIn('不能据此确定本轮新增积分', summary)
            self.assertIn('一笔正变动 +12 分', summary)

    def test_pre_exchange_checkin_gain_is_preserved_after_auto_exchange_deduction(self):
        client = checkin.GLaDOS(COOKIE)
        client.checkin = mock.Mock(return_value={'code': 0, 'message': 'Checkin! Got 11 Points'})
        client.get_status = mock.Mock(return_value=True)
        client.exchange = mock.Mock(return_value={'code': 0, 'message': 'ok'})
        client.req = mock.Mock(side_effect=[
            {'points': '501', 'history': [{'change': '11', 'time': TODAY_MS}], 'plans': {}},
            {'points': '1', 'history': [
                {'change': '-500', 'time': TODAY_MS + 1000, 'business': 'exchange'},
                {'change': '11', 'time': TODAY_MS, 'business': 'checkin'},
            ], 'plans': {}},
        ])
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW), \
                mock.patch.dict(os.environ, {
                    'GLADOS_COOKIE': COOKIE,
                    'EXCHANGE_PLAN': 'plan500',
                    'CHECKIN_MAX_ATTEMPTS': '1',
                    'PUSHPLUS_TOKEN': 'fake-token',
                    'PUSH_LEVEL': 'fail_only',
                }, clear=True), \
                mock.patch('checkin.GLaDOS', return_value=client), \
                mock.patch('checkin.pushplus') as push, \
                mock.patch('checkin.log'):
            self.assertEqual(checkin.main(), 0)

        content = push.call_args.args[2]
        self.assertIn('接口声称 +11 分（本次入账归属未确认）', content)
        self.assertIn('兑换成功 +100天 (消耗500分)', content)
        self.assertIn('-500（', content)

    def test_notify_status_only_pushes_readonly_card_without_checkin_or_exchange(self):
        client = checkin.GLaDOS(COOKIE)
        client.checkin = mock.Mock(side_effect=AssertionError('checkin must not be called in notify_status_only'))
        client.exchange = mock.Mock(side_effect=AssertionError('exchange must not be called in notify_status_only'))
        client.req = mock.Mock(side_effect=[
            {'code': 0, 'data': {'email': 'readonly@example.test', 'leftDays': '688.0'}},
            {
                'points': '15',
                'history': [
                    {'change': '-500', 'time': TODAY_MS + 5000, 'business': 'exchange'},
                    {'change': '14.0', 'time': 1794499249195, 'business': 'checkin'},
                ],
                'plans': {'plan100': {'points': 100, 'days': 10}},
            },
        ])
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW), \
                mock.patch.dict(os.environ, {
                    'GLADOS_COOKIE': COOKIE,
                    'CHECKIN_NOTIFY_STATUS_ONLY': 'true',
                    'EXCHANGE_PLAN': 'plan500',
                    'PUSH_LEVEL': 'fail_only',
                    'PUSHPLUS_TOKEN': 'fake-token',
                }, clear=True), \
                mock.patch('checkin.GLaDOS', return_value=client), \
                mock.patch('checkin.pushplus', return_value=True) as push, \
                mock.patch('checkin.log') as log_mock, \
                mock.patch('requests.sessions.Session.request', side_effect=AssertionError('No network')):
            self.assertEqual(checkin.main(), 0)

        client.checkin.assert_not_called()
        client.exchange.assert_not_called()
        self.assertEqual(client.req.call_count, 2)
        push.assert_called_once()
        title = push.call_args.args[1]
        content = push.call_args.args[2]
        self.assertEqual(title, 'GLaDOS 今日未签到｜剩 688 天 · 15 分')
        self.assertIn('readonly@example.test', content)
        self.assertIn('⏳ 未签到', content)
        self.assertIn('2026-11-13 00:00:49', content)
        self.assertNotIn('2026-11-13 00:00:49 北京时间', content)
        self.assertIn('688 天', content)
        self.assertIn('15 分', content)
        self.assertIn('https://glados.network/console/checkin', content)
        self.assertIn('近期积分流水：', content)
        self.assertIn('推送时间：2026-11-14 16:00:00', content)
        for noisy in (
            '只读状态与积分核对',
            '不会触发 Turnstile 验证',
            '完整性未确认',
            '流水观察',
            '历史记录，不代表本次新增',
            '跳过自动兑换',
            '兑换选项',
            '运行模式',
        ):
            self.assertNotIn(noisy, content)
        self.assertNotIn('readonly@example.test', str(log_mock.call_args_list))

    def test_notify_status_only_returns_nonzero_when_no_push_channel_configured(self):
        client = checkin.GLaDOS(COOKIE)
        client.checkin = mock.Mock(side_effect=AssertionError('checkin must not be called'))
        client.req = mock.Mock(side_effect=[
            {'code': 0, 'data': {'email': 'readonly@example.test', 'leftDays': '688.0'}},
            {'points': '15', 'history': [], 'plans': {}},
        ])
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW), \
                mock.patch.dict(os.environ, {
                    'GLADOS_COOKIE': COOKIE,
                    'CHECKIN_NOTIFY_STATUS_ONLY': 'true',
                }, clear=True), \
                mock.patch('checkin.GLaDOS', return_value=client), \
                mock.patch('checkin.log'):
            self.assertEqual(checkin.main(), 1)

    def test_notify_status_only_serverchan_formatting_and_titles(self):
        yesterday_ms = int(datetime(2026, 11, 13, 8, 15, 20, tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        cases = [
            (
                'not_checked_in',
                {'code': 0, 'data': {'email': 'readonly@example.test', 'leftDays': '185.0'}},
                {'points': '148', 'history': [{'change': '2', 'time': yesterday_ms, 'business': 'checkin'}], 'plans': {}},
                'GLaDOS 今日未签到｜剩 185 天 · 148 分',
                '⏳ 未签到',
                '2026-11-13 08:15:20',
                0,
            ),
            (
                'checked_in',
                {'code': 0, 'data': {'email': 'readonly@example.test', 'leftDays': '185.0'}},
                {'points': '150', 'history': [{'change': '2', 'time': TODAY_MS, 'business': 'checkin'}], 'plans': {}},
                'GLaDOS 今日已签到｜剩 185 天 · 150 分',
                '✅ 已签到（+2 分）',
                '2026-11-14 09:30:00',
                0,
            ),
            (
                'failed',
                {'request_failed': True, 'reason': 'request-failed'},
                None,
                'GLaDOS 状态查询失败',
                '❌ 查询失败（登录状态读取失败）',
                '未知',
                1,
            ),
        ]
        for label, status_payload, points_payload, expected_title, expected_status, expected_last_time, expected_rc in cases:
            with self.subTest(case=label):
                client = checkin.GLaDOS(COOKIE)
                client.domain = 'https://glados.cloud'
                client.checkin = mock.Mock(side_effect=AssertionError('checkin must not be called'))
                client.exchange = mock.Mock(side_effect=AssertionError('exchange must not be called'))
                responses = [status_payload] if points_payload is None else [status_payload, points_payload]
                client.req = mock.Mock(side_effect=responses)
                post_resp = mock.Mock()
                post_resp.json.return_value = {'code': 0, 'data': {'pushid': 'synthetic'}}
                with mock.patch('checkin.beijing_now', return_value=FIXED_NOW), \
                        mock.patch.dict(os.environ, {
                            'GLADOS_COOKIE': COOKIE,
                            'CHECKIN_NOTIFY_STATUS_ONLY': 'true',
                            'SERVERCHAN_SENDKEY': 'SCT123synthetic-sendkey',
                        }, clear=True), \
                        mock.patch('checkin.GLaDOS', return_value=client), \
                        mock.patch('checkin.requests.post', return_value=post_resp) as post, \
                        mock.patch('checkin.log'):
                    self.assertEqual(checkin.main(), expected_rc)
                sent = post.call_args.kwargs['json']
                self.assertEqual(sent['title'], expected_title)
                body = sent['desp']
                self.assertIn(expected_status, body)
                self.assertIn(f'最后签到：{expected_last_time}', body)
                self.assertIn('签到链接：https://glados.network/console/checkin', body)
                self.assertIn('推送时间：2026-11-14 16:00:00', body)
                self.assertNotIn('<', body)
                self.assertNotIn('>', body)
                blocks = body.split('\n\n')
                self.assertIn('账号：', blocks[0])
                self.assertIn('签到链接：https://glados.network/console/checkin', blocks[0])
                self.assertIn('  \n', blocks[0])
                if points_payload and points_payload['history']:
                    self.assertEqual(len(blocks), 3)
                    self.assertTrue(blocks[1].startswith('近期积分流水：'))
                    self.assertIn(f'{expected_last_time}  +2  checkin', blocks[1])
                    self.assertEqual(blocks[2], '推送时间：2026-11-14 16:00:00')
                else:
                    self.assertEqual(len(blocks), 2)
                    self.assertEqual(blocks[1], '推送时间：2026-11-14 16:00:00')

    def test_notify_status_only_multi_account_summary_title_and_recent_entries_cap(self):
        history_eight = [
            {'change': '2', 'time': TODAY_MS - i * 86400 * 1000, 'business': 'checkin'}
            for i in range(8)
        ]
        c1 = checkin.GLaDOS(COOKIE)
        c1.req = mock.Mock(side_effect=[
            {'code': 0, 'data': {'email': 'a@example.test', 'leftDays': '100.0'}},
            {'points': '200', 'history': history_eight, 'plans': {}},
        ])
        c2 = checkin.GLaDOS(COOKIE)
        c2.req = mock.Mock(side_effect=[
            {'code': 0, 'data': {'email': 'b@example.test', 'leftDays': '50.0'}},
            {'points': '80', 'history': [], 'plans': {}},
        ])
        post_resp = mock.Mock()
        post_resp.json.return_value = {'code': 0, 'data': {'pushid': 'synthetic'}}
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW), \
                mock.patch.dict(os.environ, {
                    'GLADOS_COOKIE': COOKIE + '&' + COOKIE,
                    'CHECKIN_NOTIFY_STATUS_ONLY': 'true',
                    'SERVERCHAN_SENDKEY': 'SCT123synthetic-sendkey',
                }, clear=True), \
                mock.patch('checkin.GLaDOS', side_effect=[c1, c2]), \
                mock.patch('checkin.requests.post', return_value=post_resp) as post, \
                mock.patch('checkin.log'):
            self.assertEqual(checkin.main(), 0)
        sent = post.call_args.kwargs['json']
        self.assertEqual(sent['title'], 'GLaDOS 状态：已签 1 · 未签 1')
        body = sent['desp']
        self.assertIn('a@example.test', body)
        self.assertIn('b@example.test', body)
        # Recent entries retain HH:MM:SS and cap display to 6 rows
        self.assertEqual(len(c1.recent_entries), 8)
        self.assertEqual(c1.recent_entries[0]['time'], '2026-11-14 09:30:00')
        self.assertIn('2026-11-09 09:30:00  +2  checkin', body)
        self.assertNotIn('2026-11-08 09:30:00', body)

    def test_last_checkin_time_prefers_checkin_over_exchange_and_falls_back(self):
        client = checkin.GLaDOS(COOKIE)
        exchange_ms = TODAY_MS + 5000
        client.req = mock.Mock(return_value={
            'points': '50',
            'history': [
                {'change': '-500', 'time': exchange_ms, 'business': 'exchange'},
                {'change': '2', 'time': TODAY_MS, 'business': 'checkin'},
            ],
            'plans': {},
        })
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW):
            self.assertTrue(client.get_points())
        self.assertEqual(client.last_checkin_time, '2026-11-14 09:30:00')

        # Fallback to latest valid ledger timestamp when only exchange/refund exists
        client.req = mock.Mock(return_value={
            'points': '50',
            'history': [{'change': '-500', 'time': exchange_ms, 'business': 'exchange'}],
            'plans': {},
        })
        with mock.patch('checkin.beijing_now', return_value=FIXED_NOW):
            self.assertTrue(client.get_points())
        self.assertEqual(client.last_checkin_time, '2026-11-14 09:30:05')
