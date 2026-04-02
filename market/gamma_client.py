# ══════════════════════════════════════════════════════════════════════════════
# gamma_client.py = Kode API Polymarket 
# ══════════════════════════════════════════════════════════════════════════════

"""
AQL Gamma Client — Polymarket Gamma API + CLOB Execution Layer
Responsibilities:
  1. Paginated market discovery with temperature keyword/tag filtering
  2. Liquidity + timing gate (12–14h before resolution window)
  3. EIP-712 order signing and CLOB submission (Polygon network)
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from config.settings import settings

log = logging.getLogger("aql.gamma")


# ── Market Data Container ─────────────────────────────────────────────────────

@dataclass
class PolyMarket:
    market_id: str
    condition_id: str
    question: str
    description: str
    end_date_iso: str
    yes_token_id: str
    no_token_id: str
    best_bid: float
    best_ask: float
    mid_price: float
    volume_usd: float
    liquidity_usd: float
    active: bool
    url: str

    @property
    def hours_to_close(self) -> float:
        try:
            end   = datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00"))
            delta = end - datetime.now(timezone.utc)
            return max(delta.total_seconds() / 3600, 0.0)
        except Exception:
            return 0.0


# ── Temperature Market Classifier ────────────────────────────────────────────

TEMPERATURE_KEYWORDS = [
    "temperature", "temp", "high temp", "low temp", "degrees",
    "fahrenheit", "celsius", "weather", "exceed", "°f", "°c",
    "thermometer", "heat index", "cold record", "record high", "record low",
]

TEMPERATURE_TAGS = ["weather", "temperature", "climate", "meteorology"]


def _is_temperature_market(raw: dict) -> bool:
    question    = (raw.get("question")    or "").lower()
    description = (raw.get("description") or "").lower()
    tags        = [t.lower() for t in (raw.get("tags") or [])]

    if any(tag in TEMPERATURE_TAGS for tag in tags):
        return True

    combined = question + " " + description
    return any(kw in combined for kw in TEMPERATURE_KEYWORDS)


# ── Gamma REST Client ─────────────────────────────────────────────────────────

class GammaClient:
    """Thin async wrapper around Polymarket's Gamma read-only API."""

    BASE = settings.POLY_GAMMA_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    async def _get(self, path: str, params: Optional[dict] = None) -> list | dict:
        resp = await self._http.get(f"{self.BASE}{path}", params=params, timeout=20.0)
        resp.raise_for_status()
        return resp.json()

    async def discover_temperature_markets(
        self,
        min_liquidity_usd: float = 500.0,
        hours_before_close_min: float = 12.0,
        hours_before_close_max: float = 14.0,
    ) -> list[PolyMarket]:
        """
        Returns temperature markets that satisfy:
          • Active + not yet closed
          • Liquidity ≥ min_liquidity_usd
          • Resolution in [hours_before_close_min, hours_before_close_max]
        """
        log.info("Market discovery — scanning Gamma API...")
        raw_markets: list[dict] = []
        offset, limit = 0, 100

        while True:
            try:
                page = await self._get(
                    "/markets",
                    params={
                        "active":     "true",
                        "closed":     "false",
                        "limit":      limit,
                        "offset":     offset,
                        "order":      "volumeNum",
                        "ascending":  "false",
                    },
                )
            except httpx.HTTPStatusError as e:
                log.error("Gamma API paginate error: HTTP %d", e.response.status_code)
                break
            except httpx.TimeoutException:
                log.error("Gamma API paginate timeout at offset=%d", offset)
                break

            batch = page if isinstance(page, list) else page.get("markets", [])
            if not batch:
                break
            raw_markets.extend(batch)
            if len(batch) < limit:
                break
            offset += limit

        log.info("Gamma API returned %d total active markets.", len(raw_markets))

        markets: list[PolyMarket] = []
        for raw in raw_markets:
            if not _is_temperature_market(raw):
                continue

            tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
            if len(tokens) < 2:
                continue

            def _tok(t) -> str:
                return t if isinstance(t, str) else t.get("token_id", "")

            yes_tok = _tok(tokens[0])
            no_tok  = _tok(tokens[1])

            liquidity = float(raw.get("liquidityNum") or raw.get("liquidity") or 0)
            volume    = float(raw.get("volumeNum")    or raw.get("volume")    or 0)
            bid       = float(raw.get("bestBid")      or 0.5)
            ask       = float(raw.get("bestAsk")      or 0.5)
            mid       = (bid + ask) / 2.0
            end_date  = raw.get("endDateIso") or raw.get("endDate") or ""
            url       = f"https://polymarket.com/event/{raw.get('slug', raw.get('id', ''))}"

            pm = PolyMarket(
                market_id=str(raw.get("id", "")),
                condition_id=str(raw.get("conditionId") or raw.get("condition_id", "")),
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                end_date_iso=end_date,
                yes_token_id=yes_tok,
                no_token_id=no_tok,
                best_bid=round(bid, 4),
                best_ask=round(ask, 4),
                mid_price=round(mid, 4),
                volume_usd=volume,
                liquidity_usd=liquidity,
                active=True,
                url=url,
            )

            if pm.liquidity_usd < min_liquidity_usd:
                continue

            htc = pm.hours_to_close
            if not (hours_before_close_min <= htc <= hours_before_close_max):
                continue

            markets.append(pm)
            log.info(
                "[DISCOVERED] %.1fh | $%.0f liq | mid=%.3f | %s",
                htc, pm.liquidity_usd, pm.mid_price, pm.question[:70],
            )

        log.info("Temperature markets in entry window: %d", len(markets))
        return markets

    async def refresh_market_price(self, condition_id: str) -> Optional[float]:
        """Return current mid-price or None on error."""
        try:
            data = await self._get(f"/markets/{condition_id}")
            bid  = float(data.get("bestBid") or 0.5)
            ask  = float(data.get("bestAsk") or 0.5)
            return round((bid + ask) / 2.0, 4)
        except Exception as e:
            log.error("Price refresh failed [%s]: %s", condition_id, e)
            return None


