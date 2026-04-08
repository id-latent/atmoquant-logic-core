# ==============================================================================
# market/gamma_client.py — v2.0.0 (Sesi 4)
# ==============================================================================
"""
AQL Gamma Client — Lapisan API Polymarket + Eksekusi CLOB

Perubahan dari v1.x:
  1. Discovery terpadu: /events?tag=temperature (MULTI_OUTCOME)
                      + /markets?tag=temperature (BINARY) dengan dedup
  2. Klasifikasi jenis market: MULTI_OUTCOME | BINARY_ABOVE | BINARY_BELOW
                               | BINARY_RANGE | BINARY_UNKNOWN
  3. Dataclass PolyMarket diperkaya: market_type, outcomes, htc (pre-computed)
  4. Filter kata kunci negatif — menghilangkan false positive gempa/kripto
  5. Filter API berbasis tag: mengurangi 6000+ menjadi ~50 market yang di-fetch
  6. Batas maksimal 5 halaman pada pagination binary (anti-overfetch)
  7. Jitter request + rotasi user-agent (anti-deteksi bot)
  8. CLOB mendukung submit token_id multi-outcome secara langsung
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

from config.settings import settings

log = logging.getLogger("aql.gamma")

# ── Konstanta Jenis Market ─────────────────────────────────────────────────────
MULTI_OUTCOME  = "MULTI_OUTCOME"   # Market multi-pilihan (11 outcome per event suhu)
BINARY_ABOVE   = "BINARY_ABOVE"    # "Apakah suhu akan melebihi X?"
BINARY_BELOW   = "BINARY_BELOW"    # "Apakah suhu akan di bawah X?"
BINARY_RANGE   = "BINARY_RANGE"    # "Apakah suhu akan antara X dan Y?"
BINARY_UNKNOWN = "BINARY_UNKNOWN"  # Binary tapi pola pertanyaan tidak dikenali

# ── Pool User-Agent ────────────────────────────────────────────────────────────
# Dirotasi tiap request agar tidak terdeteksi sebagai bot otomatis oleh Polymarket
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]


def _random_ua() -> str:
    """Ambil user-agent secara acak dari pool."""
    return random.choice(_USER_AGENTS)


# ── Wadah Data Market ──────────────────────────────────────────────────────────
@dataclass
class PolyMarket:
    # Identitas utama market
    market_id:     str
    condition_id:  str
    question:      str    # Pertanyaan market seperti yang tertulis di Polymarket
    description:   str
    end_date_iso:  str    # Tanggal resolusi dalam format ISO 8601
    market_type:   str    # MULTI_OUTCOME | BINARY_ABOVE | BINARY_BELOW | BINARY_RANGE
    htc:           float  # Jam tersisa hingga tutup — dihitung saat discovery, gunakan ini
    liquidity_usd: float  # Total likuiditas dalam USD
    volume_usd:    float  # Total volume perdagangan dalam USD
    active:        bool
    url:           str    # URL langsung ke market di Polymarket

    # Field khusus market binary (diisi jika market_type != MULTI_OUTCOME)
    yes_token_id: str   = ""   # Token ID untuk membeli posisi YES
    no_token_id:  str   = ""   # Token ID untuk membeli posisi NO
    best_bid:     float = 0.5  # Harga beli terbaik saat ini (buyer tawaran tertinggi)
    best_ask:     float = 0.5  # Harga jual terbaik saat ini (seller tawaran terendah)
    mid_price:    float = 0.5  # Harga tengah = (bid + ask) / 2

    # Field khusus market multi-outcome (diisi jika market_type == MULTI_OUTCOME)
    # Format tiap item: {"label": str, "token_id": str, "price": float,
    #                    "bid": float, "ask": float, "condition_id": str}
    outcomes: list[dict] = field(default_factory=list)

    # Diisi oleh engine.py setelah nama kota diekstrak dari pertanyaan
    city_name: Optional[str] = None
    event_id:  Optional[str] = None

    @property
    def hours_to_close(self) -> float:
        """Alias kompatibilitas. Lebih baik gunakan .htc (sudah dihitung, tidak re-parse)."""
        return self.htc


# ── Filter Kata Kunci Suhu ─────────────────────────────────────────────────────
# CATATAN: Tag "weather" dan "climate" sengaja TIDAK dimasukkan di sini karena
# terlalu luas — market gempa bumi juga bisa punya tag tersebut dan menyebabkan
# false positive. Keduanya ditangani secara terpisah di _is_temperature_market().
_TEMP_TAGS = frozenset(["temperature", "meteorology"])

# Kata kunci yang mengkonfirmasi bahwa market membahas suhu udara
_TEMP_KEYWORDS = (
    "temperature", "high temp", "low temp", "degrees fahrenheit",
    "degrees celsius", "fahrenheit", "celsius", "°f", "°c",
    "thermometer", "heat index", "record high", "record low",
    "will the high", "will the low", "daily high", "daily low",
    "max temp", "min temp", "average temp", "mean temp",
    "high temperature", "low temperature",
)

# Penolak keras — market yang mengandung salah satu kata ini PASTI bukan
# market suhu, meskipun memiliki tag "weather" atau "climate"
_NEGATIVE_KEYWORDS = (
    "earthquake", "seismic", "quake", "magnitude", "richter", "tremor",
    "how many 5", "how many 6", "how many 4",   # market hitung jumlah gempa
    "hurricane category", "tornado", "flood stage", "storm surge",
    "rainfall", "precipitation", "inches of rain",
    "bitcoin", "crypto", "eth ", "btc", "solana",
    "election", "vote", "president", "senate", "congress",
    "gdp", "inflation", "interest rate", "fed rate",
    "wildfire acres", "fire weather watch",
    "how many storms", "how many hurricanes",
)


def _is_temperature_market(raw: dict) -> bool:
    """
    Mengembalikan True hanya untuk market suhu/cuaca yang genuine.

    Strategi 4 lapis:
      1. Cek kata kunci negatif — jika ada, langsung tolak (prioritas absolut)
      2. Tag temperature/meteorology + minimal satu keyword suhu → terima
      3. Tag weather/climate + keyword kuat (°F/°C/temperature) → terima
      4. Keyword suhu saja tanpa tag → terima (fallback)
    """
    question    = (raw.get("question") or "").lower()
    description = (raw.get("description") or "").lower()
    tags        = {t.lower() for t in (raw.get("tags") or [])}
    combined    = question + " " + description

    # Lapis 1: penolak keras menang atas segalanya
    if any(neg in combined for neg in _NEGATIVE_KEYWORDS):
        return False

    # Lapis 2: tag eksplisit temperature/meteorology + konfirmasi keyword
    if _TEMP_TAGS & tags:
        return any(kw in combined for kw in _TEMP_KEYWORDS)

    # Lapis 3: tag weather/climate HANYA dengan konfirmasi keyword kuat
    # Tidak cukup hanya kata "degrees" — harus ada °F/°C atau "temperature"
    if {"weather", "climate"} & tags:
        strong = ("°f", "°c", "temperature", "fahrenheit", "celsius",
                  "will the high", "will the low")
        return any(kw in combined for kw in strong)

    # Lapis 4: keyword suhu saja, tanpa tag apapun
    return any(kw in combined for kw in _TEMP_KEYWORDS)


def _classify_binary(question: str) -> str:
    """Klasifikasikan market binary suhu menjadi ABOVE / BELOW / RANGE / UNKNOWN."""
    q = question.lower()
    # Tipe "apakah suhu MELEBIHI X?" — contoh: "Will the high exceed 90°F?"
    if any(w in q for w in ("above", "exceed", "over ", "at least",
                             "reach", "or higher", "or more")):
        return BINARY_ABOVE
    # Tipe "apakah suhu DI BAWAH X?" — contoh: "Will the low be below 32°F?"
    if any(w in q for w in ("below", "under ", "less than", "not exceed",
                             "or lower", "or less")):
        return BINARY_BELOW
    # Tipe "apakah suhu ANTARA X dan Y?" — contoh: "Will the high be between 85–90°F?"
    if any(w in q for w in ("between", " range", " to ", "–", "and ")):
        return BINARY_RANGE
    return BINARY_UNKNOWN


def _compute_htc(end_date_iso: str) -> float:
    """
    Hitung jam tersisa hingga market ditutup/diresolusi.
    Mengembalikan 0.0 jika format tanggal tidak valid.
    """
    try:
        end   = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        delta = end - datetime.now(timezone.utc)
        return max(delta.total_seconds() / 3600, 0.0)
    except Exception:
        return 0.0


# ── Klien REST Gamma ───────────────────────────────────────────────────────────
class GammaClient:
    """
    Wrapper async tipis untuk Polymarket Gamma API — discovery terpadu v2.0.0.

    Alur discovery:
      1. GET /events?tag=temperature  → event MULTI_OUTCOME (satu event = 11 outcome)
      2. GET /markets?tag=temperature → market BINARY individual (sudah dedup dengan step 1)
      3. Filter berdasarkan likuiditas minimum + jendela jam tutup
      4. Golden Hour Guard (jendela masuk optimal per region) diterapkan
         di engine.py, bukan di sini.
    """

    BASE = settings.POLY_GAMMA_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http = http_client

    # ── GET Internal dengan Jitter + Rotasi UA ─────────────────────────────
    async def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        jitter: bool = True,
    ) -> list | dict:
        """
        Kirim GET request ke Gamma API.
        jitter=True: tambahkan delay acak 50-350ms antar request
                     agar pola request tidak terlihat seperti bot.
        """
        if jitter:
            await asyncio.sleep(random.uniform(0.05, 0.35))
        resp = await self._http.get(
            f"{self.BASE}{path}",
            params=params,
            headers={"User-Agent": _random_ua()},
            timeout=20.0,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Langkah 1: Events → MULTI_OUTCOME ─────────────────────────────────
    async def _discover_events(self) -> tuple[list[PolyMarket], set[str]]:
        """
        Ambil event suhu dari endpoint /events Gamma API.

        Mengembalikan tuple:
          - list[PolyMarket]: satu objek per event, berisi semua outcome-nya
          - set[str]: kumpulan condition_id yang sudah diproses (untuk dedup step 2)
        """
        markets: list[PolyMarket] = []
        seen: set[str] = set()

        try:
            page = await self._get(
                "/events",
                params={
                    "active":    "true",
                    "closed":    "false",
                    "tag":       "temperature",
                    "limit":     50,
                    "order":     "volumeNum",   # urutkan dari volume terbesar
                    "ascending": "false",
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

            # Validasi: event harus lolos classifier suhu sebelum diproses
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
            primary_cid = ""  # condition_id pertama dipakai sebagai primary

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

                # Simpan condition_id pertama yang valid sebagai primary
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
                seen.add(cid)  # Tandai agar tidak di-fetch ulang di langkah 2

            if not outcomes or not primary_cid:
                continue

            markets.append(PolyMarket(
                market_id     = event_id,
                condition_id  = primary_cid,
                question      = question,
                description   = description,
                end_date_iso  = end_date,
                market_type   = MULTI_OUTCOME,
                htc           = htc,
                liquidity_usd = total_liquidity,
                volume_usd    = total_volume,
                active        = True,
                url           = f"https://polymarket.com/event/{event_slug}",
                outcomes      = outcomes,
                event_id      = event_id,
            ))

        return markets, seen

    # ── Langkah 2: Markets → BINARY (sudah dedup) ──────────────────────────
    async def _discover_binary_markets(
        self,
        exclude_cids: set[str],
    ) -> list[PolyMarket]:
        """
        Ambil market binary suhu individual dari endpoint /markets.

        Perbedaan dari v1.x:
          - Pakai filter ?tag=temperature sehingga API hanya mengembalikan ~50-100 market
          - Batas keras 5 halaman (500 market) — v1.x bisa mencapai 6000+ market
          - Market yang condition_id-nya sudah ada di exclude_cids dilewati (dedup)
        """
        markets: list[PolyMarket] = []
        offset, limit, MAX_PAGES = 0, 100, 5

        while (offset // limit) < MAX_PAGES:
            try:
                page = await self._get(
                    "/markets",
                    params={
                        "active":    "true",
                        "closed":    "false",
                        "tag":       "temperature",  # filter di sisi API
                        "limit":     limit,
                        "offset":    offset,
                        "order":     "volumeNum",
                        "ascending": "false",
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

                # Lewati market yang sudah diambil dari /events (dedup)
                if cid in exclude_cids:
                    continue

                # Lewati jika tidak lolos classifier suhu
                if not _is_temperature_market(raw):
                    continue

                tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
                if len(tokens) < 2:
                    continue  # Market binary wajib punya 2 token (YES dan NO)

                def _tok(t: object) -> str:
                    """Ekstrak token_id baik dari string maupun dict."""
                    return t if isinstance(t, str) else t.get("token_id", "")

                bid      = float(raw.get("bestBid") or 0.5)
                ask      = float(raw.get("bestAsk") or 0.5)
                end_date = raw.get("endDateIso") or raw.get("endDate") or ""
                q        = raw.get("question") or ""
                slug     = raw.get("slug") or raw.get("id") or ""

                markets.append(PolyMarket(
                    market_id     = str(raw.get("id") or ""),
                    condition_id  = cid,
                    question      = q,
                    description   = raw.get("description") or "",
                    end_date_iso  = end_date,
                    market_type   = _classify_binary(q),
                    htc           = _compute_htc(end_date),
                    liquidity_usd = float(raw.get("liquidityNum") or raw.get("liquidity") or 0),
                    volume_usd    = float(raw.get("volumeNum") or raw.get("volume") or 0),
                    active        = True,
                    url           = f"https://polymarket.com/event/{slug}",
                    yes_token_id  = _tok(tokens[0]),
                    no_token_id   = _tok(tokens[1]),
                    best_bid      = round(bid, 4),
                    best_ask      = round(ask, 4),
                    mid_price     = round((bid + ask) / 2.0, 4),
                ))
                exclude_cids.add(cid)

            if len(batch) < limit:
                break  # Halaman terakhir, tidak perlu lanjut
            offset += limit

        return markets

    # ── Titik Masuk Utama (dipanggil dari engine.py) ────────────────────────
    async def discover_temperature_markets(
        self,
        min_liquidity_usd: float = 500.0,
        hours_before_close_min: float = 1.0,
        hours_before_close_max: float = 168.0,
    ) -> list[PolyMarket]:
        """
        Discovery market suhu terpadu v2.0.0.

        Jendela htc sengaja lebar (1-168 jam) karena Golden Hour Guard
        per-region akan menyaring lebih lanjut di engine.py.

        Mengembalikan list PolyMarket diurutkan dari likuiditas terbesar
        (market paling aktif diprioritaskan untuk dianalisis lebih dulu).
        """
        log.info("Discovery terpadu v2.0.0 — mulai")

        # Langkah 1: ambil event MULTI_OUTCOME dari /events
        event_markets, seen_cids = await self._discover_events()
        # Langkah 2: ambil market BINARY dari /markets, skip yang sudah di seen_cids
        binary_markets           = await self._discover_binary_markets(seen_cids)

        all_markets = event_markets + binary_markets
        log.info(
            "Total mentah: %d MULTI_OUTCOME + %d BINARY = %d",
            len(event_markets), len(binary_markets), len(all_markets),
        )

        # Filter: buang market di bawah likuiditas minimum atau di luar jendela htc
        filtered: list[PolyMarket] = []
        for pm in all_markets:
            if pm.liquidity_usd < min_liquidity_usd:
                continue
            if not (hours_before_close_min <= pm.htc <= hours_before_close_max):
                continue
            filtered.append(pm)
            log.info(
                "[KANDIDAT] %s | %.1fh | $%.0f | %s",
                pm.market_type, pm.htc, pm.liquidity_usd, pm.question[:70],
            )

        # Urutkan dari likuiditas terbesar (lebih likuid = lebih mudah masuk/keluar)
        filtered.sort(key=lambda m: m.liquidity_usd, reverse=True)
        log.info("Kandidat setelah filter: %d", len(filtered))
        return filtered

    # ── Refresh Harga ───────────────────────────────────────────────────────
    async def refresh_market_price(self, condition_id: str) -> Optional[float]:
        """
        Ambil harga tengah terkini untuk satu market.
        Digunakan saat cycle exit_strategy untuk cek apakah SL/TP sudah tercapai.
        Mengembalikan None jika request gagal.
        """
        try:
            data = await self._get(f"/markets/{condition_id}", jitter=False)
            bid  = float(data.get("bestBid") or 0.5)
            ask  = float(data.get("bestAsk") or 0.5)
            return round((bid + ask) / 2.0, 4)
        except Exception as e:
            log.error("Refresh harga gagal [%s]: %s", condition_id, e)
            return None

    async def refresh_outcome_prices(self, pm: PolyMarket) -> PolyMarket:
        """
        Perbarui harga semua outcome dalam market secara langsung (in-place).
          - MULTI_OUTCOME: refresh setiap outcome satu per satu via condition_id-nya
          - BINARY        : hanya refresh mid_price dari condition_id utama
        """
        if pm.market_type != MULTI_OUTCOME:
            # Market binary — cukup refresh satu harga tengah
            price = await self.refresh_market_price(pm.condition_id)
            if price is not None:
                pm.mid_price = price
            return pm

        # Market multi-outcome — refresh tiap outcome satu per satu
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
    Menandatangani dan mengirim order limit FOK ke Polymarket CLOB (Central Limit Order Book).

    FOK = Fill-or-Kill: order harus langsung terisi penuh, atau dibatalkan.
    Tidak ada order yang menggantung di order book.

    Mendukung:
      - Market binary     : side="YES" atau "NO"
      - Market multi-outcome : kirim outcome_token_id outcome yang dipilih
    """

    CLOB_BASE = settings.POLY_CLOB_BASE

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http    = http_client
        self._account = Account.from_key(settings.POLY_PRIVATE_KEY)
        log.info("CLOB Executor siap | Penanda tangan: %s", self._account.address)

    def _build_order(self, token_id: str, price: float, size_usd: float) -> dict:
        """
        Bangun struktur data order untuk dikirim ke CLOB API.
        Jumlah kontrak = size_usd / harga_limit (karena harga = probabilitas dalam $).
        """
        return {
            "tokenID":    token_id,
            "side":       "BUY",
            "price":      str(round(price, 4)),
            "size":       str(round(size_usd / price, 4)),  # USD → jumlah kontrak
            "nonce":      int(time.time() * 1000),          # timestamp unik, cegah replay attack
            "feeRateBps": "170",   # biaya transaksi 1.7% (basis points)
            "expiration": "0",     # 0 = GTC (Good Till Cancelled)
            "maker":      self._account.address,
            "chainId":    settings.POLY_CHAIN_ID,
        }

    def _sign(self, order: dict) -> str:
        """
        Tanda tangani order menggunakan private key wallet Polygon.

        Proses:
          1. Serialisasi order dict ke string yang deterministik (sorted)
          2. Hash dengan SHA3-256
          3. Wrap dalam format EIP-191 (eth_sign personal message)
          4. Tanda tangani dengan private key → hasilkan signature hex
        """
        order_str = str(sorted(order.items()))
        h   = hashlib.sha3_256(order_str.encode()).hexdigest()
        msg = encode_defunct(hexstr=h)
        return self._account.sign_message(msg).signature.hex()

    async def submit_order(
        self,
        market: PolyMarket,
        side: str,
        size_usd: float,
        slippage_pct: float = 0.02,
        outcome_token_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Bangun, tandatangani, dan kirim order FOK ke CLOB.

        Parameter:
          side             : "YES" atau "NO" untuk market binary.
                             Untuk MULTI_OUTCOME, isi outcome_token_id dan
                             side bisa diisi label outcome (hanya untuk logging).
          outcome_token_id : token_id outcome spesifik (khusus MULTI_OUTCOME).
          slippage_pct     : toleransi slippage maksimum dari harga ask (default 2%).
                             Semakin besar, semakin mungkin terisi tapi harga lebih buruk.

        Mengembalikan dict receipt dari CLOB jika berhasil, atau None jika gagal.
        """
        # Tentukan token_id dan harga berdasarkan jenis market dan sisi order
        if outcome_token_id:
            # Multi-outcome: cari harga ask dari outcome yang token_id-nya cocok
            token_id = outcome_token_id
            price    = next(
                (o["ask"] for o in market.outcomes if o["token_id"] == token_id),
                0.5,  # fallback jika outcome tidak ditemukan
            )
        elif side == "YES":
            token_id = market.yes_token_id
            price    = market.best_ask           # beli YES: bayar harga ask
        else:
            token_id = market.no_token_id
            price    = 1.0 - market.best_bid     # beli NO: harganya = 1 - bid YES

        # Tambah slippage agar order lebih mungkin terisi di pasar yang bergerak cepat
        limit_price = round(price * (1 + slippage_pct), 4)
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
            log.error("CLOB gagal [%d]: %s", e.response.status_code, e.response.text[:300])
        except httpx.TimeoutException:
            log.error("CLOB timeout — order tidak terkirim.")
        except Exception as e:
            log.error("CLOB error tidak terduga: %s", str(e))
        return None
