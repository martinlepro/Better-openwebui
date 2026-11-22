from datetime import datetime, timezone

from _load import load

ul = load('utils/usage_limit.py', 'usage_limit')

NOW = datetime(2026, 11, 19, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def test_plain_429_is_rate_limit():
    info = ul.detect_limit({'message': 'Too many requests'}, status=429, now=NOW)
    assert info and info.kind == ul.KIND_RATE_LIMIT
    assert info.reset_at is None


def test_retry_after_header_wins():
    info = ul.detect_limit('rate_limit_error', status=429, headers={'Retry-After': '42'}, now=NOW)
    assert info.source == 'header'
    assert round(info.reset_at - NOW) == 42


def test_retry_after_ms_header():
    info = ul.detect_limit('rate limit', status=429, headers={'retry-after-ms': '15000'}, now=NOW)
    assert round(info.reset_at - NOW) == 15


def test_anthropic_headers_only_use_exhausted_buckets():
    headers = {
        'anthropic-ratelimit-requests-remaining': '10',
        'anthropic-ratelimit-requests-reset': '2026-11-19T18:00:00Z',  # not blocking
        'anthropic-ratelimit-input-tokens-remaining': '0',
        'anthropic-ratelimit-input-tokens-reset': '2026-11-19T10:00:30Z',
    }
    info = ul.detect_limit('rate limit', status=429, headers=headers, now=NOW)
    assert round(info.reset_at - NOW) == 30


def test_api_spend_cap_message_with_iso_date():
    msg = {
        'type': 'error',
        'error': {
            'type': 'invalid_request_error',
            'message': 'You have reached your specified API usage limits. You will regain access on 2026-12-01 at 00:00 UTC.',
        },
    }
    info = ul.detect_limit(msg, status=400, now=NOW)
    assert info.kind == ul.KIND_USAGE_LIMIT
    assert info.source == 'iso'
    assert datetime.fromtimestamp(info.reset_at, tz=timezone.utc) == datetime(2026, 12, 1, tzinfo=timezone.utc)


def test_relative_duration():
    info = ul.detect_limit('Usage limit reached. Try again in 4 hours 23 minutes.', now=NOW)
    assert info.kind == ul.KIND_USAGE_LIMIT
    assert info.source == 'relative'
    assert round(info.reset_at - NOW) == 4 * 3600 + 23 * 60


def test_short_relative_form():
    info = ul.detect_limit('Rate limit exceeded, retry in 2h30m', now=NOW)
    assert round(info.reset_at - NOW) == 2 * 3600 + 30 * 60


def test_claude_code_epoch_format():
    target = int(NOW) + 3 * 3600
    info = ul.detect_limit(f'Claude AI usage limit reached|{target}', now=NOW)
    assert info.source == 'epoch'
    assert round(info.reset_at) == target


def test_clock_time_with_zone_rolls_to_next_day():
    # 10:00 UTC now; "resets 9am (UTC)" already passed -> tomorrow 09:00 UTC.
    info = ul.detect_limit("You've hit your limit · resets 9am (UTC)", now=NOW)
    assert info.kind == ul.KIND_USAGE_LIMIT
    assert info.source == 'clock'
    assert datetime.fromtimestamp(info.reset_at, tz=timezone.utc) == datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


def test_clock_time_today_with_pm():
    info = ul.detect_limit('Limit reached, resets at 5:30 PM (UTC)', now=NOW)
    assert datetime.fromtimestamp(info.reset_at, tz=timezone.utc) == datetime(2026, 11, 19, 17, 30, tzinfo=timezone.utc)


def test_europe_paris_zone():
    info = ul.detect_limit('Usage limit reached, resets at 3pm (Europe/Paris)', now=NOW)
    # 15:00 CET (UTC+1 in November) = 14:00 UTC
    assert datetime.fromtimestamp(info.reset_at, tz=timezone.utc) == datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)


def test_bare_number_clock_is_ignored():
    info = ul.detect_limit('Usage limit reached. See you at 3', now=NOW)
    assert info.kind == ul.KIND_USAGE_LIMIT
    assert info.reset_at is None


def test_overloaded():
    info = ul.detect_limit(
        {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}, status=529, now=NOW
    )
    assert info.kind == ul.KIND_OVERLOADED


def test_credit_balance():
    info = ul.detect_limit('Your credit balance is too low to access the Anthropic API.', status=400, now=NOW)
    assert info.kind == ul.KIND_CREDIT


def test_context_length_is_not_a_limit():
    assert ul.detect_limit('maximum context length is 200000 tokens', status=400, now=NOW) is None
    assert ul.detect_limit('prompt is too long: 250000 tokens > 200000 maximum', status=400, now=NOW) is None


def test_unrelated_errors_are_not_limits():
    assert ul.detect_limit('Invalid API key', status=401, now=NOW) is None
    assert ul.detect_limit('Model not found', status=404, now=NOW) is None
    assert ul.detect_limit(None, now=NOW) is None
    assert ul.detect_limit('', now=NOW) is None


def test_reset_is_clamped():
    past = ul.detect_limit('Rate limit', status=429, headers={'retry-after': '0'}, now=NOW)
    assert past.reset_at - NOW >= ul.MIN_WAIT_SECONDS
    far = ul.detect_limit('Usage limit reached, try again in 900 days', now=NOW)
    assert far.reset_at - NOW <= ul.MAX_WAIT_SECONDS


def test_nested_error_shapes():
    assert ul.detect_limit({'error': {'message': 'rate_limit_error'}}, now=NOW).kind == ul.KIND_RATE_LIMIT
    assert ul.detect_limit([{'content': 'Too many requests'}], now=NOW).kind == ul.KIND_RATE_LIMIT


def test_default_wait_probing():
    # no announced reset: probe soon and then at most every 30 minutes
    assert ul.default_wait_seconds(ul.KIND_USAGE_LIMIT, 0) == 600
    assert ul.default_wait_seconds(ul.KIND_USAGE_LIMIT, 1) == 900
    assert ul.default_wait_seconds(ul.KIND_USAGE_LIMIT, 2) == 1350
    assert ul.default_wait_seconds(ul.KIND_USAGE_LIMIT, 10) == 1800
    assert ul.default_wait_seconds(ul.KIND_USAGE_LIMIT, 10, max_probe=900) == 900
    assert ul.default_wait_seconds(ul.KIND_RATE_LIMIT, 0) == 60
    assert ul.default_wait_seconds(ul.KIND_RATE_LIMIT, 2) == 240
    assert ul.default_wait_seconds(ul.KIND_RATE_LIMIT, 20) == 600
    assert ul.default_wait_seconds(ul.KIND_OVERLOADED, 0) == 30
    assert ul.default_wait_seconds(ul.KIND_CREDIT, 5) == 1800


def test_format_wait():
    assert ul.format_wait(30) == '30s'
    assert ul.format_wait(45 * 60) == '45m'
    assert ul.format_wait(3 * 3600 + 12 * 60) == '3h 12m'
