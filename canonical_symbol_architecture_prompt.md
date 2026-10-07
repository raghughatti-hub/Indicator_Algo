# Enterprise Architecture Blueprint: Canonical Symbol Standard & Bi-Directional Broker Adapter

> **Usage**: Save/download this document as a master architectural specification prompt. You can paste or attach this prompt into any new trading codebase project to automatically implement a unified, multi-broker symbol standard.

---

## 1. Executive Summary & Problem Statement

### **The Problem**:
In multi-broker trading platforms, every broker uses a completely different syntax for market instruments:
- **Flattrade**: `RELIANCE28AUG242900CE` (Token: `54321`, Exchange: `NFO`)
- **Upstox**: `NSE_FO|54321` or `RELIANCE 28 AUG 2900 CE`
- **Zebu**: `RELIANCE24AUG2900CE`
- **Zerodha**: `NFO:RELIANCE24AUG2900CE` (Token: `13876226`)

Without a canonical standard, core application logic (UI rendering, WebWorker calculations, strategy engines, order management, risk filters) becomes cluttered with messy fallback `if/else` checks for every broker.

### **The Solution**:
Implement a **Canonical Symbol Standard**. The core frontend UI, WebSocket relay, calculation engine, and risk filters operate **exclusively** on a unified internal format. Dedicated **Broker Adapters** perform bi-directional translation at the outer boundaries of the system.

---

## 2. Canonical Symbol Standard Specification

All internal instruments must conform to the standard 5-part pipe-delimited schema:

$$\text{Canonical ID} = \text{INSTRUMENT\_TYPE} \mid \text{SYMBOL} \mid [\text{EXPIRY}] \mid [\text{STRIKE}] \mid [\text{OPTION\_TYPE}]$$

### **Canonical Schema Examples**:

| Instrument Type | Canonical Key Syntax | Example |
| :--- | :--- | :--- |
| **Cash Equity (Spot)** | `EQ\|<SYMBOL>` | `EQ\|RELIANCE` |
| **Index Spot** | `INDEX\|<SYMBOL>` | `INDEX\|NIFTY` |
| **Stock/Index Future** | `FUT\|<SYMBOL>\|<YYYY-MM-DD>` | `FUT\|RELIANCE\|2026-08-28` |
| **Call Option** | `OPT\|<SYMBOL>\|<YYYY-MM-DD>\|<STRIKE>\|CE` | `OPT\|RELIANCE\|2026-08-28\|2900\|CE` |
| **Put Option** | `OPT\|<SYMBOL>\|<YYYY-MM-DD>\|<STRIKE>\|PE` | `OPT\|NIFTY\|2026-08-14\|24500\|PE` |

---

## 3. Bi-Directional Adapter Architecture Flow

```
┌────────────────────────────────────────────────────────────────────────┐
│                        CANONICAL CORE ENGINE                           │
│  UI Table | WebWorker | Trade Engines | Risk | WebSockets Ticks        │
│                Operates EXCLUSIVELY on Canonical Keys                  │
│                (e.g., "OPT|RELIANCE|2026-08-28|2900|CE")               │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                       ┌────────────┴────────────┐
                       │  BI-DIRECTIONAL MAPPER  │
                       │ canonical_symbol_mapper │
                       └────────────┬────────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         │                          │                          │
┌────────▼────────┐        ┌────────▼────────┐        ┌────────▼────────┐
│ FLATTRADE       │        │ UPSTOX          │        │ ZEBU / ZERODHA  │
│ ADAPTER         │        │ ADAPTER         │        │ ADAPTER         │
└─────────────────┘        └─────────────────┘        └─────────────────┘
```

1. **Inbound Path (Ticks / Quotes)**:
   $$\text{Broker Raw Symbol / Token} \xrightarrow{\text{Broker Adapter}} \text{Canonical Key} \xrightarrow{\text{Broadcast}} \text{UI / WebWorker}$$
2. **Outbound Path (Order Placement)**:
   $$\text{User Action on Canonical Key} \xrightarrow{\text{Broker Adapter}} \text{Broker Raw Symbol Payload} \xrightarrow{\text{REST API}} \text{Order Executed}$$

---

## 4. Universal Python Implementation (`canonical_symbol_mapper.py`)

Here is the ready-to-use, production-grade Python implementation of the Canonical Mapper:

