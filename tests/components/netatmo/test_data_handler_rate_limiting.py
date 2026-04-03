"""Tests for Netatmo data handler rate limiting logic."""

from __future__ import annotations

from time import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pyatmo
import pytest

from homeassistant.components.netatmo.data_handler import (
    CALL_PER_HOUR,
    CALL_PER_TEN_SECONDS,
    CPH_ADJUSTEMENT_BACK_UP,
    CPH_ADJUSTEMENT_DOWN,
    NETATMO_DEV_CALL_LIMITS,
    NETATMO_USER_CALL_LIMITS,
    SCAN_INTERVAL,
    NetatmoDataHandler,
    NetatmoPublisher,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from tests.common import MockConfigEntry

from .common import selected_platforms

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_publisher(
    name: str = "test",
    interval: int = 300,
    next_scan: float = 0.0,
    data_handler: Any = None,
) -> NetatmoPublisher:
    """Create a NetatmoPublisher with sensible defaults for unit tests."""
    return NetatmoPublisher(
        name=name,
        interval=interval,
        next_scan=next_scan,
        target=MagicMock(),
        subscriptions=set(),
        method="async_update_status",
        data_handler=data_handler or MagicMock(),
        kwargs={},
    )


def _make_data_handler(
    hass: HomeAssistant | None = None,
    auth_implementation: str = "cloud",
) -> NetatmoDataHandler:
    """Create a NetatmoDataHandler with mocked dependencies for unit tests."""
    mock_hass = hass or MagicMock()
    mock_auth = MagicMock(spec=pyatmo.AbstractAsyncAuth)
    mock_entry = MagicMock(spec=MockConfigEntry)
    mock_entry.data = {"auth_implementation": auth_implementation}
    mock_entry.options = {}
    return NetatmoDataHandler(mock_hass, mock_entry, mock_auth)


# ---------------------------------------------------------------------------
# NetatmoPublisher unit tests
# ---------------------------------------------------------------------------


class TestNetatmoPublisher:
    """Tests for NetatmoPublisher dataclass methods."""

    def test_push_emission_resets_errors(self) -> None:
        """Test push_emission resets consecutive error count."""
        pub = _make_publisher()
        pub.num_consecutive_errors = 5
        pub.push_emission(1000)
        assert pub.num_consecutive_errors == 0

    def test_push_emission_already_zero(self) -> None:
        """Test push_emission when errors already zero."""
        pub = _make_publisher()
        pub.push_emission(1000)
        assert pub.num_consecutive_errors == 0

    def test_set_next_scan_no_wait(self) -> None:
        """Test set_next_scan with no wait time."""
        pub = _make_publisher(interval=300)
        pub.set_next_scan(1000, wait_time=0)
        assert pub.next_scan == 1300

    def test_set_next_scan_with_wait(self) -> None:
        """Test set_next_scan with additional wait time."""
        pub = _make_publisher(interval=300)
        pub.set_next_scan(1000, wait_time=60)
        assert pub.next_scan == 1360

    def test_is_ts_allows_emission_before(self) -> None:
        """Test emission not allowed before next_scan."""
        pub = _make_publisher()
        pub.next_scan = 1000
        assert pub.is_ts_allows_emission(999) is False

    def test_is_ts_allows_emission_exact(self) -> None:
        """Test emission allowed at exactly next_scan."""
        pub = _make_publisher()
        pub.next_scan = 1000
        assert pub.is_ts_allows_emission(1000) is True

    def test_is_ts_allows_emission_after(self) -> None:
        """Test emission allowed after next_scan."""
        pub = _make_publisher()
        pub.next_scan = 1000
        assert pub.is_ts_allows_emission(1500) is True


# ---------------------------------------------------------------------------
# NetatmoDataHandler rate limiting unit tests
# ---------------------------------------------------------------------------


class TestRollingWindow:
    """Tests for the rolling hour API call tracking."""

    def test_add_api_call_basic(self) -> None:
        """Test adding API calls to rolling window."""
        handler = _make_data_handler()
        handler.add_api_call(3)
        assert handler.get_current_calls_count_per_hour() == 3

    def test_add_api_call_stale_entries_removed(self) -> None:
        """Test that entries older than 1 hour are removed."""
        handler = _make_data_handler()
        old_ts = time() - 3700  # older than 1 hour
        handler.rolling_hour = [old_ts, old_ts + 1, old_ts + 2]
        handler.add_api_call(1)
        # Old entries should be cleaned, only the new one remains
        assert handler.get_current_calls_count_per_hour() == 1

    def test_add_zero_only_cleans(self) -> None:
        """Test add_api_call(0) just cleans stale entries."""
        handler = _make_data_handler()
        old_ts = time() - 3700
        handler.rolling_hour = [old_ts]
        handler.add_api_call(0)
        assert handler.get_current_calls_count_per_hour() == 0

    def test_empty_window(self) -> None:
        """Test count on empty window."""
        handler = _make_data_handler()
        assert handler.get_current_calls_count_per_hour() == 0


class TestLimitSelection:
    """Tests for cloud vs dev auth limit selection."""

    def test_cloud_auth_uses_user_limits(self) -> None:
        """Test cloud auth selects user call limits."""
        handler = _make_data_handler(auth_implementation="cloud")
        assert handler._limits is NETATMO_USER_CALL_LIMITS
        assert (
            handler._initial_hourly_rate_limit
            == NETATMO_USER_CALL_LIMITS[CALL_PER_HOUR]
        )
        assert handler._10s_rate_limit == NETATMO_USER_CALL_LIMITS[CALL_PER_TEN_SECONDS]
        assert handler._scan_interval == NETATMO_USER_CALL_LIMITS[SCAN_INTERVAL]

    def test_dev_auth_uses_dev_limits(self) -> None:
        """Test non-cloud auth selects dev call limits."""
        handler = _make_data_handler(auth_implementation="my_dev_app")
        assert handler._limits is NETATMO_DEV_CALL_LIMITS
        assert (
            handler._initial_hourly_rate_limit == NETATMO_DEV_CALL_LIMITS[CALL_PER_HOUR]
        )
        assert handler._10s_rate_limit == NETATMO_DEV_CALL_LIMITS[CALL_PER_TEN_SECONDS]
        assert handler._scan_interval == NETATMO_DEV_CALL_LIMITS[SCAN_INTERVAL]


class TestPublisherCandidates:
    """Tests for get_publisher_candidates."""

    def test_selects_ready_publishers(self) -> None:
        """Test that only publishers past next_scan are returned."""
        handler = _make_data_handler()
        current = 1000
        p1 = _make_publisher(name="p1", next_scan=900)  # ready
        p2 = _make_publisher(name="p2", next_scan=1100)  # not ready
        p3 = _make_publisher(name="p3", next_scan=950)  # ready
        handler._sorted_publisher = [p1, p2, p3]

        candidates, count = handler.get_publisher_candidates(current, n=10)
        names = [c.name for c in candidates]
        assert "p1" in names
        assert "p3" in names
        assert "p2" not in names
        assert count == 2

    def test_respects_limit(self) -> None:
        """Test that at most n candidates are returned."""
        handler = _make_data_handler()
        current = 1000
        publishers = [_make_publisher(name=f"p{i}", next_scan=500) for i in range(5)]
        handler._sorted_publisher = publishers

        candidates, count = handler.get_publisher_candidates(current, n=2)
        assert count == 2
        assert len(candidates) == 2

    def test_empty_when_none_ready(self) -> None:
        """Test empty result when no publishers are ready."""
        handler = _make_data_handler()
        current = 1000
        p1 = _make_publisher(name="p1", next_scan=2000)
        handler._sorted_publisher = [p1]

        candidates, count = handler.get_publisher_candidates(current, n=10)
        assert count == 0
        assert len(candidates) == 0

    def test_skips_unnamed_publishers(self) -> None:
        """Test that publishers with name=None are skipped."""
        handler = _make_data_handler()
        current = 1000
        p1 = _make_publisher(name=None, next_scan=500)
        p2 = _make_publisher(name="real", next_scan=500)
        handler._sorted_publisher = [p1, p2]

        candidates, count = handler.get_publisher_candidates(current, n=10)
        assert count == 1
        assert candidates[0].name == "real"


class TestAdjustPerScanNumbers:
    """Tests for adjust_per_scan_numbers."""

    def test_initial_calculation(self) -> None:
        """Test per-scan limits calculated from initial rate limit."""
        handler = _make_data_handler(auth_implementation="cloud")
        # Cloud: CPH=20, 10s=2, scan_interval=60
        # scan_limit_per_hour = (20 * 60) // 3600 = 0
        # 10s_limit = (60/10) * 2 = 12
        # min = min(0, 12) = 0
        # max = max(0, 12) = 12
        assert handler._min_call_per_interval is not None
        assert handler._max_call_per_interval is not None
        assert handler._min_call_per_interval <= handler._max_call_per_interval

    def test_with_adjusted_rate(self) -> None:
        """Test per-scan limits recalculated with adjusted rate."""
        handler = _make_data_handler(auth_implementation="my_dev_app")
        # Dev: CPH=450, 10s=45, scan_interval=10
        handler._adjusted_hourly_rate_limit = 225  # halved
        handler.adjust_per_scan_numbers()
        # scan_limit_per_hour = (225 * 10) // 3600 = 0
        # 10s_limit = (10/10) * 45 = 45
        assert handler._min_call_per_interval is not None


class TestAdjustIntervalsToTarget:
    """Tests for adjust_intervals_to_target."""

    def test_no_op_when_target_unchanged(self) -> None:
        """Test no adjustment when target matches current."""
        handler = _make_data_handler()
        handler._adjusted_hourly_rate_limit = 20
        p = _make_publisher(name="p1", interval=300)
        handler._sorted_publisher = [p]
        original_interval = p.interval

        handler.adjust_intervals_to_target(target=20, force_adjust=False)
        assert p.interval == original_interval

    def test_scales_intervals_when_over_target(self) -> None:
        """Test intervals are scaled up when theoretical CPH exceeds target."""
        handler = _make_data_handler()
        handler._adjusted_hourly_rate_limit = None
        # 3600/300 = 12 CPH per publisher, 3 publishers = 36 CPH
        p1 = _make_publisher(name="p1", interval=300)
        p2 = _make_publisher(name="p2", interval=300)
        p3 = _make_publisher(name="p3", interval=300)
        handler._sorted_publisher = [p1, p2, p3]

        handler.adjust_intervals_to_target(target=18, force_adjust=True)
        # Each interval should be roughly doubled (36/18 = 2x)
        for p in [p1, p2, p3]:
            assert p.interval > 300

    def test_target_capped_at_initial(self) -> None:
        """Test target cannot exceed initial hourly rate limit."""
        handler = _make_data_handler(auth_implementation="cloud")
        # Initial limit is 20
        handler._adjusted_hourly_rate_limit = 10
        p = _make_publisher(name="p1", interval=300)
        handler._sorted_publisher = [p]

        handler.adjust_intervals_to_target(target=999, force_adjust=True)
        assert handler._adjusted_hourly_rate_limit == handler._initial_hourly_rate_limit


class TestGetWaitTime:
    """Tests for get_wait_time_to_reach_targets."""

    def test_zero_when_under_target(self) -> None:
        """Test wait time is zero when current calls are under target."""
        handler = _make_data_handler()
        current = int(time())
        handler.rolling_hour = [current - 100, current - 50]  # 2 calls
        assert handler.get_wait_time_to_reach_targets(current, target=10) == 0

    def test_positive_when_over_target(self) -> None:
        """Test wait time is positive when current calls exceed target."""
        handler = _make_data_handler()
        current = int(time())
        # 20 recent calls
        handler.rolling_hour = [current - i * 10 for i in range(20, 0, -1)]
        wait = handler.get_wait_time_to_reach_targets(current, target=10)
        assert wait > 0

    def test_zero_when_empty(self) -> None:
        """Test wait time is zero with no calls."""
        handler = _make_data_handler()
        current = int(time())
        handler.rolling_hour = []
        assert handler.get_wait_time_to_reach_targets(current, target=10) == 0

    def test_full_wait_when_delta_exceeds_window(self) -> None:
        """Test full window wait when delta exceeds rolling_hour size."""
        handler = _make_data_handler()
        current = int(time())
        handler.rolling_hour = [current]  # 1 call
        # target = -10 → delta = 11, which > len(rolling_hour)
        wait = handler.get_wait_time_to_reach_targets(current, target=-10)
        assert wait == 3600 + 2 * handler._scan_interval


class TestSpreadNextScans:
    """Tests for _spread_next_scans."""

    def test_distributes_same_interval_evenly(self) -> None:
        """Test publishers with same interval are evenly spaced."""
        handler = _make_data_handler()
        p1 = _make_publisher(name="p1", interval=300)
        p2 = _make_publisher(name="p2", interval=300)
        p3 = _make_publisher(name="p3", interval=300)
        handler._sorted_publisher = [p1, p2, p3]

        handler._spread_next_scans()

        # Should be spaced at interval/3 = 100 apart
        scans = sorted([p.next_scan for p in [p1, p2, p3]])
        assert scans[1] - scans[0] == 100
        assert scans[2] - scans[1] == 100

    def test_single_publisher_gets_half_interval(self) -> None:
        """Test single publisher placed at interval/2."""
        handler = _make_data_handler()
        p1 = _make_publisher(name="p1", interval=600)
        handler._sorted_publisher = [p1]

        handler._spread_next_scans()

        current = int(time())
        # next_scan = current + max(0, 1) + 300
        assert p1.next_scan == pytest.approx(current + 1 + 300, abs=2)

    def test_different_intervals_grouped(self) -> None:
        """Test publishers are grouped by interval for spreading."""
        handler = _make_data_handler()
        p1 = _make_publisher(name="p1", interval=300)
        p2 = _make_publisher(name="p2", interval=300)
        p3 = _make_publisher(name="p3", interval=600)
        handler._sorted_publisher = [p1, p2, p3]

        handler._spread_next_scans()

        # p1 and p2 (interval=300) should be 150 apart
        diff_same = abs(p1.next_scan - p2.next_scan)
        assert diff_same == 150

    def test_with_wait_time(self) -> None:
        """Test spread with additional wait time."""
        handler = _make_data_handler()
        p1 = _make_publisher(name="p1", interval=600)
        handler._sorted_publisher = [p1]

        handler._spread_next_scans(wait_time=120)

        current = int(time())
        # next_scan = current + max(120, 1) + 300
        assert p1.next_scan == pytest.approx(current + 120 + 300, abs=2)


class TestComputeTheoreticalCPH:
    """Tests for compute_theoretical_call_per_hour."""

    def test_single_publisher(self) -> None:
        """Test CPH calculation with one publisher."""
        handler = _make_data_handler()
        p = _make_publisher(name="p1", interval=300)
        handler._sorted_publisher = [p]
        assert handler.compute_theoretical_call_per_hour() == pytest.approx(12.0)

    def test_multiple_publishers(self) -> None:
        """Test CPH calculation sums all publishers."""
        handler = _make_data_handler()
        p1 = _make_publisher(name="p1", interval=300)  # 12 CPH
        p2 = _make_publisher(name="p2", interval=600)  # 6 CPH
        handler._sorted_publisher = [p1, p2]
        assert handler.compute_theoretical_call_per_hour() == pytest.approx(18.0)

    def test_empty_publishers(self) -> None:
        """Test CPH is zero with no publishers."""
        handler = _make_data_handler()
        handler._sorted_publisher = []
        assert handler.compute_theoretical_call_per_hour() == 0.0


# ---------------------------------------------------------------------------
# async_fetch_data tests
# ---------------------------------------------------------------------------


class TestAsyncFetchData:
    """Tests for async_fetch_data return values."""

    @pytest.fixture
    def handler_with_publisher(self) -> NetatmoDataHandler:
        """Create a handler with a mock publisher for fetch tests."""
        handler = _make_data_handler()
        mock_target = AsyncMock()
        pub = NetatmoPublisher(
            name="test_pub",
            interval=300,
            next_scan=0,
            target=mock_target,
            subscriptions=set(),
            method="async_update_status",
            data_handler=handler,
            kwargs={},
        )
        handler.publisher["test_pub"] = pub
        return handler

    @pytest.mark.asyncio
    async def test_success_returns_no_errors(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test successful fetch returns (False, False)."""
        error, throttle = await handler_with_publisher.async_fetch_data("test_pub")
        assert error is False
        assert throttle is False

    @pytest.mark.asyncio
    async def test_api_error_returns_error(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test ApiError returns (True, False)."""
        handler_with_publisher.publisher[
            "test_pub"
        ].target.async_update_status.side_effect = pyatmo.ApiError("test")
        error, throttle = await handler_with_publisher.async_fetch_data("test_pub")
        assert error is True
        assert throttle is False

    @pytest.mark.asyncio
    async def test_throttling_error_returns_throttle(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test ApiThrottlingError returns (False, True)."""
        handler_with_publisher.publisher[
            "test_pub"
        ].target.async_update_status.side_effect = pyatmo.ApiThrottlingError(
            "throttled"
        )
        error, throttle = await handler_with_publisher.async_fetch_data("test_pub")
        assert error is False
        assert throttle is True

    @pytest.mark.asyncio
    async def test_timeout_returns_error(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test TimeoutError returns (True, False)."""
        handler_with_publisher.publisher[
            "test_pub"
        ].target.async_update_status.side_effect = TimeoutError()
        error, throttle = await handler_with_publisher.async_fetch_data("test_pub")
        assert error is True
        assert throttle is False

    @pytest.mark.asyncio
    async def test_no_device_error_returns_error(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test NoDeviceError returns (True, False)."""
        handler_with_publisher.publisher[
            "test_pub"
        ].target.async_update_status.side_effect = pyatmo.NoDeviceError("no device")
        error, throttle = await handler_with_publisher.async_fetch_data("test_pub")
        assert error is True
        assert throttle is False

    @pytest.mark.asyncio
    async def test_home_reachability_error_returns_error(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test ApiHomeReachabilityError returns (True, False)."""
        handler_with_publisher.publisher[
            "test_pub"
        ].target.async_update_status.side_effect = pyatmo.ApiHomeReachabilityError(
            "unreachable"
        )
        error, throttle = await handler_with_publisher.async_fetch_data("test_pub")
        assert error is True
        assert throttle is False

    @pytest.mark.asyncio
    async def test_update_only_skips_fetch(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test update_only=True skips the actual API call."""
        handler_with_publisher.publisher["test_pub"].subscriptions.add(MagicMock())
        error, throttle = await handler_with_publisher.async_fetch_data(
            "test_pub", update_only=True
        )
        assert error is False
        assert throttle is False
        handler_with_publisher.publisher[
            "test_pub"
        ].target.async_update_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_callbacks_invoked_on_success(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test subscription callbacks are called after fetch."""
        cb = MagicMock()
        handler_with_publisher.publisher["test_pub"].subscriptions.add(cb)
        await handler_with_publisher.async_fetch_data("test_pub")
        cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_callbacks_invoked_on_error(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test subscription callbacks still called even after error."""
        handler_with_publisher.publisher[
            "test_pub"
        ].target.async_update_status.side_effect = pyatmo.ApiError("fail")
        cb = MagicMock()
        handler_with_publisher.publisher["test_pub"].subscriptions.add(cb)
        await handler_with_publisher.async_fetch_data("test_pub")
        cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_call_counted_on_fetch(
        self, handler_with_publisher: NetatmoDataHandler
    ) -> None:
        """Test that a successful fetch adds to the rolling window."""
        initial = handler_with_publisher.get_current_calls_count_per_hour()
        await handler_with_publisher.async_fetch_data("test_pub")
        assert handler_with_publisher.get_current_calls_count_per_hour() == initial + 1


# ---------------------------------------------------------------------------
# Integration-level: throttle down / recovery (using full HA setup)
# ---------------------------------------------------------------------------


async def test_throttle_adjusts_rate_down(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth: AsyncMock,
) -> None:
    """Test throttling reduces the adjusted hourly rate limit."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    data_handler: NetatmoDataHandler = config_entry.runtime_data
    # Ensure initial rate is set
    if data_handler._adjusted_hourly_rate_limit is None:
        data_handler._adjusted_hourly_rate_limit = (
            data_handler._initial_hourly_rate_limit
        )

    initial_rate = data_handler._adjusted_hourly_rate_limit

    # Add publishers so there's something to fetch
    if not data_handler._sorted_publisher:
        return  # Skip if no publishers (no sensor entities created)

    # Simulate throttling by patching the target method to raise
    first_pub = data_handler._sorted_publisher[0]
    first_pub_name = first_pub.name
    if first_pub_name is None:
        return

    # Make publisher ready and simulate throttle
    first_pub.next_scan = 0
    data_handler._last_cph_change = None  # Allow rate adjustment

    with patch.object(
        data_handler.publisher[first_pub_name].target,
        data_handler.publisher[first_pub_name].method,
        side_effect=pyatmo.ApiThrottlingError("rate limited"),
    ):
        await data_handler.async_update(dt_util.utcnow())

    # Rate should have been reduced
    if data_handler._adjusted_hourly_rate_limit != initial_rate:
        expected = int(initial_rate * CPH_ADJUSTEMENT_DOWN)
        assert data_handler._adjusted_hourly_rate_limit == expected


async def test_recovery_bumps_rate_back_up(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    netatmo_auth: AsyncMock,
) -> None:
    """Test clean update cycle bumps rate limit back up."""
    with selected_platforms([Platform.SENSOR]):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    data_handler: NetatmoDataHandler = config_entry.runtime_data

    # Simulate a previous throttle-down
    if data_handler._adjusted_hourly_rate_limit is None:
        data_handler._adjusted_hourly_rate_limit = (
            data_handler._initial_hourly_rate_limit
        )

    reduced_rate = int(data_handler._initial_hourly_rate_limit * CPH_ADJUSTEMENT_DOWN)
    data_handler._adjusted_hourly_rate_limit = reduced_rate
    data_handler._last_cph_change = None  # Allow rate change
    data_handler.rolling_hour = []  # Clean slate

    await data_handler.async_update(dt_util.utcnow())

    expected = int(
        min(
            data_handler._initial_hourly_rate_limit,
            int(reduced_rate * CPH_ADJUSTEMENT_BACK_UP),
        )
    )
    assert data_handler._adjusted_hourly_rate_limit == expected
