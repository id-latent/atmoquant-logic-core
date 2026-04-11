"""
test_gamma_client.py — Mock test untuk market/gamma_client.py

Menggunakan respx untuk mock HTTP calls ke Polymarket API.
Tanpa mock, test ini membutuhkan koneksi internet ke Polymarket.
Dengan mock, test berjalan offline dan deterministik.

Yang diuji:
  1. GammaClient.discover_temperature_markets() — discovery + filtering
  2. CLOBExecutor.submit_order() — build order, sign, kirim
  3. CLOBExecutor.sell_position() — sell order
  4. Filter: hanya market suhu yang lolos, bukan crypto/election/dll
  5. Token selection: BUY_YES pakai YES token, BUY_NO pakai NO token
"""
import json
import pytest
import httpx
import respx
from datetime import datetime, timezone, timedelta

from market.gamma_client import (
    GammaClient, CLOBExecutor, TemperatureMarket,
    _is_temperature_market, _classify_binary,
    MULTI_OUTCOME, BINARY_ABOVE, BINARY_BELOW, BINARY_RANGE, BINARY_UNKNOWN,
)
from config.settings import settings


# ==============================================================================
# TEST _is_temperature_market — Filter market suhu
# ==============================================================================

class TestIsTemperatureMarket:
    """
    _is_temperature_market() adalah gatekeeper pertama.
    Jika filter ini salah, bot bisa trading market crypto/politik.
    """

    def test_temperature_market_passes(self):
        assert _is_temperature_market({
            "question": "Will the high temperature in NYC exceed 90°F?",
            "description": "Daily high temperature market",
            "tags": ["temperature"],
        }) is True

    def test_celsius_market_passes(self):
        assert _is_temperature_market({
            "question": "Will London reach 28°C today?",
            "description": "temperature forecast",
            "tags": ["temperature"],
        }) is True

    def test_crypto_market_blocked(self):
        assert _is_temperature_market({
            "question": "Will Bitcoin exceed $100k?",
            "description": "crypto price prediction",
            "tags": ["crypto"],
        }) is False

    def test_election_market_blocked(self):
        assert _is_temperature_market({
            "question": "Will the president win the election?",
            "description": "political market",
            "tags": [],
        }) is False

    def test_earthquake_market_blocked(self):
        """Earthquake punya angka tapi bukan suhu."""
        assert _is_temperature_market({
            "question": "Will there be a magnitude 6 earthquake?",
            "description": "seismic activity",
            "tags": [],
        }) is False

    def test_hurricane_market_blocked(self):
        assert _is_temperature_market({
            "question": "How many hurricanes category 5 this season?",
            "description": "storm count",
            "tags": [],
        }) is False

    def test_false_positive_guard(self):
        """Market yang menyebut angka tapi bukan suhu harus di-blokir."""
        assert _is_temperature_market({
            "question": "Will rainfall exceed 5 inches this week?",
            "description": "precipitation forecast",
            "tags": ["weather"],
        }) is False


# ==============================================================================
# TEST _classify_binary — Klasifikasi jenis binary market
# ==============================================================================

class TestClassifyBinary:

    def test_above_keywords(self):
        assert _classify_binary("Will NYC exceed 90°F?") == BINARY_ABOVE
        assert _classify_binary("Will temperature be above 30°C?") == BINARY_ABOVE
        assert _classify_binary("Will it reach or exceed 95°F?") == BINARY_ABOVE

    def test_below_keywords(self):
        assert _classify_binary("Will temp stay below 32°F?") == BINARY_BELOW
        assert _classify_binary("Will it be under 0°C?") == BINARY_BELOW

    def test_range_keywords(self):
        assert _classify_binary("Will temp be between 85-90°F?") == BINARY_RANGE

    def test_unknown_pattern(self):
        assert _classify_binary("Highest temperature NYC 2025?") == BINARY_UNKNOWN


# ==============================================================================
# TEST GammaClient.discover_temperature_markets() — dengan respx mock
# ==============================================================================

