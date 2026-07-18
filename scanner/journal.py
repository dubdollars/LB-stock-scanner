"""
Trade Journal v2 — position model with adds/partial exits.

Each Trade represents ONE position (not one transaction). Inside that position
is a list of Transaction records (BUYs for the starter + pyramid adds, SELLs
for partial exits and final close). This matches how real swing trading
actually works.

Storage:
    data/journal.json — single JSON file with a list of trade records.
    Atomic writes prevent half-written corruption on interrupted saves.

Backward compatibility:
    The v1 schema stored flat trades (entry_price/stop_price/shares/exit_price
    as top-level fields). load_trades() auto-migrates v1 records to the new
    transactions[] format so existing journal entries survive the upgrade.

Stop price:
    Now OPTIONAL. If stop_price == 0, no R-multiple is computed (shown as "—"
    in the UI). P&L still computes normally. This lets you log trades where
    you didn't predefine a stop (e.g. thesis-driven holds you'll exit on
    discretion).

Expectancy math:
    Realized P&L uses weighted-average-cost (avg_entry_price × shares_sold).
    R-multiple = realized_pnl / initial_risk_dollars where initial_risk is
    (starter_price - stop_price) × starter_shares. Only computed when stop > 0.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# Single source of truth for setup tags — used by the form dropdown AND
# the expectancy dashboard to group trades.
SETUP_TAGS = [
    "VCP-A",
    "VCP-B",
    "Breakout",
    "Power Play",
    "Pullback to 50MA",
    "Coil-in-progress",     # anticipating EARLY mode
    "RS Divergence",        # anticipating EARLY mode
    "Stage 1->2 Transition",  # anticipating EARLY mode
    "Earnings Inflection",  # anticipating EARLY mode
    "Other",
]

# Single source of truth for trade SOURCES — where the candidate originated.
# Distinct from SETUP_TAGS (which describes the chart pattern). Source tracks
# which scan/filter surfaced the ticker so we can later answer questions like
# "what's my hit rate on BUY ZONE trades vs Top Stocks intersections?".
SOURCE_OPTIONS = [
    "Top Stocks",
    "Momentum - BUY ZONE",
    "Momentum - Power Play",
    "Momentum - Other Filter",
    "Quality",
    "Mean Reversion",
    "Social",
    "Rotation",
    "Manual / Eyeball",
    "Other",
]

JOURNAL_FILE = (
    Path(__file__).resolve().parent.parent / "data" / "journal.json"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_dir() -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic write: temp file + os.replace. Prevents partial writes."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Data model — Transaction + Trade
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    """One buy or sell event inside a trade. Dates are 'YYYY-MM-DD' strings."""
    kind: str = "BUY"          # "BUY" or "SELL"
    date: str = ""
    price: float = 0.0
    shares: int = 0


@dataclass
class StopUpdate:
    """One stop-adjustment event. Logs every time the user trails their stop
    so the journal can show the progression: entry stop → breakeven → first
    pullback low → etc. Useful for post-trade analysis ('did I tighten too
    early?' / 'did I get stopped on noise?')."""
    date: str = ""              # YYYY-MM-DD
    old_stop: float = 0.0
    new_stop: float = 0.0
    note: str = ""              # optional rationale ("under prior swing low", etc.)


@dataclass
class Trade:
    """One position. Multiple transactions accumulate inside it.

    The first transaction (transactions[0]) is the "starter" — the original
    entry. Additional BUYs are pyramid adds. SELLs are partial exits (with
    the final SELL being the close).

    Stop fields:
      stop_price: the CURRENT (live, mutable) stop. Updated by update_stop().
      initial_stop_price: the ORIGINAL stop set at entry. Immutable. Used as
        the R-multiple anchor — raising your stop to lock profit shouldn't
        retroactively shrink your initial-risk basis (that would inflate R
        and make the expectancy math meaningless).

    All money in dollars. Dates are 'YYYY-MM-DD' strings.
    """
    id: str = ""
    ticker: str = ""
    setup_tag: str = "Other"
    source: str = "Other"             # which scan/filter surfaced this trade
    stop_price: float = 0.0           # CURRENT stop (mutable; updated as you trail)
    initial_stop_price: float = 0.0   # IMMUTABLE anchor for R-multiple math
    notes: str = ""
    created_at: str = ""              # ISO timestamp when row was first written
    transactions: list = field(default_factory=list)
    stop_history: list = field(default_factory=list)  # list[StopUpdate]

    # ---- Constructors ----

    @classmethod
    def new(cls, ticker: str, setup_tag: str, entry_date: str,
            entry_price: float, stop_price: float, shares: int,
            notes: str = "", source: str = "Other") -> "Trade":
        """Construct a fresh trade with one BUY transaction (the starter).

        Sets BOTH stop_price (current/mutable) AND initial_stop_price (immutable
        R-multiple anchor) to the same value at entry.

        `source` is validated against SOURCE_OPTIONS (falls back to "Other"
        if an unknown value is passed) and tracks which scan/filter the trade
        came from — separate from setup_tag, which describes the chart pattern.
        """
        sp = round(float(stop_price), 4) if stop_price else 0.0
        t = cls(
            id=str(uuid.uuid4())[:8],
            ticker=ticker.strip().upper(),
            setup_tag=setup_tag if setup_tag in SETUP_TAGS else "Other",
            source=source if source in SOURCE_OPTIONS else "Other",
            stop_price=sp,
            initial_stop_price=sp,
            notes=notes.strip(),
            created_at=datetime.utcnow().isoformat() + "Z",
            transactions=[
                Transaction(
                    kind="BUY",
                    date=entry_date,
                    price=round(float(entry_price), 4),
                    shares=int(shares),
                )
            ],
        )
        return t

    # ---- Mutators ----

    def add_buy(self, date: str, price: float, shares: int) -> None:
        """Append a pyramid-add BUY transaction."""
        self.transactions.append(Transaction(
            kind="BUY",
            date=date,
            price=round(float(price), 4),
            shares=int(shares),
        ))

    def add_sell(self, date: str, price: float, shares: int) -> tuple[bool, str]:
        """Append a SELL transaction. Returns (ok, error_message).

        Rejects sells larger than current net_shares (can't sell what you
        don't have). Caller should check the return.
        """
        sh = int(shares)
        if sh <= 0:
            return False, "Shares to sell must be > 0"
        if sh > self.net_shares:
            return False, f"Cannot sell {sh} shares — only {self.net_shares} held"
        self.transactions.append(Transaction(
            kind="SELL",
            date=date,
            price=round(float(price), 4),
            shares=sh,
        ))
        return True, ""

    def append_note(self, action: str, text: str, date: str = "") -> None:
        """Append a timestamped, action-tagged line to self.notes.

        Used by Add Shares / Sell Shares / Move Stop to grow the position's
        Notes into a running narrative. No-op if text is empty/whitespace.

        Format: '[14Jun2026 · ADD 50 @ $43.20] confirmation on volume'
        """
        if not text or not text.strip():
            return
        # Format date as DDmmmYYYY to match the UI's date display
        date_str = ""
        if date:
            try:
                d = datetime.strptime(str(date)[:10], "%Y-%m-%d")
                date_str = d.strftime("%d%b%Y")
            except Exception:
                date_str = str(date)[:10]
        prefix = f"[{date_str} · {action}]" if date_str else f"[{action}]"
        line = f"{prefix} {text.strip()}"
        self.notes = (self.notes.rstrip() + "\n" + line) if self.notes else line

    def update_stop(self, date: str, new_stop: float, note: str = "") -> tuple[bool, str]:
        """Move the live stop to a new level and log the change.

        Rules:
          - Position must still be open or partial (net_shares > 0). No stop
            updates on closed positions.
          - new_stop must be > 0. Use 0 / no-stop only at trade construction.
          - initial_stop_price is NOT touched. R-multiple math stays anchored
            to the original risk so that a trailing-stop tighten doesn't
            retroactively inflate the realized-R reading.

        Returns (ok, error_message). Caller should check the return.
        """
        try:
            new_sp = round(float(new_stop), 4)
        except (TypeError, ValueError):
            return False, "Invalid stop value"
        if new_sp <= 0:
            return False, "New stop must be > 0 (use 0 only at trade entry to mean 'no stop')"
        if self.net_shares == 0:
            return False, "Can't update stop on a closed position"
        old_sp = self.stop_price
        # Backfill initial_stop_price for legacy trades that don't have it
        # (loaded via _trade_from_row should already cover this, but belt-and-braces)
        if not self.initial_stop_price or self.initial_stop_price <= 0:
            self.initial_stop_price = old_sp if old_sp > 0 else new_sp
        self.stop_history.append(StopUpdate(
            date=date,
            old_stop=round(float(old_sp), 4),
            new_stop=new_sp,
            note=note.strip(),
        ))
        self.stop_price = new_sp
        return True, ""

    # ---- Computed properties ----

    @property
    def buys(self) -> list:
        return [t for t in self.transactions if t.kind == "BUY"]

    @property
    def sells(self) -> list:
        return [t for t in self.transactions if t.kind == "SELL"]

    @property
    def total_shares_bought(self) -> int:
        return sum(t.shares for t in self.buys)

    @property
    def total_shares_sold(self) -> int:
        return sum(t.shares for t in self.sells)

    @property
    def net_shares(self) -> int:
        return self.total_shares_bought - self.total_shares_sold

    @property
    def avg_entry_price(self) -> float:
        """Weighted-average price across all BUYs."""
        if self.total_shares_bought == 0:
            return 0.0
        total_cost = sum(t.price * t.shares for t in self.buys)
        return round(total_cost / self.total_shares_bought, 4)

    @property
    def avg_exit_price(self) -> float:
        """Weighted-average price across all SELLs."""
        if self.total_shares_sold == 0:
            return 0.0
        total_proceeds = sum(t.price * t.shares for t in self.sells)
        return round(total_proceeds / self.total_shares_sold, 4)

    @property
    def realized_pnl(self) -> float:
        """Realized P&L using weighted-average-cost method.

        proceeds - (avg_entry × shares_sold)
        Only counts the portion that has actually been SOLD.
        """
        if self.total_shares_sold == 0:
            return 0.0
        cost_of_sold = self.avg_entry_price * self.total_shares_sold
        proceeds = sum(t.price * t.shares for t in self.sells)
        return round(proceeds - cost_of_sold, 2)

    @property
    def initial_risk_dollars(self) -> float:
        """Risk anchored to the ORIGINAL stop: (starter_price - initial_stop) × starter_shares.

        Uses initial_stop_price (not the current stop) so that trailing the
        stop up doesn't shrink the risk basis and inflate realized R-multiples.
        Falls back to stop_price for legacy trades that don't have
        initial_stop_price recorded yet.

        Returns 0 if no stop was set (or stop >= entry).
        """
        if not self.buys:
            return 0.0
        starter = self.buys[0]
        anchor = self.initial_stop_price if self.initial_stop_price > 0 else self.stop_price
        if anchor <= 0 or anchor >= starter.price:
            return 0.0
        return round((starter.price - anchor) * starter.shares, 2)

    @property
    def realized_r(self) -> Optional[float]:
        """Realized R-multiple on the shares ACTUALLY SOLD.

        Formula: realized_pnl / (risk_per_share × total_shares_sold)
        where risk_per_share = initial_risk_dollars / starter_shares.

        This is the standard Van Tharp / Minervini / O'Neil interpretation —
        R measures *per-unit-of-risk exit quality*, not a position-completion
        ratio. Partial exits report their true per-share R immediately rather
        than a diluted "you've earned 50% of your max R so far" reading.

        Example (the user-reported bug 2026-06-25):
          KRYS — 8 starter shares @ $321.98, stop $308 → risk per share $13.98.
          Sold 4 shares @ $361.05 → realized PnL $156.28.

          OLD math (wrong):  156.28 / 111.84 (risk on all 8) = +1.40R
          NEW math (right):  156.28 / (13.98 × 4)            = +2.79R
                             (which equals (361.05 − 321.98) / 13.98 per share)

        When the remaining 4 sell at, say, $400:
          Total realized = 156.28 + 4×(400 − 321.98) = $468.36
          Risk on all sold = 8 × $13.98 = $111.84
          R = 468.36 / 111.84 = +4.19 (weighted average of the two exits)

        Returns None when no stop was set or nothing's been sold yet.
        """
        if not self.buys:
            return None
        starter_shares = self.buys[0].shares
        if starter_shares == 0 or self.total_shares_sold == 0:
            return None
        risk_total = self.initial_risk_dollars
        if risk_total <= 0:
            return None
        risk_per_share = risk_total / starter_shares
        risk_on_sold_shares = risk_per_share * self.total_shares_sold
        if risk_on_sold_shares <= 0:
            return None
        return round(self.realized_pnl / risk_on_sold_shares, 3)

    @property
    def status(self) -> str:
        """open (nothing sold) / partial (some sold, some held) / closed (all sold)."""
        if self.total_shares_sold == 0:
            return "open"
        if self.net_shares == 0:
            return "closed"
        return "partial"

    @property
    def cost_basis(self) -> float:
        """Total cost of shares still held (avg_entry × net_shares)."""
        return round(self.avg_entry_price * self.net_shares, 2)

    @property
    def entry_date(self) -> str:
        """Date of the starter transaction (used as the 'opened' date)."""
        return self.buys[0].date if self.buys else ""

    @property
    def last_exit_date(self) -> str:
        """Date of the most recent SELL (used as 'closed' date for closed trades)."""
        return self.sells[-1].date if self.sells else ""

    @property
    def days_held(self) -> int:
        """Calendar days since the starter (first BUY) transaction.

        - OPEN / PARTIAL positions: today − entry_date (still accruing)
        - CLOSED positions: last_exit_date − entry_date (total trade duration, frozen)

        Returns 0 for trades with no entry date or unparseable dates.
        Uses calendar days (not trading days) for simplicity — easy to reason about
        and matches how most journals/brokerages report holding period.
        """
        if not self.buys:
            return 0
        entry_str = self.entry_date
        if not entry_str:
            return 0
        try:
            from datetime import date
            entry_d = datetime.strptime(entry_str[:10], "%Y-%m-%d").date()
            if self.status == "closed":
                last_exit = self.last_exit_date
                if last_exit:
                    end_d = datetime.strptime(last_exit[:10], "%Y-%m-%d").date()
                else:
                    end_d = date.today()
            else:
                end_d = date.today()
            return max(0, (end_d - entry_d).days)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _migrate_v1_record(row: dict) -> dict:
    """Convert a v1 flat trade record into the v2 position format.

    v1 schema had top-level entry_price/shares/stop_price/exit_price.
    v2 schema has a transactions list. If the row already has transactions,
    return it unchanged. Otherwise, build transactions from the v1 fields.
    """
    if "transactions" in row and row["transactions"]:
        return row  # already v2

    # Build the starter BUY from v1 fields
    txs = []
    if row.get("entry_price") and row.get("shares"):
        txs.append({
            "kind": "BUY",
            "date": row.get("entry_date", ""),
            "price": float(row.get("entry_price", 0)),
            "shares": int(row.get("shares", 0)),
        })
    # If v1 had an exit, add that as a SELL
    if row.get("status") == "closed" and row.get("exit_price") and row.get("shares"):
        txs.append({
            "kind": "SELL",
            "date": row.get("exit_date", ""),
            "price": float(row.get("exit_price", 0)),
            "shares": int(row.get("shares", 0)),
        })

    # Strip v1-only fields and add transactions
    keep_fields = {"id", "ticker", "setup_tag", "stop_price", "notes", "created_at"}
    migrated = {k: v for k, v in row.items() if k in keep_fields}
    migrated["transactions"] = txs
    return migrated


def _trade_from_row(row: dict) -> Trade:
    """Build a Trade from a JSON row, handling Transaction + StopUpdate conversion.

    Backwards-compat: trades saved before initial_stop_price + stop_history
    existed (anything before 2026-06-17) get those fields backfilled:
      - initial_stop_price defaults to stop_price (so R-multiple math keeps
        the same anchor as before)
      - stop_history defaults to empty list
    """
    row = _migrate_v1_record(row)
    raw_txs = row.pop("transactions", [])
    txs = [Transaction(**tx) if isinstance(tx, dict) else tx for tx in raw_txs]
    raw_sh = row.pop("stop_history", []) or []
    stop_hist = [StopUpdate(**su) if isinstance(su, dict) else su for su in raw_sh]
    # Defensive: fill any missing top-level fields with defaults
    for fld in ("id", "ticker", "setup_tag", "notes", "created_at"):
        row.setdefault(fld, "")
    row.setdefault("stop_price", 0.0)
    # Backfill initial_stop_price from stop_price for legacy trades that
    # pre-date the field's existence.
    if "initial_stop_price" not in row or not row.get("initial_stop_price"):
        row["initial_stop_price"] = row.get("stop_price", 0.0)
    # Backfill source for legacy trades that pre-date the field. "Other"
    # is the neutral default so old trades don't pollute source-based stats.
    if "source" not in row or not row.get("source"):
        row["source"] = "Other"
    return Trade(transactions=txs, stop_history=stop_hist, **row)


def _row_from_trade(t: Trade) -> dict:
    """Serialize a Trade to a JSON-safe dict (transactions as nested dicts)."""
    d = asdict(t)  # dataclass + nested dataclasses → nested dicts. Works.
    return d


def load_trades() -> list[Trade]:
    """Load all trades from journal.json. Returns [] if file missing/unreadable.

    Auto-migrates v1 records on load (doesn't write back until next save).
    """
    _ensure_dir()
    if not JOURNAL_FILE.exists():
        return []
    try:
        raw = JOURNAL_FILE.read_bytes().replace(b"\x00", b"")
        data = json.loads(raw)
        return [_trade_from_row(r) for r in data]
    except Exception:
        return []


def save_trades(trades: list[Trade]) -> None:
    """Persist all trades atomically. Replaces the entire file."""
    _ensure_dir()
    body = json.dumps([_row_from_trade(t) for t in trades], indent=2, default=str)
    _atomic_write_text(JOURNAL_FILE, body)


def add_trade(trade: Trade) -> None:
    """Append a single trade and persist."""
    trades = load_trades()
    trades.append(trade)
    save_trades(trades)


def update_trade(trade_id: str, mutator) -> bool:
    """Find trade by id, apply `mutator(trade)`, save. Returns True if found.

    mutator is a callable that takes a Trade and modifies it in place.
    Example: lambda t: t.add_buy(date='2026-06-12', price=720, shares=50)
    """
    trades = load_trades()
    for t in trades:
        if t.id == trade_id:
            mutator(t)
            save_trades(trades)
            return True
    return False


def delete_trade(trade_id: str) -> bool:
    """Remove the trade matching trade_id. Returns True if found + deleted."""
    trades = load_trades()
    before = len(trades)
    trades = [t for t in trades if t.id != trade_id]
    if len(trades) < before:
        save_trades(trades)
        return True
    return False


# ---------------------------------------------------------------------------
# Expectancy
# ---------------------------------------------------------------------------

def expectancy_by_setup(trades: list[Trade]) -> dict:
    """Compute hit rate, avg win R, avg loss R, expectancy R per setup.

    Only CLOSED trades that have a computable realized_r (i.e., had a stop)
    count toward expectancy. Closed trades without a stop are counted in the
    P&L total but excluded from R-based statistics.
    """
    by_setup: dict = {}
    for t in trades:
        if t.status != "closed":
            continue
        r = t.realized_r
        if r is None:
            continue  # no stop set, can't compute R
        bucket = by_setup.setdefault(t.setup_tag, {"wins_r": [], "losses_r": []})
        if r > 0:
            bucket["wins_r"].append(r)
        else:
            bucket["losses_r"].append(r)

    out = {}
    for tag, b in by_setup.items():
        wins = len(b["wins_r"])
        losses = len(b["losses_r"])
        n = wins + losses
        if n == 0:
            continue
        hit_rate = wins / n
        avg_win = sum(b["wins_r"]) / wins if wins else 0.0
        avg_loss = sum(b["losses_r"]) / losses if losses else 0.0
        expectancy = hit_rate * avg_win + (1 - hit_rate) * avg_loss
        out[tag] = {
            "n": n,
            "wins": wins,
            "losses": losses,
            "hit_rate": round(hit_rate, 3),
            "avg_win_r": round(avg_win, 3),
            "avg_loss_r": round(avg_loss, 3),
            "expectancy_r": round(expectancy, 3),
        }
    return out


def expectancy_by_source(trades: list[Trade]) -> dict:
    """Same shape as expectancy_by_setup, but bucketed by trade SOURCE.

    Lets you answer: "Which scan/filter actually surfaces my best winners?"
    e.g. compare hit rate + expectancy of BUY ZONE-sourced trades vs Top
    Stocks intersection trades vs Manual / Eyeball picks.

    Only CLOSED trades that have a computable realized_r count.
    """
    by_source: dict = {}
    for t in trades:
        if t.status != "closed":
            continue
        r = t.realized_r
        if r is None:
            continue
        bucket = by_source.setdefault(t.source or "Other", {"wins_r": [], "losses_r": []})
        if r > 0:
            bucket["wins_r"].append(r)
        else:
            bucket["losses_r"].append(r)

    out = {}
    for src, b in by_source.items():
        wins = len(b["wins_r"])
        losses = len(b["losses_r"])
        n = wins + losses
        if n == 0:
            continue
        hit_rate = wins / n
        avg_win = sum(b["wins_r"]) / wins if wins else 0.0
        avg_loss = sum(b["losses_r"]) / losses if losses else 0.0
        expectancy = hit_rate * avg_win + (1 - hit_rate) * avg_loss
        out[src] = {
            "n": n,
            "wins": wins,
            "losses": losses,
            "hit_rate": round(hit_rate, 3),
            "avg_win_r": round(avg_win, 3),
            "avg_loss_r": round(avg_loss, 3),
            "expectancy_r": round(expectancy, 3),
        }
    return out
