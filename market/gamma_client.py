# ==============================================================================
# gamma_client.py — Unified Market Discovery
# ==============================================================================
"""
AQL Gamma Client

Perubahan dari sebelumnya:
  - Unified discovery: /events (multi-outcome) + /markets (binary)
  - Market classifier: MULTI_OUTCOME / BINARY_ABOVE / BINARY_BELOW / BINARY_RANGE
  - endDate parser untuk semua format Polymarket
  - Deduplication berdasarkan condition_id
  - Adaptive liquidity threshold
  - Anti-detection: jitter + user-agent rotation tetap ada
  - CLOBExecutor: tambah sell_position untuk exit strategy
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from config.settings import settings
from core.location_registry import (
    CityInfo,
    calculate_min_liquidity,
    check_golden_hour,
    golden_hour_multiplier,
    resolve_location,
    GoldenHourStatus,
)

log = logging.getLogger("aql.gamma")


# ── User-Agent Pool ───────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36",

    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36",

    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
    "Gecko/20100101 Firefox/124.0",

    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",

    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36",
]


def _random_headers(include_content_type: bool = False) -> dict:
    headers = {
        "User-Agent":      random.choice(_USER_AGENTS),
        "Accept":          "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Cache-Control":   "no-cache",
    }
    if include_content_type:
        headers["Content-Type"] = "application/json"
    return headers


async def _jitter(min_ms: int = 300, max_ms: int = 1200) -> None:
    """Human-like delay."""
    delay_ms = random.randint(min_ms, max_ms)
    if random.random() < 0.05:
        delay_ms += random.randint(2000, 5000)
    await asyncio.sleep(delay_ms / 1000)


# ── endDate Parser ────────────────────────────────────────────────────────────

def parse_end_date(raw: dict) -> Optional[str]:
    """
    Parse endDate dari market/event dict ke ISO UTC string.

    Handles semua format Polymarket:
      "2026-04-05T04:00:00Z"        → langsung pakai
      "2026-04-05T00:00:00Z"        → langsung pakai
      "2026-04-05"                  → tambah T23:59:00Z
      "2026-04-05T23:59:00-05:00"  → konversi ke UTC
    """
    candidates = [
        raw.get("endDateIso"),
        raw.get("endDate"),
        raw.get("end_date"),
    ]

    for raw_date in candidates:
        if not raw_date:
            continue
        try:
            # Sudah ada timezone info
            if "T" in str(raw_date):
                dt = datetime.fromisoformat(
                    str(raw_date).replace("Z", "+00:00")
                )
                return dt.astimezone(timezone.utc).isoformat()
            else:
                # Hanya tanggal — asumsi 23:59 UTC
                return f"{raw_date}T23:59:00+00:00"
        except Exception:
            continue

    return None


def hours_to_close(end_date_iso: str) -> float:
    """Hitung sisa jam sampai market close."""
    try:
        end   = datetime.fromisoformat(
            end_date_iso.replace("Z", "+00:00")
        )
        delta = end - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 3600, 0.0)
    except Exception:
        return 0.0


# ── Market Type Classifier ────────────────────────────────────────────────────

def classify_market_type(question: str) -> str:
    """
    Klasifikasikan tipe market dari judul pertanyaan.

    Returns:
      "MULTI_OUTCOME" — "Highest temperature in NYC on April 5?"
      "BINARY_RANGE"  — "Will temp be 56-57°F?"
      "BINARY_ABOVE"  — "Will temp exceed 90°F?"
      "BINARY_BELOW"  — "Will temp stay below 32°F?"
      "UNKNOWN"       — tidak bisa diklasifikasi
    """
    q_upper = question.upper()
    q_lower = question.lower()

    # Multi-outcome patterns
    if any(kw in q_lower for kw in [
        "highest temperature in",
        "highest temp in",
        "daily high in",
        "maximum temperature in",
    ]):
        return "MULTI_OUTCOME"

    # Range patterns
    if re.search(
        r"\d+\.?\d*\s*[-–]\s*\d+\.?\d*\s*°?\s*[FfCc]",
        question
    ):
        return "BINARY_RANGE"
    if re.search(
        r"between\s+\d+.*and\s+\d+",
        q_lower
    ):
        return "BINARY_RANGE"

    # Above/Below
    above_kws = [
        "EXCEED", "ABOVE", "OVER", "MORE THAN",
        "AT LEAST", "SURPASS", "REACH OR EXCEED",
    ]
    below_kws = [
        "BELOW", "UNDER", "LESS THAN", "NOT REACH",
        "STAY UNDER", "DROP BELOW", "NOT EXCEED",
    ]

    if any(kw in q_upper for kw in below_kws):
        return "BINARY_BELOW"
    if any(kw in q_upper for kw in above_kws):
        return "BINARY_ABOVE"

    return "UNKNOWN"


# ── Temperature Filter ────────────────────────────────────────────────────────

TEMPERATURE_KEYWORDS = [
    "temperature", "temp", "degrees", "fahrenheit", "celsius",
    "weather", "exceed", "°f", "°c", "heat", "cold",
    "highest temperature", "daily high", "high temp",
]

TEMPERATURE_TAGS = ["weather", "temperature", "climate", "meteorology"]


def _is_temperature_market(raw: dict) -> bool:
    question    = (raw.get("question") or "").lower()
    description = (raw.get("description") or "").lower()
    tags        = [t.lower() for t in (raw.get("tags") or [])]

    if any(tag in TEMPERATURE_TAGS for tag in tags):
        return True

    combined = question + " " + description
    return any(kw in combined for kw in TEMPERATURE_KEYWORDS)


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class OutcomeToken:
    """Satu outcome dari multi-outcome event."""
    label: str          # "76°F", "13°C"
    token_id: str       # YES token ID
    price: float        # Implied probability
    volume_24h: float   # Volume 24 jam


@dataclass
class TemperatureMarket:
    """
    Unified market container untuk semua tipe market.
    """
    condition_id: str
    event_slug: str
    question: str
    description: str
    end_date_iso: str
    market_type: str          # MULTI_OUTCOME / BINARY_ABOVE / dll

    # Untuk MULTI_OUTCOME: list semua outcomes
    outcomes: list[OutcomeToken] = field(default_factory=list)

    # Untuk BINARY markets: YES dan NO token
    yes_token_id: str = ""
    no_token_id: str  = ""
    best_bid: float   = 0.5
    best_ask: float   = 0.5

    # Market metadata
    volume_usd: float     = 0.0
    liquidity_usd: float  = 0.0
    url: str              = ""

    # Resolved fields (diisi setelah discovery)
    city: Optional[CityInfo]      = None
    golden_hour_status: str       = ""
    golden_hour_mult: float       = 1.0
    htc: float                    = 0.0   # hours to close

    @property
    def mid_price(self) -> float:
        return round((self.best_bid + self.best_ask) / 2.0, 4)

    @property
    def cache_key(self) -> str:
        """Key untuk market cache."""
        city_key = self.city.key if self.city else "unknown"
        date_str = self.end_date_iso[:10]
        return f"{city_key}-{date_str}-{self.market_type}"


# ── Gamma Client ──────────────────────────────────────────────────────────────

class GammaClient:
    """
    Unified Polymarket Gamma API client.
    Fetch events (multi-outcome) + markets (binary) sekaligus.
    """

    BASE = settings.POLY_GAMMA_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def _get(
        self,
        path: str,
        params: Optional[dict] = None,
    ) -> list | dict:
        await _jitter(min_ms=300, max_ms=900)
        resp = await self._http.get(
            f"{self.BASE}{path}",
            params=params,
            headers=_random_headers(),
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Event Fetch (Multi-Outcome) ───────────────────────────────────────

    async def _fetch_temperature_events(self) -> list[dict]:
        """
        Fetch temperature events dari /events endpoint.
        Setiap event = satu kota + satu tanggal dengan 11 outcomes.
        """
        log.info("[Discovery] Fetching temperature events...")
        events = []
        offset, limit = 0, 100

        while True:
            try:
                await _jitter(min_ms=200, max_ms=600)
                page = await self._get(
                    "/events",
                    params={
                        "active":    "true",
                        "closed":    "false",
                        "tag":       "temperature",
                        "limit":     limit,
                        "offset":    offset,
                        "order":     "volume",
                        "ascending": "false",
                    },
                )
            except Exception as e:
                log.error("[Discovery] Events fetch error: %s", e)
                break

            batch = page if isinstance(page, list) else page.get("events", [])
            if not batch:
                break

            events.extend(batch)
            if len(batch) < limit:
                break
            offset += limit

        log.info("[Discovery] Events fetched: %d", len(events))
        return events

    # ── Market Fetch (Binary) ─────────────────────────────────────────────

    async def _fetch_binary_markets(self) -> list[dict]:
        """
        Fetch binary temperature markets dari /markets endpoint.
        Ini adalah YES/NO markets seperti "Will NYC exceed 90°F?"
        """
        log.info("[Discovery] Fetching binary temperature markets...")
        markets = []
        offset, limit = 0, 100

        while True:
            try:
                await _jitter(min_ms=200, max_ms=600)
                page = await self._get(
                    "/markets",
                    params={
                        "active":    "true",
                        "closed":    "false",
                        "limit":     limit,
                        "offset":    offset,
                        "order":     "volumeNum",
                        "ascending": "false",
                    },
                )
            except Exception as e:
                log.error("[Discovery] Markets fetch error: %s", e)
                break

            batch = page if isinstance(page, list) else page.get("markets", [])
            if not batch:
                break

            # Filter temperature markets saja
            temp_batch = [m for m in batch if _is_temperature_market(m)]
            markets.extend(temp_batch)

            if len(batch) < limit:
                break
            offset += limit

        log.info(
            "[Discovery] Binary markets fetched: %d", len(markets)
        )
        return markets

    # ── Parse Event → TemperatureMarket ──────────────────────────────────

    def _parse_event(self, event: dict) -> list[TemperatureMarket]:
        """
        Parse satu event menjadi list TemperatureMarket.
        Satu event bisa punya banyak markets (outcomes).
        """
        markets_raw = event.get("markets", [])
        if not markets_raw:
            return []

        event_slug = event.get("slug", event.get("id", ""))
        event_url  = f"https://polymarket.com/event/{event_slug}"

        # Kumpulkan semua outcomes dari markets dalam event
        outcomes: list[OutcomeToken] = []
        condition_id = ""
        end_date     = parse_end_date(event)

        if not end_date:
            # Coba dari market pertama
            if markets_raw:
                end_date = parse_end_date(markets_raw[0])

        if not end_date:
            return []

        total_volume   = 0.0
        total_liquidity = 0.0

        for mkt in markets_raw:
            label    = mkt.get("question", mkt.get("groupItemTitle", ""))
            tokens   = mkt.get("clobTokenIds") or mkt.get("tokens") or []
            prices   = mkt.get("outcomePrices", ["0.5", "0.5"])
            vol      = float(mkt.get("volume24hr") or mkt.get("volumeNum") or 0)
            liq      = float(mkt.get("liquidityNum") or mkt.get("liquidity") or 0)

            total_volume    += vol
            total_liquidity += liq

            # YES token = index 0
            if tokens:
                yes_token = (
                    tokens[0] if isinstance(tokens[0], str)
                    else tokens[0].get("token_id", "")
                )
                try:
                    price = float(prices[0]) if prices else 0.5
                except (ValueError, IndexError):
                    price = 0.5

                outcomes.append(OutcomeToken(
                    label=label,
                    token_id=yes_token,
                    price=price,
                    volume_24h=vol,
                ))

            if not condition_id:
                condition_id = str(
                    mkt.get("conditionId")
                    or mkt.get("condition_id", "")
                )

        if not outcomes:
            return []

        # Event question = judul event
        event_question = event.get("title", event.get("question", ""))

        return [TemperatureMarket(
            condition_id=condition_id,
            event_slug=event_slug,
            question=event_question,
            description=event.get("description", ""),
            end_date_iso=end_date,
            market_type="MULTI_OUTCOME",
            outcomes=outcomes,
            volume_usd=total_volume,
            liquidity_usd=total_liquidity,
            url=event_url,
        )]

    # ── Parse Binary Market → TemperatureMarket ───────────────────────────

    def _parse_binary(self, raw: dict) -> Optional[TemperatureMarket]:
        """Parse satu binary market."""
        end_date = parse_end_date(raw)
        if not end_date:
            return None

        tokens = raw.get("clobTokenIds") or raw.get("tokens") or []
        if len(tokens) < 2:
            return None

        def _tok(t) -> str:
            return t if isinstance(t, str) else t.get("token_id", "")

        yes_tok = _tok(tokens[0])
        no_tok  = _tok(tokens[1])

        question = raw.get("question", "")
        mtype    = classify_market_type(question)

        if mtype == "UNKNOWN" or mtype == "MULTI_OUTCOME":
            return None

        prices = raw.get("outcomePrices", ["0.5", "0.5"])
        try:
            prices_list = (
                prices if isinstance(prices, list)
                else __import__("json").loads(prices)
            )
            bid = float(prices_list[0])
            ask = 1.0 - float(prices_list[1])
        except Exception:
            bid = float(raw.get("bestBid") or 0.5)
            ask = float(raw.get("bestAsk") or 0.5)

        slug = raw.get("slug", raw.get("id", ""))

        return TemperatureMarket(
            condition_id=str(
                raw.get("conditionId")
                or raw.get("condition_id", "")
            ),
            event_slug=str(slug),
            question=question,
            description=raw.get("description", ""),
            end_date_iso=end_date,
            market_type=mtype,
            yes_token_id=yes_tok,
            no_token_id=no_tok,
            best_bid=round(bid, 4),
            best_ask=round(ask, 4),
            volume_usd=float(
                raw.get("volumeNum") or raw.get("volume") or 0
            ),
            liquidity_usd=float(
                raw.get("liquidityNum") or raw.get("liquidity") or 0
            ),
            url=f"https://polymarket.com/event/{slug}",
        )

    # ── Unified Discovery ─────────────────────────────────────────────────

    async def discover_temperature_markets(self) -> list[TemperatureMarket]:
        """
        Unified discovery: fetch events + binary markets.

        Pipeline:
          1. Fetch /events (multi-outcome) + /markets (binary) parallel
          2. Parse ke TemperatureMarket
          3. Deduplicate by condition_id
          4. Resolve city dari LOCATION_REGISTRY
          5. Filter: liquidity + golden hour + hours_to_close
          6. Alert untuk unknown cities
          7. Return filtered list
        """
        log.info("[Discovery] Starting unified market discovery...")

        # 1. Fetch parallel
        events_raw, markets_raw = await asyncio.gather(
            self._fetch_temperature_events(),
            self._fetch_binary_markets(),
        )

        # 2. Parse
        all_markets: list[TemperatureMarket] = []

        for event in events_raw:
            parsed = self._parse_event(event)
            all_markets.extend(parsed)

        for mkt_raw in markets_raw:
            parsed = self._parse_binary(mkt_raw)
            if parsed:
                all_markets.append(parsed)

        log.info(
            "[Discovery] Total parsed: %d markets", len(all_markets)
        )

        # 3. Deduplicate by condition_id
        seen_ids: set[str]    = set()
        deduped: list[TemperatureMarket] = []
        for mkt in all_markets:
            if mkt.condition_id and mkt.condition_id in seen_ids:
                log.debug(
                    "[Discovery] Dedup skip: %s", mkt.condition_id[:20]
                )
                continue
            if mkt.condition_id:
                seen_ids.add(mkt.condition_id)
            deduped.append(mkt)

        log.info(
            "[Discovery] After dedup: %d markets", len(deduped)
        )

        # 4. Resolve city + filter
        result: list[TemperatureMarket] = []
        unknown_markets: list[TemperatureMarket] = []

        for mkt in deduped:
            # Resolve city
            city = resolve_location(mkt.question)
            if city is None:
                unknown_markets.append(mkt)
                continue

            mkt.city = city

            # Hitung hours to close
            htc = hours_to_close(mkt.end_date_iso)
            mkt.htc = htc

            # Hard limits
            if htc > settings.MAX_HOURS_TO_CLOSE:
                log.debug(
                    "[Discovery] Skip (too far) %.1fh: %s",
                    htc, mkt.question[:60],
                )
                continue

            if htc < settings.MIN_HOURS_TO_CLOSE:
                log.debug(
                    "[Discovery] Skip (too close) %.1fh: %s",
                    htc, mkt.question[:60],
                )
                continue

            # Golden Hour check
            gh_status = check_golden_hour(city, htc)
            gh_mult   = golden_hour_multiplier(gh_status)
            mkt.golden_hour_status = gh_status.value
            mkt.golden_hour_mult   = gh_mult

            if gh_status == GoldenHourStatus.SKIP:
                log.debug(
                    "[Discovery] Skip (golden hour) %s %.1fh: %s",
                    gh_status.value, htc, mkt.question[:60],
                )
                continue

            # Adaptive liquidity check
            min_liq = calculate_min_liquidity(
                mkt.market_type, htc, city.tier
            )
            if mkt.liquidity_usd < min_liq:
                log.debug(
                    "[Discovery] Skip (liquidity $%.0f < $%.0f): %s",
                    mkt.liquidity_usd, min_liq, mkt.question[:60],
                )
                continue

            result.append(mkt)
            log.info(
                "[DISCOVERED] %s | %s | %.1fh | $%.0f liq | GH=%s",
                city.key.upper(),
                mkt.market_type,
                htc,
                mkt.liquidity_usd,
                gh_status.value,
            )

        log.info(
            "[Discovery] Final: %d markets pass all filters",
            len(result),
        )

        # 5. Return unknown cities untuk alert di engine
        if unknown_markets:
            log.warning(
                "[Discovery] %d markets with unknown cities",
                len(unknown_markets),
            )
            # Simpan untuk diakses engine
            self._unknown_markets = unknown_markets
        else:
            self._unknown_markets = []

        return result

    @property
    def unknown_markets(self) -> list[TemperatureMarket]:
        return getattr(self, "_unknown_markets", [])

    # ── Price Refresh ─────────────────────────────────────────────────────

    async def refresh_market_price(
        self, condition_id: str
    ) -> Optional[float]:
        """Refresh harga terkini untuk satu market."""
        try:
            data = await self._get(f"/markets/{condition_id}")
            bid  = float(data.get("bestBid") or 0.5)
            ask  = float(data.get("bestAsk") or 0.5)
            return round((bid + ask) / 2.0, 4)
        except Exception as e:
            log.error(
                "Price refresh failed [%s]: %s", condition_id, e
            )
            return None


# ── CLOB Executor ─────────────────────────────────────────────────────────────

class CLOBExecutor:
    """
    Signs dan submit orders ke Polymarket CLOB.
    v2.0.0: tambah sell_position untuk exit strategy.
    """

    CLOB_BASE = settings.POLY_CLOB_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http    = http_client
        self._account = Account.from_key(settings.POLY_PRIVATE_KEY)
        log.info(
            "CLOB Executor ready | Signer: %s",
            self._account.address,
        )

    def _build_order(
        self,
        token_id: str,
        side: str,       # "BUY" atau "SELL"
        price: float,
        size_usd: float,
    ) -> dict:
        return {
            "tokenID":    token_id,
            "side":       side,
            "price":      str(round(price, 4)),
            "size":       str(round(size_usd / price, 4)),
            "nonce":      int(time.time() * 1000),
            "feeRateBps": "170",
            "expiration": "0",
            "maker":      self._account.address,
            "chainId":    settings.POLY_CHAIN_ID,
        }

    def _sign(self, order: dict) -> str:
        order_str = str(sorted(order.items()))
        h         = hashlib.sha3_256(order_str.encode()).hexdigest()
        msg       = encode_defunct(hexstr=h)
        return self._account.sign_message(msg).signature.hex()

    async def _submit(self, order: dict) -> Optional[dict]:
        """Internal submit order."""
        sig     = self._sign(order)
        payload = {"order": order, "signature": sig, "orderType": "FOK"}

        await _jitter(min_ms=500, max_ms=1500)

        try:
            resp = await self._http.post(
                f"{self.CLOB_BASE}/order",
                json=payload,
                headers=_random_headers(include_content_type=True),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as e:
            log.error(
                "CLOB failed [%d]: %s",
                e.response.status_code,
                e.response.text[:300],
            )
        except httpx.TimeoutException:
            log.error("CLOB timeout.")
        except Exception as e:
            log.error("CLOB error: %s", str(e))

        return None

    async def submit_order(
        self,
        token_id: str,
        size_usd: float,
        ask_price: float,
        slippage_pct: float = 0.02,
    ) -> Optional[dict]:
        """
        Submit BUY order untuk token YES/NO.
        Dipanggil oleh engine setelah signal terkonfirmasi.
        """
        limit_price = round(ask_price * (1 + slippage_pct), 4)
        order       = self._build_order(token_id, "BUY", limit_price, size_usd)
        receipt     = await self._submit(order)

        if receipt:
            log.info(
                "BUY OK | token=%s... | $%.2f @ %.4f | id=%s",
                token_id[:12],
                size_usd,
                limit_price,
                receipt.get("orderID", "?"),
            )

        return receipt

    async def sell_position(
        self,
        token_id: str,
        size_usd: float,
        entry_price: float,
        current_price: float,
        slippage_pct: float = 0.02,
    ) -> Optional[dict]:
        """
        Submit SELL order untuk exit position.
        Dipanggil oleh exit strategy (stop loss / take profit).
        """
        # Jumlah kontrak yang dimiliki
        contracts   = size_usd / entry_price

        # Harga sell dengan slippage (sedikit lebih rendah)
        sell_price  = round(current_price * (1 - slippage_pct), 4)
        sell_value  = round(contracts * sell_price, 4)

        order   = self._build_order(token_id, "SELL", sell_price, sell_value)
        receipt = await self._submit(order)

        if receipt:
            log.info(
                "SELL OK | token=%s... | contracts=%.4f @ %.4f | id=%s",
                token_id[:12],
                contracts,
                sell_price,
                receipt.get("orderID", "?"),
            )

        return receipt
