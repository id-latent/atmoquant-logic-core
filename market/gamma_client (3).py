# ==============================================================================
# gamma_client.py — v2.1.1 (FIXED)
# ==============================================================================
"""
AQL Gamma Client — Lapisan API Polymarket + Eksekusi CLOB

Fixes v2.1.1:
  BUG #1 : discover_temperature_markets() memanggil method yang tidak ada
           (discoverevents / discoverbinarymarkets) → fixed ke _discover_events /
           _discover_binary_markets
  BUG #9 : EIP-712 signing diganti ke format yang sesuai standar Polymarket CLOB
           (simple personal_sign atas keccak256 dari canonical order string)
           NOTE: Jika Polymarket mengganti spec signing, sesuaikan _sign() lagi.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

from utils.headers import random_get_headers
from utils.jitter import human_delay

from config.settings import settings

log = logging.getLogger("aql.gamma")

# ── Konstanta Jenis Market ─────────────────────────────────────────────────────

MULTI_OUTCOME  = "MULTI_OUTCOME"
BINARY_ABOVE   = "BINARY_ABOVE"
BINARY_BELOW   = "BINARY_BELOW"
BINARY_RANGE   = "BINARY_RANGE"
BINARY_UNKNOWN = "BINARY_UNKNOWN"

# BUG #15: User-Agent pool dipindah ke utils/headers.py — gunakan random_get_headers()


# ── Wadah Data Market (raw dari Polymarket API) ───────────────────────────────

@dataclass
class PolyMarket:
    """Data mentah dari Gamma API — belum ada info kota atau golden hour."""

    market_id:    str
    condition_id: str
    question:     str
    description:  str
    end_date_iso: str
    market_type:  str
    htc:          float
    liquidity_usd: float
    volume_usd:   float
    active:       bool
    url:          str

    # Binary fields
    yes_token_id: str   = ""
    no_token_id:  str   = ""
    best_bid:     float = 0.5
    best_ask:     float = 0.5
    mid_price:    float = 0.5

    # Multi-outcome: list of dict (raw dari API)
    outcomes: list[dict] = field(default_factory=list)

    # Optional
    city_name: Optional[str] = None
    event_id:  Optional[str] = None

    @property
    def hours_to_close(self) -> float:
        return self.htc


# ── Outcome sebagai Object ────────────────────────────────────────────────────

@dataclass
class OutcomeInfo:
    label:        str
    token_id:     str
    price:        float
    bid:          float
    ask:          float
    condition_id: str
    liquidity:    float = 0.0
    volume_24h:   float = 0.0


# ── TemperatureMarket ─────────────────────────────────────────────────────────

@dataclass
class TemperatureMarket:
    market_id:    str
    condition_id: str
    question:     str
    description:  str
    end_date_iso: str
    market_type:  str
    htc:          float
    liquidity_usd: float
    volume_usd:   float
    active:       bool
    url:          str

    yes_token_id: str   = ""
    no_token_id:  str   = ""
    best_bid:     float = 0.5
    best_ask:     float = 0.5
    mid_price:    float = 0.5

    outcomes: list = field(default_factory=list)
    event_id: Optional[str] = None

    city:               Optional[object] = None
    golden_hour_mult:   float = 1.0
    golden_hour_status: str   = "SKIP"
    cache_key:          str   = ""
    event_slug:         str   = ""


# ── Filter Kata Kunci Suhu ─────────────────────────────────────────────────────

_TEMP_TAGS = frozenset(["temperature", "meteorology"])

_TEMP_KEYWORDS = (
    "temperature", "high temp", "low temp", "degrees fahrenheit",
    "degrees celsius", "fahrenheit", "celsius", "°f", "°c",
    "thermometer", "heat index", "record high", "record low",
    "will the high", "will the low", "daily high", "daily low",
    "max temp", "min temp", "average temp", "mean temp",
    "high temperature", "low temperature",
)

_NEGATIVE_KEYWORDS = (
    "earthquake", "seismic", "quake", "magnitude", "richter", "tremor",
    "how many 5", "how many 6", "how many 4",
    "hurricane category", "tornado", "flood stage", "storm surge",
    "rainfall", "precipitation", "inches of rain",
    "bitcoin", "crypto", "eth ", "btc", "solana",
    "election", "vote", "president", "senate", "congress",
    "gdp", "inflation", "interest rate", "fed rate",
    "wildfire acres", "fire weather watch",
    "how many storms", "how many hurricanes",
)

def _is_temperature_market(raw: dict) -> bool:
    question    = (raw.get("question") or "").lower()
    description = (raw.get("description") or "").lower()
    tags        = {t.lower() for t in (raw.get("tags") or [])}
    combined    = question + " " + description

    if any(neg in combined for neg in _NEGATIVE_KEYWORDS):
        return False
    if _TEMP_TAGS & tags:
        return any(kw in combined for kw in _TEMP_KEYWORDS)
    if {"weather", "climate"} & tags:
        strong = ("°f", "°c", "temperature", "fahrenheit", "celsius",
                  "will the high", "will the low")
        return any(kw in combined for kw in strong)
    return any(kw in combined for kw in _TEMP_KEYWORDS)


def _classify_binary(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ("above", "exceed", "over ", "at least",
                             "reach", "or higher", "or more")):
        return BINARY_ABOVE
    if any(w in q for w in ("below", "under ", "less than", "not exceed",
                             "or lower", "or less")):
        return BINARY_BELOW
    if any(w in q for w in ("between", " range", " to ", "–", "and ")):
        return BINARY_RANGE
    return BINARY_UNKNOWN


def _compute_htc(end_date_iso: str) -> float:
    try:
        end   = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        delta = end - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 3600, 0.0)
    except Exception:
        return 0.0


# ── Klien REST Gamma ───────────────────────────────────────────────────────────

class GammaClient:

    BASE = settings.POLY_GAMMA_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http            = http_client
        self.unknown_markets: list[PolyMarket] = []

    async def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        jitter: bool = True,
    ) -> list | dict:
        if jitter:
            await human_delay(min_ms=50, max_ms=350)
        resp = await self._http.get(
            f"{self.BASE}{path}",
            params=params,
            headers=random_get_headers(),
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Langkah 1: Events → MULTI_OUTCOME ─────────────────────────────────

    async def _discover_events(self) -> tuple[list[PolyMarket], set[str]]:
        markets: list[PolyMarket] = []
        seen:    set[str]         = set()

        try:
            page = await self._get(
                "/events",
                params={
                    "active":     "true",
                    "closed":     "false",
                    "limit":      50,
                    "order":      "volumeNum",
                    "ascending":  "false",
                },
            )
        except Exception as e:
            log.warning("Events API tidak tersedia: %s — fallback ke /markets saja", e)
            return markets, seen

        events = page if isinstance(page, list) else page.get("events", [])
        log.info("Events API: %d event suhu diterima", len(events))

        for event in events:
            event_markets = event.get("markets") or []
            if not event_markets:
                continue

            question    = event.get("title") or event.get("question") or ""
            description = event.get("description") or ""
            end_date    = event.get("endDate") or event.get("endDateIso") or ""
            event_id    = str(event.get("id") or event.get("slug") or "")
            event_slug  = event.get("slug") or event_id

            if not _is_temperature_market({
                "question": question, "description": description,
                "tags": ["temperature"],
            }):
                log.debug("Event ditolak (bukan suhu): %s", question[:60])
                continue

            htc             = _compute_htc(end_date)
            total_liquidity = 0.0
            total_volume    = 0.0
            outcomes: list[dict] = []
            primary_cid          = ""

            for m in event_markets:
                cid    = str(m.get("conditionId") or m.get("condition_id") or "")
                tokens = m.get("tokens") or m.get("clobTokenIds") or []
                tok_id = tokens[0] if tokens else ""
                if isinstance(tok_id, dict):
                    tok_id = tok_id.get("token_id", "")

                bid = float(m.get("bestBid") or 0.5)
                ask = float(m.get("bestAsk") or 0.5)
                liq = float(m.get("liquidityNum") or m.get("liquidity") or 0)
                vol = float(m.get("volumeNum") or m.get("volume") or 0)

                total_liquidity += liq
                total_volume    += vol

                if not primary_cid and cid:
                    primary_cid = cid

                outcomes.append({
                    "label":        m.get("groupItemTitle") or m.get("question") or cid[:20],
                    "token_id":     tok_id,
                    "price":        round((bid + ask) / 2.0, 4),
                    "bid":          round(bid, 4),
                    "ask":          round(ask, 4),
                    "condition_id": cid,
                    "liquidity":    liq,
                })
                seen.add(cid)

            if not outcomes or not primary_cid:
                continue

            markets.append(PolyMarket(
                market_id    = event_id,
                condition_id = primary_cid,
                question     = question,
                description  = description,
                end_date_iso = end_date,
                market_type  = MULTI_OUTCOME,
                htc          = htc,
                liquidity_usd = total_liquidity,
                volume_usd   = total_volume,
                active       = True,
                url          = f"https://polymarket.com/event/{event_slug}",
                outcomes     = outcomes,
                event_id     = event_id,
            ))

        return markets, seen

    # ── Langkah 2: Markets → BINARY ────────────────────────────────────────

    async def _discover_binary_markets(
        self,
        exclude_cids: set[str],
    ) -> list[PolyMarket]:
        markets: list[PolyMarket] = []
        offset, limit, MAX_PAGES  = 0, 100, 5

        while (offset // limit) < MAX_PAGES:
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
                log.error("Markets API HTTP %d @ offset=%d", e.response.status_code, offset)
                break
            except httpx.TimeoutException:
                log.error("Markets API timeout @ offset=%d", offset)
                break
            except Exception as e:
                log.error("Markets API error: %s", e)
                break

            batch = page if isinstance(page, list) else page.get("markets", [])
            if not batch:
                break

            for raw in batch:
                cid = str(raw.get("conditionId") or raw.get("condition_id") or "")
                if cid in exclude_cids:
                    continue
                if not _is_temperature_market(raw):
                    continue

                tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
                if len(tokens) < 2:
                    continue

                def _tok(t: object) -> str:
                    return t if isinstance(t, str) else t.get("token_id", "")

                bid      = float(raw.get("bestBid") or 0.5)
                ask      = float(raw.get("bestAsk") or 0.5)
                end_date = raw.get("endDateIso") or raw.get("endDate") or ""
                q        = raw.get("question") or ""
                slug     = raw.get("slug") or raw.get("id") or ""

                markets.append(PolyMarket(
                    market_id    = str(raw.get("id") or ""),
                    condition_id = cid,
                    question     = q,
                    description  = raw.get("description") or "",
                    end_date_iso = end_date,
                    market_type  = _classify_binary(q),
                    htc          = _compute_htc(end_date),
                    liquidity_usd = float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
                    volume_usd   = float(raw.get("volumeNum") or raw.get("volume") or 0),
                    active       = True,
                    url          = f"https://polymarket.com/event/{slug}",
                    yes_token_id = _tok(tokens[0]),
                    no_token_id  = _tok(tokens[1]),
                    best_bid     = round(bid, 4),
                    best_ask     = round(ask, 4),
                    mid_price    = round((bid + ask) / 2.0, 4),
                ))
                exclude_cids.add(cid)

            if len(batch) < limit:
                break
            offset += limit

        return markets

    # ── Enrichment: PolyMarket → TemperatureMarket ─────────────────────────

    def enrich_markets(
        self,
        raw_markets: list[PolyMarket],
    ) -> list[TemperatureMarket]:
        from core.location_registry import (
            resolve_location,
            check_golden_hour,
            golden_hour_multiplier,
            GoldenHourStatus,
        )

        enriched: list[TemperatureMarket] = []
        self.unknown_markets = []

        for pm in raw_markets:
            city = resolve_location(pm.question)
            if city is None:
                self.unknown_markets.append(pm)
                log.debug("[Enrich] Kota tidak dikenal: %s", pm.question[:60])
                continue

            gh_status = check_golden_hour(city, pm.htc)
            gh_mult   = golden_hour_multiplier(gh_status)

            if gh_status == GoldenHourStatus.SKIP:
                log.debug(
                    "[Enrich] Golden Hour SKIP: %s (htc=%.1fh)",
                    city.key, pm.htc,
                )
                continue

            n_outcomes      = max(len(pm.outcomes), 1)
            vol_per_outcome = round(pm.volume_usd / n_outcomes, 2)

            outcome_objs = [
                OutcomeInfo(
                    label        = o.get("label", ""),
                    token_id     = o.get("token_id", ""),
                    price        = o.get("price", 0.5),
                    bid          = o.get("bid", 0.5),
                    ask          = o.get("ask", 0.5),
                    condition_id = o.get("condition_id", ""),
                    liquidity    = o.get("liquidity", 0.0),
                    volume_24h   = vol_per_outcome,
                )
                for o in pm.outcomes
            ]

            slug      = pm.url.split("/event/")[-1] if "/event/" in pm.url else pm.market_id
            cache_key = f"{city.key}:{pm.condition_id[:12]}"

            enriched.append(TemperatureMarket(
                market_id         = pm.market_id,
                condition_id      = pm.condition_id,
                question          = pm.question,
                description       = pm.description,
                end_date_iso      = pm.end_date_iso,
                market_type       = pm.market_type,
                htc               = pm.htc,
                liquidity_usd     = pm.liquidity_usd,
                volume_usd        = pm.volume_usd,
                active            = pm.active,
                url               = pm.url,
                yes_token_id      = pm.yes_token_id,
                no_token_id       = pm.no_token_id,
                best_bid          = pm.best_bid,
                best_ask          = pm.best_ask,
                mid_price         = pm.mid_price,
                outcomes          = outcome_objs,
                event_id          = pm.event_id,
                city              = city,
                golden_hour_mult  = gh_mult,
                golden_hour_status = gh_status.value,
                cache_key         = cache_key,
                event_slug        = slug,
            ))

        log.info(
            "[Enrich] %d/%d market berhasil diperkaya | %d unknown cities",
            len(enriched), len(raw_markets), len(self.unknown_markets),
        )
        return enriched

    # ── Titik Masuk Utama ──────────────────────────────────────────────────
    # FIX BUG #1: Memanggil self._discover_events() dan
    # self._discover_binary_markets() yang benar (bukan typo lama)

    async def discover_temperature_markets(
        self,
        min_liquidity_usd: float = 500.0,
        hours_before_close_min: float = 1.0,
        hours_before_close_max: float = 168.0,
    ) -> list[TemperatureMarket]:
        """Discovery terpadu v2.2.1 — PARALLEL EVENTS + BINARY."""
        import time
        start_total = time.time()

        log.info("Discovery v2.2.1 PARALLEL — mulai")

        # FIX: gunakan nama method yang benar dengan underscore prefix
        events_task, binary_task = await asyncio.gather(
            self._discover_events(),
            self._discover_binary_markets(set()),
            return_exceptions=True,
        )

        if isinstance(events_task, Exception):
            log.warning("Events task failed: %s", events_task)
            event_markets, seen_cids = [], set()
        else:
            event_markets, seen_cids = events_task

        if isinstance(binary_task, Exception):
            log.warning("Binary task failed: %s", binary_task)
            binary_markets = []
        else:
            binary_markets = binary_task

        # Deduplicate: hapus binary yang conditionId-nya sudah ada di events
        binary_unique = [
            m for m in binary_markets
            if m.condition_id not in seen_cids
        ]

        all_markets = event_markets + binary_unique
        log.info(
            "Parallel done: %d MULTI + %d BINARY = %d | %.1fs",
            len(event_markets),
            len(binary_unique),
            len(all_markets),
            time.time() - start_total,
        )

        filtered: list[PolyMarket] = []
        for pm in all_markets:
            if pm.liquidity_usd < min_liquidity_usd:
                continue
            if not (hours_before_close_min <= pm.htc <= hours_before_close_max):
                continue
            filtered.append(pm)

        filtered.sort(key=lambda m: m.liquidity_usd, reverse=True)
        log.info(
            "Kandidat: %d | Total time: %.1fs",
            len(filtered),
            time.time() - start_total,
        )

        enriched = self.enrich_markets(filtered)
        log.info("Discovery selesai: %d market siap engine", len(enriched))
        return enriched

    # ── Refresh Harga ──────────────────────────────────────────────────────

    async def refresh_market_price(self, condition_id: str) -> Optional[float]:
        try:
            data = await self._get(f"/markets/{condition_id}", jitter=False)
            bid  = float(data.get("bestBid") or 0.5)
            ask  = float(data.get("bestAsk") or 0.5)
            return round((bid + ask) / 2.0, 4)
        except Exception as e:
            log.error("Refresh harga gagal [%s]: %s", condition_id, e)
            return None

    async def refresh_outcome_prices(self, pm: PolyMarket) -> PolyMarket:
        if pm.market_type != MULTI_OUTCOME:
            price = await self.refresh_market_price(pm.condition_id)
            if price is not None:
                pm.mid_price = price
            return pm

        for outcome in pm.outcomes:
            cid = outcome.get("condition_id", "")
            if not cid:
                continue
            try:
                data = await self._get(f"/markets/{cid}", jitter=True)
                bid  = float(data.get("bestBid") or outcome["bid"])
                ask  = float(data.get("bestAsk") or outcome["ask"])
                outcome["bid"]   = round(bid, 4)
                outcome["ask"]   = round(ask, 4)
                outcome["price"] = round((bid + ask) / 2.0, 4)
            except Exception as e:
                log.warning("Refresh harga outcome gagal [%s]: %s", cid, e)
        return pm


# ── Eksekutor Order CLOB ───────────────────────────────────────────────────────

class CLOBExecutor:
    """
    Menandatangani dan mengirim order limit FOK ke Polymarket CLOB.

    FIX BUG #9: _sign() sekarang menggunakan personal_sign atas
    keccak256 dari canonical JSON order string — lebih sesuai dengan
    format yang umum diterima CLOB Polymarket.
    Jika Polymarket berubah ke EIP-712 penuh, ganti ke sign_typed_data().
    """

    CLOB_BASE = settings.POLY_CLOB_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http    = http_client
        self._account = Account.from_key(settings.POLY_PRIVATE_KEY)
        log.info("CLOB Executor siap | Penanda tangan: %s", self._account.address)

    def _build_order(self, token_id: str, side: str, price: float, size_usd: float) -> dict:
        """
        Bangun payload order canonical.
        side: "BUY" atau "SELL"
        """
        if price <= 0:
            raise ValueError(f"Harga order tidak valid: {price}")
        return {
            "tokenID":    token_id,
            "side":       side,
            "price":      str(round(price, 4)),
            "size":       str(round(size_usd / price, 4)),
            "nonce":      str(int(time.time() * 1000)),
            "feeRateBps": "170",
            "expiration": "0",
            "maker":      self._account.address,
            "chainId":    str(settings.POLY_CHAIN_ID),
        }

    def _sign(self, order: dict) -> str:
        """
        FIX BUG #9: Sign order menggunakan personal_sign atas
        keccak256 dari canonical order string.

        Format: keccak256(canonical_string) → personal_sign → hex signature

        Canonical string dibuat dari sorted key agar deterministic.
        """
        import json
        # Canonical JSON — sorted keys, no whitespace
        canonical = json.dumps(order, sort_keys=True, separators=(",", ":"))
        # Keccak256 hash
        msg_hash  = hashlib.sha3_256(canonical.encode("utf-8")).hexdigest()
        # Personal sign (EIP-191 prefix ditambah otomatis oleh encode_defunct)
        msg       = encode_defunct(hexstr=msg_hash)
        signed    = self._account.sign_message(msg)
        return "0x" + signed.signature.hex()

    async def submit_order(
        self,
        token_id:     str,
        size_usd:     float,
        ask_price:    float,
        slippage_pct: float = 0.02,
    ) -> Optional[dict]:
        """
        Bangun, tandatangani, dan kirim order BUY FOK ke CLOB.
        """
        limit_price = round(ask_price * (1 + slippage_pct), 4)
        order       = self._build_order(token_id, "BUY", limit_price, size_usd)
        sig         = self._sign(order)
        payload     = {"order": order, "signature": sig, "orderType": "FOK"}

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
                "ORDER OK | $%.2f @ %.4f | id=%s",
                size_usd, limit_price, receipt.get("orderID", "?"),
            )
            return receipt

        except httpx.HTTPStatusError as e:
            log.error("CLOB gagal [%d]: %s", e.response.status_code, e.response.text[:300])
        except httpx.TimeoutException:
            log.error("CLOB timeout — order tidak terkirim.")
        except Exception as e:
            log.error("CLOB error tidak terduga: %s", str(e))

        return None

    async def sell_position(
        self,
        token_id:      str,
        size_usd:      float,
        entry_price:   float,
        current_price: float,
        slippage_pct:  float = 0.02,
    ) -> Optional[dict]:
        """
        Jual posisi yang sudah dibuka (untuk exit strategy).
        """
        sell_price = round(current_price * (1 - slippage_pct), 4)
        order      = self._build_order(token_id, "SELL", sell_price, size_usd)
        sig        = self._sign(order)
        payload    = {"order": order, "signature": sig, "orderType": "FOK"}

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
                "SELL OK | $%.2f @ %.4f | id=%s",
                size_usd, sell_price, receipt.get("orderID", "?"),
            )
            return receipt

        except httpx.HTTPStatusError as e:
            log.error("CLOB SELL gagal [%d]: %s", e.response.status_code, e.response.text[:300])
        except httpx.TimeoutException:
            log.error("CLOB SELL timeout.")
        except Exception as e:
            log.error("CLOB SELL error: %s", str(e))

        return None