# ── CLOB Order Executor ───────────────────────────────────────────────────────

class CLOBExecutor:
    """
    Signs and submits limit orders to Polymarket CLOB via Polygon-signed messages.
    Private key is sourced exclusively from POLY_PRIVATE_KEY env var.
    """

    CLOB_BASE = settings.POLY_CLOB_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http    = http_client
        self._account = Account.from_key(settings.POLY_PRIVATE_KEY)
        log.info("CLOB Executor ready | Signer: %s", self._account.address)

    def _build_order(self, token_id: str, price: float, size_usd: float) -> dict:
        return {
            "tokenID":    token_id,
            "side":       "BUY",
            "price":      str(round(price, 4)),
            "size":       str(round(size_usd / price, 4)),
            "nonce":      int(time.time() * 1000),
            "feeRateBps": "170",      # 1.7%
            "expiration": "0",        # GTC
            "maker":      self._account.address,
            "chainId":    settings.POLY_CHAIN_ID,
        }

    def _sign(self, order: dict) -> str:
        order_str = str(sorted(order.items()))
        h         = hashlib.sha3_256(order_str.encode()).hexdigest()
        msg       = encode_defunct(hexstr=h)
        return self._account.sign_message(msg).signature.hex()

    async def submit_order(
        self,
        market: PolyMarket,
        side: str,
        size_usd: float,
        slippage_pct: float = 0.02,
    ) -> Optional[dict]:
        """
        Build, sign, and submit a Fill-or-Kill limit order.
        Returns the CLOB receipt dict or None on any failure.
        """
        token_id    = market.yes_token_id if side == "YES" else market.no_token_id
        raw_price   = market.best_ask if side == "YES" else (1.0 - market.best_bid)
        limit_price = round(raw_price * (1 + slippage_pct), 4)

        order   = self._build_order(token_id, limit_price, size_usd)
        sig     = self._sign(order)
        payload = {"order": order, "signature": sig, "orderType": "FOK"}

        try:
            resp = await self._http.post(
                f"{self.CLOB_BASE}/order",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=15.0,
            )
            resp.raise_for_status()
            receipt = resp.json()
            log.info(
                "ORDER OK | %s | $%.2f @ %.4f | id=%s",
                side, size_usd, limit_price, receipt.get("orderID", "?"),
            )
            return receipt

        except httpx.HTTPStatusError as e:
            log.error(
                "CLOB submission failed [%d]: %s",
                e.response.status_code, e.response.text[:300],
            )
        except httpx.TimeoutException:
            log.error("CLOB submission timed out.")
        except Exception as e:
            log.error("CLOB unexpected error: %s", str(e))

        return None

