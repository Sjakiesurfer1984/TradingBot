# src/TradingBot/v2/risk/price_policy.py
#
# This module defines how we select a conservative "execution reference" price
# from a quote (bid/ask/mid).
#
# Core idea
# - Strategies should not assume mid-price fills.
# - BUY legs should be pessimistic and prefer the ask.
# - SELL legs should be pessimistic and prefer the bid.
#
# This is NOT an order routing engine.
# It is simply a deterministic selector used inside strategy and risk logic.

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from TradingBot.domain.types import AssetQuote
from TradingBot.utilities.logger import setup_logger
from TradingBot.utilities.logging_utils import log_scope


logger = setup_logger("PricePolicy")

class PriceSide(Enum):
    """
    Trade intent side used for execution-aware pricing.

    BUY
    - Used when entering a long position.
    - Execution is pessimistic: prefer ask.

    SELL
    - Used when exiting a long position or selling premium.
    - Execution is pessimistic: prefer bid.
    """

    BUY = "buy"
    SELL = "sell"


class PriceSelectionPolicy(ABC):
    """
    Strategy-independent interface for selecting an execution price
    from a market quote.

    Design intent
    - Centralise bid/ask selection logic.
    - Avoid hardcoding execution assumptions inside strategies.
    - Make pricing rules explicit, testable, and replaceable.

    This policy does NOT
    - Fetch market data.
    - Know about strategies.
    - Know about order types or sizing.
    """

    @abstractmethod
    def select(self, quote: AssetQuote, side: PriceSide) -> Optional[float]:
        """
        Select an execution price from a quote.

        Behaviour
        - Returns a positive float if a usable price exists.
        - Returns None if no safe execution price can be determined.

        Implementations must be conservative by default.
        """
        raise NotImplementedError


class DefaultPriceSelectionPolicy(PriceSelectionPolicy):
    """
    Conservative execution-aware price selection.

    Rules
    - BUY  -> prefer ask, fallback to mid, then bid
    - SELL -> prefer bid, fallback to mid, then ask

    Why this exists
    - Models worst-case fills.
    - Prevents strategies from assuming mid-price execution.
    - Safe default for multi-leg option strategies (PMCC, spreads).
    """

    def select(self, quote: AssetQuote, side: PriceSide) -> Optional[float]:
        """
        Return the selected execution reference price for a given side.

        Logging
        - We log which field was selected (ask/mid/bid) so it is obvious
          when this policy is actually used at runtime.
        - If nothing is usable, we log that explicitly and return None.

        Note
        - This function must stay pure: no broker IO, no randomness.
        """
        # We keep logs scoped so you can correlate selection with the caller.
        # quote.symbol may or may not exist depending on your AssetQuote definition,
        # so we log only the numeric fields here.
        with log_scope("price_policy.select", logger, extra=f"side={side.value}"):
            ask: Optional[float] = quote.ask
            bid: Optional[float] = quote.bid
            mid: Optional[float] = quote.mid

            logger.info(
                "Select start | side=%s bid=%s ask=%s mid=%s",
                side.value,
                bid,
                ask,
                mid,
            )

            if side == PriceSide.BUY:
                # BUY: pessimistic for a long entry (assume you pay the ask).
                if ask is not None and ask > 0.0:
                    logger.info("Select result | side=%s selected=ask price=%.6f", side.value, float(ask))
                    return float(ask)

                # Fallback: if spread is missing, mid can be a reasonable proxy.
                if mid is not None and mid > 0.0:
                    logger.info("Select result | side=%s selected=mid price=%.6f", side.value, float(mid))
                    return float(mid)

                # Last resort: bid only. This is optimistic for BUY, but better than nothing.
                if bid is not None and bid > 0.0:
                    logger.info("Select result | side=%s selected=bid price=%.6f", side.value, float(bid))
                    return float(bid)

                logger.warning("Select result | side=%s selected=none reason=no_usable_price", side.value)
                return None

            if side == PriceSide.SELL:
                # SELL: pessimistic for selling premium or exiting (assume you get the bid).
                if bid is not None and bid > 0.0:
                    logger.info("Select result | side=%s selected=bid price=%.6f", side.value, float(bid))
                    return float(bid)

                # Fallback: if spread is missing, mid can be a reasonable proxy.
                if mid is not None and mid > 0.0:
                    logger.info("Select result | side=%s selected=mid price=%.6f", side.value, float(mid))
                    return float(mid)

                # Last resort: ask only. This is optimistic for SELL, but better than nothing.
                if ask is not None and ask > 0.0:
                    logger.info("Select result | side=%s selected=ask price=%.6f", side.value, float(ask))
                    return float(ask)

                logger.warning("Select result | side=%s selected=none reason=no_usable_price", side.value)
                return None

            # Defensive: if a new enum value appears and is not handled.
            logger.warning("Select result | side=%s selected=none reason=unknown_side", str(side))
            return None