# Contoh response dari Polymarket Gamma API /events
def _future_iso(hours: float = 5.0) -> str:
    """Generate ISO timestamp N jam dari sekarang untuk test data."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


MOCK_EVENTS_RESPONSE = [
    {
        "id": "event_nyc_001",
        "slug": "nyc-high-temp-apr-15",
        "title": "Highest temperature in New York City on April 15?",
        "description": "Daily high temperature for NYC",
        "endDate": _future_iso(5.0),
        "markets": [
            {
                "conditionId": "cond_001_a",
                "groupItemTitle": "86°F",
                "tokens": ["yes_token_86F"],
                "bestBid": 0.15,
                "bestAsk": 0.17,
                "liquidityNum": 1200.0,
                "volumeNum": 500.0,
            },
            {
                "conditionId": "cond_001_b",
                "groupItemTitle": "88°F",
                "tokens": ["yes_token_88F"],
                "bestBid": 0.35,
                "bestAsk": 0.37,
                "liquidityNum": 2000.0,
                "volumeNum": 800.0,
            },
            {
                "conditionId": "cond_001_c",
                "groupItemTitle": "90°F or higher",
                "tokens": ["yes_token_90F"],
                "bestBid": 0.45,
                "bestAsk": 0.47,
                "liquidityNum": 1800.0,
                "volumeNum": 700.0,
            },
        ],
    },
    {
        "id": "event_bitcoin_002",
        "slug": "bitcoin-price",
        "title": "Will Bitcoin exceed $100k?",
        "description": "crypto market",
        "endDate": "2026-04-15T23:00:00Z",
        "markets": [],
    },
]

# Contoh response dari /markets (binary markets)
MOCK_BINARY_RESPONSE = [
    {
        "id": "market_binary_001",
        "conditionId": "cond_binary_001",
        "question": "Will the high temperature in London exceed 25°C on April 15?",
        "description": "Daily high temperature",
        "endDateIso": _future_iso(5.0),
        "bestBid": 0.40,
        "bestAsk": 0.42,
        "liquidityNum": 800.0,
        "volumeNum": 300.0,
        "tokens": ["yes_token_london", "no_token_london"],
        "tags": ["temperature"],
        "slug": "london-high-temp-apr-15",
    },
]


@pytest.mark.asyncio
class TestGammaClientDiscovery:

    @respx.mock
    async def test_discover_returns_temperature_markets_only(self):
        """
        Dari 2 events (1 suhu + 1 crypto), hanya 1 yang lolos.
        """
        # Mock /events endpoint
        respx.get(
            f"{settings.POLY_GAMMA_BASE}/events",
        ).mock(return_value=httpx.Response(200, json=MOCK_EVENTS_RESPONSE))

        # Mock /markets endpoint (return kosong agar tidak mempersulit)
        respx.get(
            f"{settings.POLY_GAMMA_BASE}/markets",
        ).mock(return_value=httpx.Response(200, json=[]))

        async with httpx.AsyncClient() as client:
            gamma   = GammaClient(client)
            markets = await gamma.discover_temperature_markets(
                min_liquidity_usd=100.0,  # rendah agar tidak difilter
                hours_before_close_min=0.1,
                hours_before_close_max=8760.0,  # 1 tahun
            )

        # Hanya market NYC suhu yang lolos, bukan Bitcoin
        market_questions = [m.question for m in markets]
        assert any("New York" in q or "NYC" in q for q in market_questions), \
            f"Market NYC tidak ditemukan di: {market_questions}"
        assert not any("Bitcoin" in q for q in market_questions), \
            "Market Bitcoin tidak seharusnya ada!"

    @respx.mock
    async def test_unknown_cities_collected_separately(self):
        """
        Market yang kotanya tidak dikenal dikumpulkan di unknown_markets,
        bukan di hasil utama.
        """
        unknown_event = [{
            "id": "event_unknown",
            "slug": "unknown-city-temp",
            "title": "Highest temperature in Atlantis on April 15?",
            "description": "temperature",
            "endDate": _future_iso(10.0),
            "markets": [{
                "conditionId": "cond_unknown",
                "groupItemTitle": "86°F",
                "tokens": ["token_x"],
                "bestBid": 0.50, "bestAsk": 0.50,
                "liquidityNum": 1000.0, "volumeNum": 200.0,
            }],
        }]

        respx.get(f"{settings.POLY_GAMMA_BASE}/events").mock(
            return_value=httpx.Response(200, json=unknown_event)
        )
        respx.get(f"{settings.POLY_GAMMA_BASE}/markets").mock(
            return_value=httpx.Response(200, json=[])
        )

        async with httpx.AsyncClient() as client:
            gamma   = GammaClient(client)
            markets = await gamma.discover_temperature_markets(
                min_liquidity_usd=100.0,
                hours_before_close_min=0.1,
                hours_before_close_max=8760.0,
            )

        # Atlantis tidak ada di registry → masuk unknown_markets
        assert len(gamma.unknown_markets) >= 1, "Atlantis harus masuk unknown_markets"
        assert len(markets) == 0 or not any("Atlantis" in str(m) for m in markets)

    @respx.mock
    async def test_liquidity_filter_works(self):
        """Market dengan liquidity terlalu rendah harus difilter."""
        low_liq_event = [{
            "id": "event_low_liq",
            "slug": "nyc-low-liquidity",
            "title": "Highest temperature in New York City on April 20?",
            "description": "temperature NYC",
            "endDate": _future_iso(8.0),
            "markets": [{
                "conditionId": "cond_low_liq",
                "groupItemTitle": "86°F",
                "tokens": ["token_y"],
                "bestBid": 0.50, "bestAsk": 0.50,
                "liquidityNum": 50.0,    # sangat rendah
                "volumeNum": 10.0,
            }],
        }]

        respx.get(f"{settings.POLY_GAMMA_BASE}/events").mock(
            return_value=httpx.Response(200, json=low_liq_event)
        )
        respx.get(f"{settings.POLY_GAMMA_BASE}/markets").mock(
            return_value=httpx.Response(200, json=[])
        )

        async with httpx.AsyncClient() as client:
            gamma   = GammaClient(client)
            markets = await gamma.discover_temperature_markets(
                min_liquidity_usd=500.0,  # filter tinggi
                hours_before_close_min=0.1,
                hours_before_close_max=8760.0,
            )

        assert len(markets) == 0, \
            f"Market liquidity $50 harus difilter (min $500), dapat {len(markets)}"

    @respx.mock
    async def test_events_api_failure_fallback(self):
        """
        Jika /events gagal, bot harus fallback ke /markets saja
        dan tidak crash.
        """
        respx.get(f"{settings.POLY_GAMMA_BASE}/events").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        respx.get(f"{settings.POLY_GAMMA_BASE}/markets").mock(
            return_value=httpx.Response(200, json=MOCK_BINARY_RESPONSE)
        )

        async with httpx.AsyncClient() as client:
            gamma = GammaClient(client)
            # Tidak boleh raise exception
            markets = await gamma.discover_temperature_markets(
                min_liquidity_usd=100.0,
                hours_before_close_min=0.1,
                hours_before_close_max=8760.0,
            )

        # Fallback ke binary markets saja
        assert isinstance(markets, list), "Harus return list meski /events gagal"


# ==============================================================================
# TEST CLOBExecutor — Order submission dengan mock CLOB
# ==============================================================================

MOCK_ORDER_RECEIPT = {
    "orderID": "order_abc123",
    "status": "matched",
    "size": "10.0",
    "price": "0.5200",
}


@pytest.mark.asyncio
class TestCLOBExecutor:

    @respx.mock
    async def test_submit_order_success(self):
        """
        submit_order() harus:
        1. POST ke CLOB endpoint
        2. Payload berisi token_id, signature, dan orderType=FOK
        3. Return receipt saat berhasil
        """
        respx.post(f"{settings.POLY_CLOB_BASE}/order").mock(
            return_value=httpx.Response(200, json=MOCK_ORDER_RECEIPT)
        )

        async with httpx.AsyncClient() as client:
            clob    = CLOBExecutor(client)
            receipt = await clob.submit_order(
                token_id="yes_token_abc",
                size_usd=10.0,
                ask_price=0.50,
            )

        assert receipt is not None, "submit_order harus return receipt jika 200"
        assert receipt.get("orderID") == "order_abc123"

    @respx.mock
    async def test_submit_order_payload_structure(self):
        """
        Verifikasi struktur payload yang dikirim ke CLOB.
        Payload harus punya: order, signature, orderType.
        """
        captured_payload = {}

        def capture_and_respond(request):
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200, json=MOCK_ORDER_RECEIPT)

        respx.post(f"{settings.POLY_CLOB_BASE}/order").mock(
            side_effect=capture_and_respond
        )

        async with httpx.AsyncClient() as client:
            clob = CLOBExecutor(client)
            await clob.submit_order(
                token_id="yes_token_abc",
                size_usd=15.0,
                ask_price=0.55,
            )

        assert "order" in captured_payload,    "Payload harus punya 'order'"
        assert "signature" in captured_payload, "Payload harus punya 'signature'"
        assert "orderType" in captured_payload, "Payload harus punya 'orderType'"
        assert captured_payload["orderType"] == "FOK"

        order = captured_payload["order"]
        assert order.get("tokenID") == "yes_token_abc"
        assert order.get("side") == "BUY"

        # Verifikasi signature format (harus string yang dimulai 0x)
        sig = captured_payload["signature"]
        assert isinstance(sig, str), "Signature harus string"
        assert sig.startswith("0x"), f"Signature harus dimulai 0x, dapat: {sig[:20]}"

    @respx.mock
    async def test_submit_order_returns_none_on_4xx(self):
        """
        CLOB reject (4xx) → return None, bukan crash.
        Engine akan record rejection dan lanjut.
        """
        respx.post(f"{settings.POLY_CLOB_BASE}/order").mock(
            return_value=httpx.Response(400, text="Order rejected: insufficient liquidity")
        )

        async with httpx.AsyncClient() as client:
            clob    = CLOBExecutor(client)
            receipt = await clob.submit_order("token", 10.0, 0.50)

        assert receipt is None, "4xx harus return None, bukan raise exception"

    @respx.mock
    async def test_submit_order_returns_none_on_timeout(self):
        """Timeout → return None, bukan crash."""
        respx.post(f"{settings.POLY_CLOB_BASE}/order").mock(
            side_effect=httpx.TimeoutException("Connection timed out")
        )

        async with httpx.AsyncClient() as client:
            clob    = CLOBExecutor(client)
            receipt = await clob.submit_order("token", 10.0, 0.50)

        assert receipt is None

    @respx.mock
    async def test_sell_position_uses_sell_side(self):
        """sell_position() harus kirim order dengan side=SELL."""
        captured_payload = {}

        def capture(request):
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200, json=MOCK_ORDER_RECEIPT)

        respx.post(f"{settings.POLY_CLOB_BASE}/order").mock(side_effect=capture)

        async with httpx.AsyncClient() as client:
            clob = CLOBExecutor(client)
            await clob.sell_position(
                token_id="yes_token_abc",
                size_usd=10.0,
                entry_price=0.30,
                current_price=0.80,
            )

        order = captured_payload.get("order", {})
        assert order.get("side") == "SELL", \
            f"sell_position harus pakai side=SELL, dapat {order.get('side')}"

    @respx.mock
    async def test_buy_no_order_uses_no_token(self):
        """
        Bug #4 regression test:
        Jika engine memutuskan BUY_NO dan meneruskan no_token_id,
        CLOBExecutor harus mengirim order dengan token tersebut.
        """
        captured_payload = {}

        def capture(request):
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200, json=MOCK_ORDER_RECEIPT)

        respx.post(f"{settings.POLY_CLOB_BASE}/order").mock(side_effect=capture)

        no_token = "no_token_xyz789"

        async with httpx.AsyncClient() as client:
            clob = CLOBExecutor(client)
            # Engine meneruskan no_token_id sebagai token_id untuk BUY_NO
            await clob.submit_order(
                token_id=no_token,
                size_usd=10.0,
                ask_price=0.50,
            )

        order = captured_payload.get("order", {})
        assert order.get("tokenID") == no_token, \
            f"Token harus {no_token}, dapat {order.get('tokenID')}"

    @respx.mock
    async def test_slippage_applied_to_price(self):
        """
        ask_price=0.50 dengan slippage_pct=0.02 →
        limit_price = 0.50 * 1.02 = 0.51.
        """
        captured_payload = {}

        def capture(request):
            captured_payload.update(json.loads(request.content))
            return httpx.Response(200, json=MOCK_ORDER_RECEIPT)

        respx.post(f"{settings.POLY_CLOB_BASE}/order").mock(side_effect=capture)

        async with httpx.AsyncClient() as client:
            clob = CLOBExecutor(client)
            await clob.submit_order("token", 10.0, ask_price=0.50, slippage_pct=0.02)

        order = captured_payload.get("order", {})
        price = float(order.get("price", 0))
        expected = round(0.50 * 1.02, 4)
        assert abs(price - expected) < 0.001, \
            f"Slippage: expected price {expected}, dapat {price}"