```python
"""
Canonical Symbol Mapper Module
Provides O(1) bi-directional symbol translation between internal canonical IDs
and broker-specific tokens/trading symbols.
"""
import os
import json
import logging
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger("CanonicalMapper")

class CanonicalSymbolMapper:
    def __init__(self):
        # Maps canonical_id -> broker_name -> broker_spec dict
        self._canonical_to_broker: Dict[str, Dict[str, Dict[str, Any]]] = {}
        
        # Maps (broker_name, token_or_tsym) -> canonical_id
        self._broker_to_canonical: Dict[Tuple[str, str], str] = {}

    @staticmethod
    def build_canonical_id(inst_type: str, symbol: str, expiry: Optional[str] = None, strike: Optional[float] = None, option_type: Optional[str] = None) -> str:
        """
        Builds unified canonical key.
        Examples:
          build_canonical_id("EQ", "RELIANCE") -> "EQ|RELIANCE"
          build_canonical_id("FUT", "RELIANCE", expiry="2026-08-28") -> "FUT|RELIANCE|2026-08-28"
          build_canonical_id("OPT", "RELIANCE", expiry="2026-08-28", strike=2900, option_type="CE") -> "OPT|RELIANCE|2026-08-28|2900|CE"
        """
        inst = inst_type.upper().strip()
        sym = symbol.upper().strip()
        
        if inst in ("EQ", "EQUITY", "SPOT"):
            return f"EQ|{sym}"
        elif inst in ("INDEX", "IDX"):
            return f"INDEX|{sym}"
        elif inst in ("FUT", "FUTSTK", "FUTIDX"):
            exp_str = str(expiry).split(" ")[0] if expiry else "NEAR"
            return f"FUT|{sym}|{exp_str}"
        elif inst in ("OPT", "OPTSTK", "OPTIDX"):
            exp_str = str(expiry).split(" ")[0] if expiry else "NEAR"
            stk_str = f"{float(strike):.2f}".rstrip('0').rstrip('.') if strike is not None else "0"
            opt_str = option_type.upper().strip() if option_type else "CE"
            return f"OPT|{sym}|{exp_str}|{stk_str}|{opt_str}"
        
        return f"{inst}|{sym}"

    def register_mapping(self, canonical_id: str, broker_name: str, broker_token: str, broker_symbol: str, exchange: str, lot_size: int = 1, extra_info: Optional[Dict[str, Any]] = None):
        """Registers bi-directional mapping between canonical_id and broker-specific details."""
        b_name = broker_name.upper().strip()
        token_str = str(broker_token).strip()
        symbol_str = str(broker_symbol).strip()
        
        spec = {
            "canonical_id": canonical_id,
            "broker_name": b_name,
            "broker_token": token_str,
            "broker_symbol": symbol_str,
            "exchange": exchange.upper().strip(),
            "lot_size": lot_size
        }
        if extra_info:
            spec.update(extra_info)

        # 1. Store Canonical -> Broker Spec
        if canonical_id not in self._canonical_to_broker:
            self._canonical_to_broker[canonical_id] = {}
        self._canonical_to_broker[canonical_id][b_name] = spec

        # 2. Store Broker (Name, Token) -> Canonical ID
        if token_str and token_str != "-":
            self._broker_to_canonical[(b_name, token_str)] = canonical_id
            
        # 3. Store Broker (Name, Symbol) -> Canonical ID
        if symbol_str:
            self._broker_to_canonical[(b_name, symbol_str)] = canonical_id
            self._broker_to_canonical[(b_name, f"{spec['exchange']}|{symbol_str}")] = canonical_id

    def get_broker_spec(self, canonical_id: str, broker_name: str) -> Optional[Dict[str, Any]]:
        """Returns broker-specific order payload fields for a canonical_id."""
        return self._canonical_to_broker.get(canonical_id, {}).get(broker_name.upper().strip())

    def get_canonical_id(self, broker_name: str, token_or_symbol: str) -> Optional[str]:
        """Resolves broker token or trading symbol back to canonical_id."""
        b_name = broker_name.upper().strip()
        key = (b_name, str(token_or_symbol).strip())
        return self._broker_to_canonical.get(key)

# Global Singleton Instance
symbol_mapper = CanonicalSymbolMapper()
```

---

## 5. Prompt for AI Assistants to Apply in Any Project

Copy and paste the following prompt when starting a new codebase or refactoring an existing project:

```text
[SYSTEM TASK PROMPT: IMPLEMENT CANONICAL SYMBOL MAPPER ARCHITECTURE]

Please refactor/implement the symbol handling in this repository to strictly conform to the Canonical Symbol Standard architecture:

1. Create a `broker/canonical_symbol_mapper.py` module using the 5-part pipe-delimited schema:
   - EQ|<SYMBOL>
   - FUT|<SYMBOL>|<EXPIRY>
   - OPT|<SYMBOL>|<EXPIRY>|<STRIKE>|<CE/PE>

2. Master Instrument Loader:
   When reading broker instrument CSVs or master endpoints, seed all tokens and trading symbols into `symbol_mapper.register_mapping()`.

3. Inbound Websocket Feed / Ticks:
   When broker WebSockets send tick messages containing broker tokens/symbols, translate them via `symbol_mapper.get_canonical_id(broker_name, token)` before broadcasting ticks to UI/workers.

4. UI & Calculation Engine:
   Ensure all table row IDs, WebWorker calculations, and state keys operate exclusively on `canonical_id`.

5. Outbound Order Placement:
   When ordering, pass `canonical_id` to `symbol_mapper.get_broker_spec(canonical_id, active_broker_name)` to generate exact broker-native order payloads (trading symbol, exchange, lot size).
```

---

### **Status**: Ready to use in any project! You can copy/download `canonical_symbol_architecture_prompt.md`.
