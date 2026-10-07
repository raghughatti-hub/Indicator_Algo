from typing import Any

from models.schemas import IntradayRunRequest
from utils.helpers import _resolved_option_type


class RiskManagerMixin:
    """Mixin class for IntradayRunner handling trade-level targets, stop-losses, and trailing SL updates."""

    def _sl_price(self, entry: float, request: IntradayRunRequest) -> float:
        """Calculate initial manual stoploss price based on points or percentage settings."""
        if request.sl_type == "percentage":
            move = entry * request.stoploss_value / 100
        else:
            move = request.stoploss_value
        return entry - move if request.option_strategy == "BUY" else entry + move

    def _target_price(self, entry: float, request: IntradayRunRequest) -> float:
        """Calculate initial manual target price based on points or percentage settings."""
        if request.target_type == "percentage":
            move = entry * request.target_value / 100
        else:
            move = request.target_value
        return entry + move if request.option_strategy == "BUY" else entry - move

    def _sync_strategy_sltp(self, order: dict[str, Any], request: IntradayRunRequest, strategy: dict[str, Any]) -> str | None:
        """Synchronize indicator SL/TSL/Targets dynamically if strategy-driven exits are configured."""
        if request.sltp_instrument != "strategy":
            return None
        signal = self._matching_strategy_signal(order, strategy, request)
        if not signal:
            return None
        status = str(signal.get("status") or "").upper()
        terminal = signal.get("closed", False)
        reason = signal.get("exit_reason")
        if terminal or status in {"SL", "TP3", "TSL", "OPPOSITE"} or "_TSL" in status or "_OPP" in status:
            return f"STRATEGY_{reason or status}"
        return None

    @staticmethod
    def _matching_strategy_signal(order: dict[str, Any], strategy: dict[str, Any], request: IntradayRunRequest) -> dict[str, Any] | None:
        """Helper to link a local paper order with its original strategy signal using timestamp and side."""
        order_time = str(order.get("source_signal_time") or order.get("entry_time") or "")
        order_side = str(order.get("source_signal") or "")
        for signal in strategy.get("signals", []):
            if str(signal.get("time")) == order_time and str(signal.get("side")) == order_side:
                return signal
        return None

    def _update_trailing_stop(self, order: dict[str, Any], ltp: float, request: IntradayRunRequest, direction: int) -> None:
        """Calculate and apply Trailing SL transitions and move-to-cost updates on current LTP quote."""
        entry = float(order.get("risk_entry") or order["option_entry"])
        profit_from_entry = (ltp - entry) * direction
        
        # Prefer order-level settings over request-level global settings
        move_sl_to_cost = order.get("move_sl_to_cost") if order.get("move_sl_to_cost") is not None else request.move_sl_to_cost
        move_sl_to_cost_points = order.get("move_sl_to_cost_points") if order.get("move_sl_to_cost_points") is not None else request.move_sl_to_cost_points
        trailing_stoploss = order.get("trailing_stoploss") if order.get("trailing_stoploss") is not None else request.trailing_stoploss
        
        # 1. Handle Move-SL-To-Cost
        if move_sl_to_cost:
            cost_after = float(move_sl_to_cost_points or 0)
            if not order.get("sl_moved_to_cost", False):
                if profit_from_entry >= cost_after:
                    cost_sl = entry
                    order["sl_moved_to_cost"] = True
                    current_sl = float(order["stoploss"])
                    if direction == 1:
                        if current_sl < cost_sl:
                            order["stoploss"] = cost_sl
                    else:
                        if current_sl > cost_sl:
                            order["stoploss"] = cost_sl
                    
        if not trailing_stoploss:
            return

        current_sl = float(order["stoploss"])
        initial_sl = float(order["initial_stoploss"])
        sl_differed = (current_sl != initial_sl)

        # If move_sl_to_cost is enabled, trailing should start after cost adjustment OR if SL has differed
        if move_sl_to_cost and not order.get("sl_moved_to_cost", False) and not sl_differed:
            return
            
        # 2. Process Trailing SL Steps
        trail_start_value = float(order.get("trail_start_value") if order.get("trail_start_value") is not None else request.trail_start_value)
        trail_when_moves_by = float(order.get("trail_when_moves_by") if order.get("trail_when_moves_by") is not None else request.trail_when_moves_by)
        trail_move_sl_by = float(order.get("trail_move_sl_by") if order.get("trail_move_sl_by") is not None else request.trail_move_sl_by)
        trail_start_type = order.get("trail_start_type") if order.get("trail_start_type") is not None else request.trail_start_type
        
        if trail_start_type == "percentage":
            start_move = entry * trail_start_value / 100
            trail_when_moves_by = entry * trail_when_moves_by / 100
            trail_move_sl_by = entry * trail_move_sl_by / 100
        else:
            start_move = trail_start_value
            
        is_active = order.get("trail_active", False) or (profit_from_entry >= start_move) or sl_differed
        if not is_active:
            return
            
        if not order.get("trail_active"):
            order["trail_active"] = True
            if order.get("sl_moved_to_cost"):
                order["trail_anchor"] = ltp
            else:
                if direction == 1:
                    order["trail_anchor"] = entry + start_move
                else:
                    order["trail_anchor"] = entry - start_move
            
        moved_after_anchor = (ltp - float(order["trail_anchor"])) * direction
        if trail_when_moves_by <= 0 or moved_after_anchor < trail_when_moves_by:
            return
            
        steps = int(moved_after_anchor // trail_when_moves_by)
        sl_move = steps * trail_move_sl_by * direction
        next_sl = float(order["stoploss"]) + sl_move
        
        if direction == 1:
            order["stoploss"] = max(float(order["stoploss"]), next_sl)
        else:
            order["stoploss"] = min(float(order["stoploss"]), next_sl)
        order["trail_anchor"] = float(order["trail_anchor"]) + steps * trail_when_moves_by * direction
