const voiceToggle = document.querySelector("#voiceToggle");
async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = sessionStorage.getItem("control_api_token");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response = await fetch(url, {...options, headers});
  if (response.status === 401 && !token) {
    const supplied = window.prompt("Enter the CONTROL_API_TOKEN configured on this server:");
    if (supplied) {
      sessionStorage.setItem("control_api_token", supplied);
      headers.set("Authorization", `Bearer ${supplied}`);
      response = await fetch(url, {...options, headers});
    }
  }
  return response;
}
// ==============================================================================
// === SECTION 1: GLOBAL SELECTIONS & STATE CONSTANTS ===
// ==============================================================================

const statusEl = document.querySelector("#status");
const connectionBadge = document.querySelector("#connectionBadge");
const modeBadge = document.querySelector("#modeBadge");
const summaryGrid = document.querySelector("#summaryGrid");
const signalsBody = document.querySelector("#signalsBody");
const ordersBody = document.querySelector("#ordersBody");
const dayPnlBadge = document.querySelector("#dayPnlBadge");
const brokerPnlBadge = document.querySelector("#brokerPnlBadge");
const themeToggleBtn = document.querySelector("#themeToggleBtn");
const themeToggleIcon = document.querySelector("#themeToggleIcon");
const themeToggleText = document.querySelector("#themeToggleText");

function initTheme() {
  const savedTheme = localStorage.getItem("app_theme") || "dark";
  if (savedTheme === "light") {
    document.body.classList.add("light-theme");
    if (themeToggleIcon) themeToggleIcon.textContent = "☀️";
    if (themeToggleText) themeToggleText.textContent = "Light";
  } else {
    document.body.classList.remove("light-theme");
    if (themeToggleIcon) themeToggleIcon.textContent = "🌙";
    if (themeToggleText) themeToggleText.textContent = "Dark";
  }
}

if (themeToggleBtn) {
  themeToggleBtn.addEventListener("click", () => {
    const isLight = document.body.classList.toggle("light-theme");
    localStorage.setItem("app_theme", isLight ? "light" : "dark");
    themeToggleIcon.textContent = isLight ? "☀️" : "🌙";
    themeToggleText.textContent = isLight ? "Light" : "Dark";
  });
}

initTheme();

const chart = document.querySelector("#chart");
const ctx = chart.getContext("2d");

let runnerTimer = null;
let orderTimer = null;
let quoteTimer = null;
let tradeMode = "PAPER";
let runnerActive = false;
let lastConnectionState = null;
let lastRunnerActiveState = null;
let voiceUnlocked = false;
let voiceEnabled = false;
let instrumentMetaTimer = null;

let chartState = {
  bars: [],
  signals: [],
  startIdx: 0,
  endIdx: 0,
  isDragging: false,
  dragStartPos: null,
  dragStartIdxs: null,
};

let activeOrderKeyToAdjust = null;
let speakTimeoutId = null;
let adjustmentsDirty = false;
let adjustSLDirty = false;
let adjustTargetDirty = false;
let userEditingAdjustments = false;
let adjustFocusTimeout = null;
let appInitialized = false;
let _ordersAbortController = null;  // Cancels stale in-flight /orders requests

const announcedOrders = new Map();
const announcedSignals = new Map();

const lotSizes = {
  NIFTY: 65,
  BANKNIFTY: 35,
  FINNIFTY: 65,
  MIDCPNIFTY: 140,
  SENSEX: 20,
  BANKEX: 15,
};

const strikeGaps = {
  NIFTY: 50,
  BANKNIFTY: 100,
  FINNIFTY: 50,
  MIDCPNIFTY: 25,
  SENSEX: 100,
  BANKEX: 100,
};


// ==============================================================================
// === SECTION 2: VOICE ALERT NOTIFICATION ENGINE ===
// ==============================================================================

function enableVoiceAlerts() {
  voiceUnlocked = voiceEnabled;
}

function setVoiceEnabled(enabled) {
  voiceEnabled = enabled;
  voiceUnlocked = enabled;
  if (voiceToggle) {
    voiceToggle.classList.toggle("on", enabled);
    voiceToggle.classList.toggle("off", !enabled);
    voiceToggle.setAttribute("aria-label", enabled ? "Voice alerts on" : "Voice alerts off");
    voiceToggle.setAttribute("title", enabled ? "Voice alerts on" : "Voice alerts off");
  }
}

function speakAlert(message) {
  if (!voiceEnabled || !voiceUnlocked || !("speechSynthesis" in window) || !message) return;
  
  // Clear any previously scheduled alerts that haven't spoken yet
  if (speakTimeoutId) {
    clearTimeout(speakTimeoutId);
  }
  
  // Cancel any currently speaking alert instantly
  window.speechSynthesis.cancel();
  
  // Debounce consecutive speech triggers to capture only the final setting change
  speakTimeoutId = setTimeout(() => {
    if (!voiceEnabled) return;
    const utterance = new SpeechSynthesisUtterance(message);
    utterance.rate = 1;
    utterance.pitch = 1;
    window.speechSynthesis.speak(utterance);
    speakTimeoutId = null;
  }, 150);
}

function announceOrderEvents(orders) {
  (orders || []).forEach((o) => {
    const key = orderKey(o);
    const alreadySeen = announcedOrders.has(key);
    const previous = announcedOrders.get(key) || {};
    const current = {
      status: o.status,
      quantity: Number(o.quantity || 0),
      executionAlert: o.execution_alert || "",
      entryId: o.entry_order_id || "",
      exitId: o.exit_order_id || "",
      stoploss: o.stoploss,
      exit: o.option_exit,
    };
    
    if (current.executionAlert && current.executionAlert !== previous.executionAlert) {
      speakAlert(`${optionLabel(o)}: ${current.executionAlert}. Remaining quantity ${current.quantity}.`);
    }
    // Announce entries
    if (!previous.entryAnnounced && current.quantity > 0) {
      speakAlert(`Entry of ${optionLabel(o)} at ${money(o.option_entry)}. ${statusRemark(o)}`);
      current.entryAnnounced = true;
    } else {
      current.entryAnnounced = previous.entryAnnounced || false;
    }
    
    // Announce exits
    const exitHappened = alreadySeen && (
      (previous.quantity > current.quantity) ||
      (previous.status && previous.status !== "Closed" && current.status === "Closed")
    );
    if (exitHappened) {
      speakAlert(`Exit of ${optionLabel(o)} at ${money(o.option_exit || o.option_ltp)}. ${statusRemark(o)}`);
    }
    
    // Announce Trailing stop movements
    if (alreadySeen && previous.stoploss !== undefined && Number(previous.stoploss) !== Number(current.stoploss)) {
      speakAlert(`Trailing stop updated for ${optionLabel(o)} to ${money(current.stoploss)}.`);
    }
    announcedOrders.set(key, current);
  });
}

function announceSignalEvents(signals) {
  (signals || []).forEach((s) => {
    const key = signalKey(s);
    const current = { status: s.status, tsl: s.tsl };
    announcedSignals.set(key, current);
  });
}


// ==============================================================================
// === SECTION 3: CORE UTILITIES & TEXT FORMATTERS ===
// ==============================================================================

function orderKey(o) {
  return String(o.order_key || o.entry_order_id || `${o.entry_time || o.time}|${o.tradingsymbol || ""}|${o.option_type || ""}|${o.strike || ""}`);
}

function optionLabel(o) {
  const scrip = o.scripname || o.underlying || "";
  const option = o.option_type ? `${o.option_type}` : "";
  const strike = o.strike ? `${o.strike}` : "";
  return `${scrip} ${option} strike ${strike}`.replace(/\s+/g, " ").trim();
}

function statusRemark(o) {
  return o.exit_remarks || o.entry_remarks || o.quote_error || o.exit_reason || o.status || "";
}

function signalKey(s) {
  return `${s.time}|${s.strike}|${s.option_type}|${s.side}`;
}

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  return Number(value).toFixed(2);
}

function editValue(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "";
  return Number(value).toFixed(2);
}

function signedMoney(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const num = Number(value);
  return `${num > 0 ? "+" : ""}${num.toFixed(2)}`;
}

function signedPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
  const num = Number(value);
  return `${num > 0 ? "+" : ""}${num.toFixed(2)}%`;
}

function timeOnly(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString();
}

function pnlClass(value) {
  const num = Number(value);
  if (Number.isNaN(num) || num === 0) return "";
  return num > 0 ? "profit" : "loss";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}


// ==============================================================================
// === SECTION 4: DOM RENDERING UTILITIES ===
// ==============================================================================

function renderConnectionStatus(s, phase = "") {
  const brokerName = s.broker ? s.broker.toUpperCase() : "BROKER";
  const creds = s.creds_txt_loaded ? "creds loaded" : "no creds";
  const live = s.live_trading_enabled ? "Live orders enabled" : "Live orders off";
  
  statusEl.textContent = `${s.connected ? (brokerName + " connected") : (brokerName + " not connected")} | ${creds} | ${live}`;
  connectionBadge.classList.toggle("live", Boolean(s.connected));
  connectionBadge.classList.toggle("offline", !s.connected);
  connectionBadge.classList.toggle("checking", phase === "checking");
  
  connectionBadge.textContent = s.connected
    ? `${brokerName} LIVE`
    : `${brokerName} OFF${phase === "checking" ? " - checking..." : ""}`;
  connectionBadge.title = s.last_error || statusEl.textContent;
  
  if (lastConnectionState !== null && lastConnectionState !== Boolean(s.connected)) {
    speakAlert(s.connected ? `${brokerName} login successful. Connection live.` : `${brokerName} logout. Connection not live.`);
  }
  lastConnectionState = Boolean(s.connected);

  // Transition UI: If connected, hide landing page, else show landing page
  const landingPage = document.querySelector("#landingPage");
  if (landingPage) {
    if (s.connected) {
      landingPage.classList.add("hidden");
    } else {
      landingPage.classList.remove("hidden");
    }
  }

  // Update notes if they mention broker
  const modeNote = document.querySelector("#modeNote");
  if (modeNote) {
    modeNote.textContent = `Candles are fetched from ${brokerName}. Paper mode is default; Real mode sends broker orders when live trading is enabled.`;
  }

  // Update selected broker card highlight
  if (s.broker) {
    window.selectedBroker = s.broker.toLowerCase();
    const cardZ = document.querySelector("#brokerCardZebu");
    const cardF = document.querySelector("#brokerCardFlattrade");
    const cardU = document.querySelector("#brokerCardUpstox");
    if (cardZ) cardZ.classList.toggle("active", window.selectedBroker === "zebu");
    if (cardF) cardF.classList.toggle("active", window.selectedBroker === "flattrade");
    if (cardU) cardU.classList.toggle("active", window.selectedBroker === "upstox");
  }

  // Update Login/Logout toggle button state (replaces Check Login button)
  const checkBtn = document.querySelector("#checkConnection");
  if (checkBtn) {
    if (s.connected) {
      checkBtn.textContent = "Logout";
      checkBtn.classList.remove("success");
      checkBtn.classList.add("danger");
      checkBtn.title = "Disconnect current session and switch broker";
    } else {
      checkBtn.textContent = "Login";
      checkBtn.classList.remove("danger");
      checkBtn.classList.add("success");
      checkBtn.title = "View broker login landing page";
    }
  }
}

function drawChart(bars, signals) {
  const isLight = document.body.classList.contains("light-theme");
  ctx.fillStyle = isLight ? "#ffffff" : "#171b20";
  ctx.fillRect(0, 0, chart.width, chart.height);
  
  if (!bars || bars.length === 0) return;
  
  const wasAtEnd = (chartState.endIdx === chartState.bars.length);
  const prevLength = chartState.bars.length;
  
  chartState.bars = bars;
  chartState.signals = signals || [];
  
  if (prevLength === 0 || chartState.endIdx === 0 || wasAtEnd) {
    const currentRange = Math.max(10, chartState.endIdx - chartState.startIdx || 100);
    chartState.endIdx = bars.length;
    chartState.startIdx = Math.max(0, bars.length - currentRange);
  } else {
    if (chartState.startIdx >= bars.length) {
      chartState.startIdx = Math.max(0, bars.length - 100);
      chartState.endIdx = bars.length;
    } else if (chartState.endIdx > bars.length) {
      chartState.endIdx = bars.length;
    }
  }

  const start = chartState.startIdx;
  const end = chartState.endIdx;
  const visibleBars = bars.slice(start, end);
  const visibleCount = visibleBars.length;
  
  const pad = 28;
  const plotW = chart.width - pad * 2;
  const plotH = chart.height - pad * 2;
  const highs = visibleBars.map((b) => Number(b.high));
  const lows = visibleBars.map((b) => Number(b.low));
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const scaleY = (p) => pad + (max - p) * (plotH / Math.max(1, max - min));
  
  const colW = plotW / Math.max(1, visibleCount);
  const candleW = Math.max(3, colW - 2);

  // Draw chart border grids
  ctx.strokeStyle = isLight ? "#cbd5e1" : "#2b343b";
  ctx.beginPath();
  ctx.moveTo(pad, pad);
  ctx.lineTo(pad, chart.height - pad);
  ctx.lineTo(chart.width - pad, chart.height - pad);
  ctx.stroke();

  // Draw candle bars
  visibleBars.forEach((b, k) => {
    const x = pad + k * colW + candleW / 2;
    const open = Number(b.open);
    const close = Number(b.close);
    const high = Number(b.high);
    const low = Number(b.low);
    const up = close >= open;
    
    ctx.strokeStyle = up ? "#57d68d" : "#ff7468";
    ctx.fillStyle = ctx.strokeStyle;
    
    ctx.beginPath();
    ctx.moveTo(x, scaleY(high));
    ctx.lineTo(x, scaleY(low));
    ctx.stroke();
    
    const y = Math.min(scaleY(open), scaleY(close));
    const h = Math.max(1, Math.abs(scaleY(open) - scaleY(close)));
    ctx.fillRect(x - candleW / 2, y, candleW, h);
  });

  // Draw Buy/Sell indicator bubbles ("B" / "S")
  if (signals && signals.length > 0) {
    signals.forEach((s) => {
      const sigTime = new Date(s.time).getTime();
      let bestIndex = -1;
      let minDiff = Infinity;
      bars.forEach((b, idx) => {
        const bTime = new Date(b.time).getTime();
        const diff = Math.abs(bTime - sigTime);
        if (diff < minDiff) {
          minDiff = diff;
          bestIndex = idx;
        }
      });

      // Align signal if the matched candle time is within timeframe tolerance (5m) and in viewport
      if (bestIndex !== -1 && minDiff < 5 * 60 * 1000) {
        if (bestIndex >= start && bestIndex < end) {
          const k = bestIndex - start;
          const b = bars[bestIndex];
          const x = pad + k * colW + candleW / 2;
          const isBuy = s.side === "BUY";
          
          // Draw execution circle badge
          const y = isBuy ? scaleY(b.low) + 14 : scaleY(b.high) - 14;
          
          ctx.fillStyle = isBuy ? "#57d68d" : "#ff7468";
          ctx.beginPath();
          ctx.arc(x, y, 9, 0, 2 * Math.PI);
          ctx.fill();
          
          ctx.fillStyle = "#ffffff";
          ctx.font = "bold 10px sans-serif";
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(isBuy ? "B" : "S", x, y);
        }
      }
    });
  }
}

function renderSummary(summary, runningTf) {
  const items = [
    ["Bars", summary.bars],
    ["Buy", summary.buy_count],
    ["Sell", summary.sell_count],
    ["Close", money(summary.last_close)],
    ["ADX", money(summary.last_adx)],
    ["VWAP", money(summary.last_vwap)],
    ["Trend", summary.last_trend],
    ["Time Frame", runningTf ? `${runningTf} Min` : "-"],
  ];
  summaryGrid.innerHTML = items.map(([k, v]) => `<div class="metric"><span>${k}</span><strong>${v}</strong></div>`).join("");
}

function renderDayPnl(value, brokerValue) {
  if (dayPnlBadge) {
    dayPnlBadge.textContent = `Algo MTM: ${signedMoney(value || 0)}`;
    dayPnlBadge.classList.toggle("profit", Number(value) > 0);
    dayPnlBadge.classList.toggle("loss", Number(value) < 0);
  }
  if (brokerPnlBadge) {
    brokerPnlBadge.textContent = `Broker MTM: ${signedMoney(brokerValue || 0)}`;
    brokerPnlBadge.classList.toggle("profit", Number(brokerValue) > 0);
    brokerPnlBadge.classList.toggle("loss", Number(brokerValue) < 0);
  }
}

function isRunningSignal(s) {
  const status = (s.status || "").toUpperCase();
  return (
    status !== "SL" &&
    status !== "TP3" &&
    status !== "TSL" &&
    status !== "OPPOSITE" &&
    status !== "CLOSED" &&
    !status.includes("_TSL") &&
    !status.includes("_OPP")
  );
}

function renderSignals(signals, summary) {
  if (!signals || signals.length === 0) {
    signalsBody.innerHTML = `<tr><td colspan="11" style="text-align: center; color: #8d9aa4; padding: 20px; font-weight: 500;">No strategy signals found for this scrip/segment.</td></tr>`;
    const badgeEl = document.querySelector("#runningSignalBadge");
    if (badgeEl) badgeEl.style.display = "none";
    return;
  }

  const sortedSignals = [...(signals || [])].sort((a, b) => {
    const timeA = new Date(a.time).getTime();
    const timeB = new Date(b.time).getTime();
    return timeB - timeA;
  });
  signalsBody.innerHTML = sortedSignals.map((s) => {
    const cls = s.side === "BUY" ? "buy" : "sell";
    // Always show the pure strategy indicator status (OPEN/SL/TP1/TP2/TP3/TSL/OPPOSITE etc.)
    // order_status is the trade execution state (Entry_Pending/Active/Closed etc.) shown as a small badge
    const stratStatus = s.status || "OPEN";
    const orderStat = s.order_status || "";
    const orderBadge = orderStat && orderStat !== stratStatus
      ? ` <span class="order-status-badge order-status-${orderStat.toLowerCase().replace(/_/g,'-')}">${escapeHtml(orderStat)}</span>`
      : "";
    return `<tr>
      <td>${new Date(s.time).toLocaleString()}</td>
      <td class="${cls}">${s.side}</td>
      <td>${s.strike} ${s.option_type}</td>
      <td>${money(s.entry)}</td>
      <td>${money(s.stop_loss)}</td>
      <td>${money(s.tp1)}</td>
      <td>${money(s.tp2)}</td>
      <td>${money(s.tp3)}</td>
      <td>${money(s.tsl)}</td>
      <td>${escapeHtml(stratStatus)}${orderBadge}</td>
      <td class="${pnlClass(s.pnl)}">${signedMoney(s.pnl)}</td>
    </tr>`;
  }).join("");

  const badgeEl = document.querySelector("#runningSignalBadge");
  if (!badgeEl) return;

  const running = sortedSignals.find((s) => isRunningSignal(s));
  const ltp = summary?.last_close;

  if (running && ltp !== null && ltp !== undefined) {
    const entry = Number(running.entry);
    const direction = running.side === "BUY" ? 1 : -1;
    const pointsChange = (Number(ltp) - entry) * direction;
    const percentChange = (pointsChange / entry) * 100;

    const signedPts = `${pointsChange >= 0 ? "+" : ""}${money(pointsChange)}`;
    const signedPct = `${percentChange >= 0 ? "+" : ""}${percentChange.toFixed(2)}%`;
    badgeEl.textContent = `Ltp ${money(ltp)} (${signedPts} / ${signedPct})`;
    
    badgeEl.className = "runningSignalBadge";
    if (pointsChange > 0) {
      badgeEl.classList.add("profit");
    } else if (pointsChange < 0) {
      badgeEl.classList.add("loss");
    }
    badgeEl.style.display = "inline-flex";
  } else {
    badgeEl.style.display = "none";
  }
}

function renderOrders(orders) {
  const allOrders = orders || [];
  window.lastOrdersList = allOrders;
  
  // Find the first running order to adjust
  const activeOrder = allOrders.find((o) =>
    ["Active", "Entry_Pending", "Exit_Pending", "Idle"].includes(o.status)
  );
  const adjustRow = document.querySelector("#activeOrderAdjustRow");
  if (activeOrder && adjustRow) {
    const currentKey = orderKey(activeOrder);
    const activeOrderChanged = (activeOrderKeyToAdjust !== currentKey);
    activeOrderKeyToAdjust = currentKey;
    if (activeOrderChanged) {
      adjustmentsDirty = false;
      adjustSLDirty = false;
      adjustTargetDirty = false;
    }
    document.querySelector("#activeTradeLabel").textContent = `${activeOrder.scripname} ${activeOrder.option_type || ""} ${activeOrder.strike || ""} (${activeOrder.status})`;
    
    const slInput = document.querySelector("#adjustSL");
    const tgtInput = document.querySelector("#adjustTarget");
    const trailSelect = document.querySelector("#adjustTrail");
    const trailStartInput = document.querySelector("#adjustTrailStart");
    const trailWhenInput = document.querySelector("#adjustTrailWhen");
    const trailMoveInput = document.querySelector("#adjustTrailMove");
    const costCheckbox = document.querySelector("#adjustMoveToCost");
    const costPointsInput = document.querySelector("#adjustMoveToCostPoints");
    
    // Only update input values if active order has changed, or if focus is not within the adjust block and adjustments are not dirty/user is not editing
    const shouldUpdateInputs = activeOrderChanged || (!adjustmentsDirty && !userEditingAdjustments && !adjustRow.contains(document.activeElement));
    
    if (shouldUpdateInputs) {
      slInput.value = editValue(activeOrder.stoploss);
      tgtInput.value = editValue(activeOrder.target);
      adjustSLDirty = false;
      adjustTargetDirty = false;
      trailSelect.value = activeOrder.trailing_stoploss ? "YES" : "NO";
      
      const tStart = activeOrder.trail_start_value;
      trailStartInput.value = editValue(tStart !== null && tStart !== undefined ? tStart : 0);
      
      const tWhen = activeOrder.trail_when_moves_by;
      trailWhenInput.value = editValue(tWhen !== null && tWhen !== undefined ? tWhen : 1);
      
      const tMove = activeOrder.trail_move_sl_by;
      if (trailMoveInput) {
        trailMoveInput.value = editValue(tMove !== null && tMove !== undefined ? tMove : 1);
      }
      
      costCheckbox.checked = !!activeOrder.sl_moved_to_cost;
      
      if (costPointsInput) {
        const cPoints = activeOrder.move_sl_to_cost_points;
        costPointsInput.value = editValue(cPoints !== null && cPoints !== undefined ? cPoints : 10);
      }
      
      // Initialize manual trade controls
      const lotSize = getActiveOrderLotSize();
      const adjustQtyInput = document.querySelector("#adjustQty");
      const adjustQtyLotsLabel = document.querySelector("#adjustQtyLots");
      if (adjustQtyInput && typeof userEditingManualQty !== "undefined" && !userEditingManualQty) {
        adjustQtyInput.value = lotSize;
        if (adjustQtyLotsLabel) {
          adjustQtyLotsLabel.textContent = "1 lot";
        }
      }
      const adjustPriceInput = document.querySelector("#adjustPrice");
      if (adjustPriceInput && typeof userEditingManualPrice !== "undefined" && !userEditingManualPrice) {
        adjustPriceInput.value = "At Mkt";
      }
    }
    
    // Update Pending Manual Order Status Block
    const pendingBlock = document.querySelector("#pendingManualBlock");
    const pendingStatus = document.querySelector("#pendingManualStatus");
    if (pendingBlock && pendingStatus) {
      const pOrders = activeOrder.pending_manual_orders || [];
      if (pOrders.length > 0) {
        const pOrder = pOrders[0];
        pendingStatus.textContent = `Pending Manual ${pOrder.action} Limit @ ${Number(pOrder.price).toFixed(2)} (Qty: ${pOrder.quantity})`;
        pendingBlock.style.display = "flex";
      } else {
        pendingBlock.style.display = "none";
      }
    }
    
    adjustRow.style.display = "flex";
    updateControlStates();
  } else {
    // No active order — always hide the adjust row immediately.
    activeOrderKeyToAdjust = null;
    if (adjustRow) adjustRow.style.display = "none";
    const pendingBlock = document.querySelector("#pendingManualBlock");
    if (pendingBlock) pendingBlock.style.display = "none";
  }

  const activeRank = (status) => (["Active", "Entry_Pending", "Exit_Pending", "Idle"].includes(status) ? 0 : 1);
  const sortedOrders = [...allOrders].sort((a, b) => {
    const rankA = activeRank(a.status);
    const rankB = activeRank(b.status);
    if (rankA !== rankB) return rankA - rankB;
    const timeA = new Date(a.entry_time || a.time || 0).getTime();
    const timeB = new Date(b.entry_time || b.time || 0).getTime();
    return timeB - timeA;
  });
  
  ordersBody.innerHTML = sortedOrders.map((o) => {
    const opt = o.instrument_type === "Option" ? `${o.strike} ${o.option_type} ${o.strike_mode}` : o.instrument_type;
    const contract = escapeHtml(o.tradingsymbol || o.trade_contract?.tradingsymbol || "-");
    const entryRemarks = o.status === "Closed" && o.entry_remarks === "Paper entry active" ? "Paper entry closed" : (o.entry_remarks || o.quote_error);
    const exitRemarks = o.execution_alert || o.exit_remarks || (o.status === "Closed" ? (o.exit_reason || "Closed") : "");
    const intentSummary = o.exit_intent || o.entry_intent;
    const executionText = intentSummary ? `${intentSummary.state} | ${intentSummary.side === "BUY" ? "Max" : "Min"} ${money(intentSummary.boundary)}` : "";
    const remarks = o.exit_remarks || o.entry_remarks || o.quote_error || "";
    const statusTitle = remarks ? ` title="${escapeHtml(remarks)}"` : "";
    // Row class for dimming non-active orders (they stay visible for the intraday session)
    const rowCls = `order-row-${(o.status || "").toLowerCase().replace(/_/g, "-")}`;
    
    return `<tr class="${rowCls}">
      <td>${new Date(o.time).toLocaleString()}</td>
      <td>${escapeHtml(o.scripname)}</td>
      <td>${escapeHtml(o.side)}</td>
      <td>${escapeHtml(opt)}</td>
      <td>${contract}</td>
      <td>${o.quantity}</td>
      <td>${money(o.option_entry)}</td>
      <td>${money(o.option_ltp)}</td>
      <td>${money(o.option_exit)}</td>
      <td>${money(o.initial_stoploss)}</td>
      <td>${money(o.stoploss)}</td>
      <td>${money(o.target)}</td>
      <td${statusTitle}>${escapeHtml(o.status)}</td>
      <td>${escapeHtml(o.entry_order_id || "-")}</td>
      <td>${escapeHtml(entryRemarks || "-")}</td>
      <td>${escapeHtml(o.exit_order_id || "-")}</td>
      <td>${escapeHtml([exitRemarks, executionText].filter(Boolean).join(" | ") || "-")}</td>
      <td class="${pnlClass(o.pnl)}">${signedMoney(o.pnl)}</td>
    </tr>`;
  }).join("");
}

function renderIndexQuote(prefix, quote) {
  const ltpEl = document.querySelector(`#${prefix}Quote`);
  const changeEl = document.querySelector(`#${prefix}Change`);
  ltpEl.textContent = money(quote?.ltp);
  changeEl.textContent = `${signedMoney(quote?.change)} (${signedPercent(quote?.change_percent)})`;
  changeEl.classList.toggle("profit", Number(quote?.change) > 0);
  changeEl.classList.toggle("loss", Number(quote?.change) < 0);
}

function renderResult(data, runningTf) {
  const result = data.strategy || data;
  renderSummary(result.summary, runningTf);
  renderSignals(result.signals, result.summary);
  announceSignalEvents(result.signals);
  drawChart(result.bars, result.signals);
}

function clearLiveTables() {
  signalsBody.innerHTML = "";
  ordersBody.innerHTML = "";
}


// ==============================================================================
// === SECTION 5: BACKEND REST-API CONNECTIONS ===
// ==============================================================================

async function refreshStatus() {
  const res = await apiFetch("/api/status");
  const json = await res.json();
  const s = json.data;
  renderConnectionStatus(s);
}

async function refreshRunner() {
  const res = await apiFetch("/api/intraday/status");
  if (!res.ok) return;
  const json = await res.json();
  const data = json.data;
  const activeMode = data.trade_mode || tradeMode;
  
  runnerActive = Boolean(data.active);
  document.querySelector("#stopRunner").textContent = data.entries_enabled ? "Pause entries" : "Entries paused";
  tradeMode = activeMode;
  const updateTime = timeOnly(data.last_order_update || data.last_update);
  
  if (lastRunnerActiveState !== null && lastRunnerActiveState !== runnerActive) {
    const minutes = data.request?.timeframe_minutes || document.querySelector("#timeframe")?.value || "";
    speakAlert(runnerActive ? `Algo running. ${minutes} minute candle activated.` : "Algo stopped.");
  }
  lastRunnerActiveState = runnerActive;
  renderModeBadge(activeMode, runnerActive, updateTime);
  document.querySelector("#ordersHeading").textContent = `${activeMode === "REAL" ? "Real" : "Paper"} Orders`;
  
  renderDayPnl(data.day_pnl || 0, data.broker_day_pnl || 0);
  renderOrders(data.paper_orders || []);
  announceOrderEvents(data.paper_orders || []);
  
  if (runnerActive) {
    if (data.strategy) renderResult(data.strategy, data.request?.timeframe_minutes);
    if (!orderTimer) startOrderTimer();
    if (!runnerTimer) {
      runnerTimer = setInterval(refreshRunner, 3000);
    }
  } else {
    signalsBody.innerHTML = "";
    const badgeEl = document.querySelector("#runningSignalBadge");
    if (badgeEl) badgeEl.style.display = "none";
    stopOrderTimer();
  }
  
  if (data.last_error) {
    statusEl.textContent = `${activeMode === "REAL" ? "Real" : "Paper"} mode | ${data.phase}: ${data.last_error}`;
  } else if (data.last_order_error) {
    statusEl.textContent = `${activeMode === "REAL" ? "Real" : "Paper"} mode | ${updateTime || "-"} | Order watcher error: ${data.last_order_error}`;
  } else if (data.last_skip_reason) {
    statusEl.textContent = `${activeMode === "REAL" ? "Real" : "Paper"} mode | ${updateTime || "-"} | ${data.last_skip_reason}`;
  } else if (data.active) {
    statusEl.textContent = `${activeMode === "REAL" ? "Real" : "Paper"} mode | ${updateTime || "-"} | Running OK | ${data.phase || "RUNNING"}`;
  } else {
    statusEl.textContent = `${activeMode === "REAL" ? "Real" : "Paper"} mode | ${updateTime || "-"} | STOPPED`;
  }
}

async function refreshOrdersOnly() {
  // Cancel any previous in-flight request so stale responses never overwrite fresh ones
  if (_ordersAbortController) {
    _ordersAbortController.abort();
  }
  _ordersAbortController = new AbortController();
  try {
    const res = await apiFetch("/api/intraday/orders", { signal: _ordersAbortController.signal });
    if (!res.ok) return;
    const json = await res.json();
    const data = json.data;
    if (!data.active) {
      renderModeBadge(data.trade_mode || tradeMode, false, timeOnly(data.last_order_update));
      renderDayPnl(data.day_pnl || 0, data.broker_day_pnl || 0);
      renderOrders(data.paper_orders || []);
      stopOrderTimer();
      return;
    }
    const updateTime = timeOnly(data.last_order_update);
    renderModeBadge(data.trade_mode || tradeMode, true, updateTime);
    renderDayPnl(data.day_pnl || 0, data.broker_day_pnl || 0);
    renderOrders(data.paper_orders || []);
    announceOrderEvents(data.paper_orders || []);
    if (data.last_order_error) statusEl.textContent = `${data.trade_mode || tradeMode} mode | ${updateTime || "-"} | Order watcher error: ${data.last_order_error}`;
  } catch (err) {
    if (err.name === "AbortError") return;  // Intentionally cancelled — not an error
    // Silently ignore transient network errors during polling
  } finally {
    _ordersAbortController = null;
  }
}

async function refreshIndexQuotes() {
  try {
    const res = await apiFetch("/api/index-quotes");
    if (!res.ok) return;
    const json = await res.json();
    const quotes = json.data || [];
    const nifty = quotes.find((q) => q.name === "NIFTY");
    const banknifty = quotes.find((q) => q.name === "BANKNIFTY");
    const sensex = quotes.find((q) => q.name === "SENSEX");
    const bankex = quotes.find((q) => q.name === "BANKEX");
    renderIndexQuote("nifty", nifty);
    renderIndexQuote("banknifty", banknifty);
    renderIndexQuote("sensex", sensex);
    renderIndexQuote("bankex", bankex);
  } catch {
    document.querySelector("#niftyQuote").textContent = "-";
    document.querySelector("#bankniftyQuote").textContent = "-";
    document.querySelector("#sensexQuote").textContent = "-";
    document.querySelector("#bankexQuote").textContent = "-";
    document.querySelector("#niftyChange").textContent = "-";
    document.querySelector("#bankniftyChange").textContent = "-";
    document.querySelector("#sensexChange").textContent = "-";
    document.querySelector("#bankexChange").textContent = "-";
  }
}

async function loadInstrumentMetadata() {
  const exchange = document.querySelector("#exchange").value;
  const symbol = document.querySelector("#symbol").value;
  const underlying = document.querySelector("#underlying").value.trim().toUpperCase();
  if (!underlying) return;
  
  try {
    const params = new URLSearchParams({ exchange, symbol, underlying });
    const res = await apiFetch(`/api/instruments/metadata?${params.toString()}`);
    if (!res.ok) return;
    
    const json = await res.json();
    const meta = json.data || {};
    
    if (meta.lot_size) {
      document.querySelector("#lotSize").value = meta.lot_size;
      document.querySelector("#qty").value = symbol === "Spot" ? 1 : Number(document.querySelector("#optionQtyLots").value) * Number(meta.lot_size);
    }
    if (meta.strike_gap) document.querySelector("#strikeInterval").value = meta.strike_gap;
    if (meta.tick_size) document.querySelector("#tickSize").value = Number(meta.tick_size).toFixed(2);
    
    const expiry = document.querySelector("#optionExpiry");
    const current = expiry.value;
    const expiries = meta.expiries || [];
    
    expiry.innerHTML = `<option>CURRENT_WEEK</option>${expiries.map((item) => `<option>${escapeHtml(item)}</option>`).join("")}`;
    expiry.value = expiries.includes(current) && current !== "CURRENT_WEEK" ? current : (meta.default_expiry || "CURRENT_WEEK");
  } catch {
    // Keep fallback defaults when instrument metadata is unavailable.
  }
}

async function loadUnderlyingSuggestions() {
  const exchange = document.querySelector("#exchange").value;
  try {
    const res = await apiFetch(`/api/instruments/underlyings?exchange=${exchange}`);
    if (!res.ok) return;
    const json = await res.json();
    window.currentExchangeSymbols = json.data || [];
    updateUnderlyingDatalist("");
  } catch (err) {
    console.error("Error loading underlying suggestions:", err);
  }
}

function updateUnderlyingDatalist(query) {
  const val = (query || "").trim().toUpperCase();
  const datalist = document.querySelector("#underlyings-list");
  if (!datalist) return;
  const symbols = window.currentExchangeSymbols || [];
  let matches = [];
  if (!val) {
    // Show exchange-specific defaults when the field is empty
    const exchange = (document.querySelector("#exchange")?.value || "NSE").toUpperCase();
    let defaults = [];
    if (exchange === "BFO" || exchange === "BSE") {
      defaults = ["SENSEX", "BANKEX", "SENSEX50", "RELIANCE", "HDFCBANK"];
    } else if (exchange === "CDS") {
      defaults = ["USDINR", "EURINR", "GBPINR", "JPYINR"];
    } else if (exchange === "MCX") {
      defaults = ["GOLD", "SILVER", "CRUDEOIL", "NATURALGAS", "COPPER"];
    } else {
      // NSE / NFO and anything else — show Nifty family first
      defaults = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"];
    }
    matches = defaults.filter(d => symbols.some(s => s.toUpperCase() === d));
    if (matches.length < 10) {
      matches = matches.concat(symbols.slice(0, 100));
      matches = [...new Set(matches)];
    }
  } else {
    // Prefix match first, then substring fallback
    matches = symbols.filter(sym => sym.toUpperCase().startsWith(val));
    if (matches.length === 0) {
      matches = symbols.filter(sym => sym.toUpperCase().includes(val));
    }
    matches = matches.slice(0, 100);
  }
  datalist.innerHTML = matches.map(item => `<option value="${escapeHtml(item)}">`).join("");
}

async function showError(response) {
  const text = await response.text();
  try {
    alert(JSON.parse(text).detail || text);
  } catch {
    alert(text);
  }
}


// ==============================================================================
// === SECTION 6: CONTROLLER INPUT CONFIG DATA ASSEMBLE ===
// ==============================================================================

function runnerPayload() {
  const instrument = document.querySelector("#symbol").value;
  const lots = Number(document.querySelector("#optionQtyLots").value);
  const lotSize = Number(document.querySelector("#lotSize").value);
  const qty = instrument === "Spot" ? Number(document.querySelector("#qty").value) : lots * lotSize;
  const entryLimitRaw = document.querySelector("#entryLimitPrice").value.trim();
  const exitLimitRaw = document.querySelector("#exitLimitPrice").value.trim();
  
  return {
    exchange: document.querySelector("#exchange").value.trim(),
    underlying: document.querySelector("#underlying").value.trim(),
    symbol: instrument,
    timeframe_minutes: Number(document.querySelector("#timeframe").value),
    qty,
    trading_mode: document.querySelector("#tradingMode").value,
    option_strategy: document.querySelector("#optionStrategy").value,
    option_moneyness: Number(document.querySelector("#optionMoneyness").value),
    option_expiry: document.querySelector("#optionExpiry").value.trim() || "CURRENT_WEEK",
    option_qty_lots: Number(document.querySelector("#optionQtyLots").value),
    start_time: document.querySelector("#startTime").value.trim(),
    exit_time: document.querySelector("#exitTime").value.trim(),
    max_profit: Number(document.querySelector("#maxProfit").value),
    max_loss: Number(document.querySelector("#maxLoss").value),
    max_trades_per_day: Number(document.querySelector("#maxTrades").value),
    order_product_type: document.querySelector("#orderProductType").value,
    sltp_instrument: document.querySelector("#sltpInstrument").value,
    sl_type: document.querySelector("#slType").value,
    target_type: document.querySelector("#targetType").value,
    stoploss_value: Number(document.querySelector("#stoplossValue").value),
    target_value: Number(document.querySelector("#targetValue").value),
    trailing_stoploss: document.querySelector("#trailingStoploss").value === "YES",
    trail_start_type: document.querySelector("#trailStartType").value,
    trail_start_value: Number(document.querySelector("#trailStartValue").value),
    trail_when_moves_by: Number(document.querySelector("#trailWhenMovesBy").value),
    trail_move_sl_by: Number(document.querySelector("#trailMoveSlBy").value),
    move_sl_to_cost: document.querySelector("#moveSlToCost").value === "YES",
    move_sl_to_cost_points: Number(document.querySelector("#moveSlToCostPoints").value || 0),
    entry_order_mode: document.querySelector("#entryOrderMode").value,
    entry_limit_price: entryLimitRaw === "" ? null : Number(entryLimitRaw),
    exit_order_mode: document.querySelector("#exitOrderMode").value,
    exit_limit_price: exitLimitRaw === "" ? null : Number(exitLimitRaw),
    continuous_execution: true,
    entry_slippage_pct: Number(document.querySelector("#entrySlippagePct").value),
    exit_slippage_pct: Number(document.querySelector("#exitSlippagePct").value),
    protective_exit_slippage_pct: Number(document.querySelector("#protectiveExitSlippagePct").value),
    entry_slippage_points: document.querySelector("#entrySlippagePoints").value === "" ? null : Number(document.querySelector("#entrySlippagePoints").value),
    exit_slippage_points: document.querySelector("#exitSlippagePoints").value === "" ? null : Number(document.querySelector("#exitSlippagePoints").value),
    protective_exit_slippage_points: document.querySelector("#protectiveExitSlippagePoints").value === "" ? null : Number(document.querySelector("#protectiveExitSlippagePoints").value),
    entry_timeout_seconds: Number(document.querySelector("#entryTimeoutSeconds").value),
    max_entry_spread_pct: Number(document.querySelector("#maxEntrySpreadPct").value),
    execution_reprice_seconds: Number(document.querySelector("#executionRepriceSeconds").value),
    execution_max_attempts: Number(document.querySelector("#executionMaxAttempts").value),
    enable_price_chasing: document.querySelector("#enablePriceChasing").value === "YES",
    chase_max_retries: Number(document.querySelector("#chaseMaxRetries").value),
    chase_timeout_seconds: Number(document.querySelector("#chaseTimeoutSeconds").value),
    chase_slippage_pct: Number(document.querySelector("#chaseSlippagePct").value),
    chase_sweep_market: document.querySelector("#chaseSweepMarket").value === "YES",
    strike_interval: Number(document.querySelector("#strikeInterval").value),
    tick_size: Number(document.querySelector("#tickSize").value || 0.05),
    poll_seconds: 1,
    order_watch_seconds: 0.5,
    lookback_days: 15,
    live_trade: tradeMode === "REAL",
    paper_trade: tradeMode !== "REAL",
    config: chartConfigPayload(),
  };
}

function chartConfigPayload() {
  return {
    ss_length: Number(document.querySelector("#ssLength").value),
    ss_target: Number(document.querySelector("#ssTarget").value),
    ss_target1_mult: Number(document.querySelector("#ssTarget1Mult").value),
    ss_target2_mult: Number(document.querySelector("#ssTarget2Mult").value),
    ss_target3_mult: Number(document.querySelector("#ssTarget3Mult").value),
    use_adx_filter: document.querySelector("#useAdxFilter").value === "YES",
    adx_length: Number(document.querySelector("#adxLength").value),
    adx_threshold: Number(document.querySelector("#adxThreshold").value),
    entry_lookback: Number(document.querySelector("#entryLookback").value),
    st_atr_len: Number(document.querySelector("#stAtrLen").value),
    st_factor: Number(document.querySelector("#stFactor").value),
    strike_interval: Number(document.querySelector("#strikeInterval").value),
  };
}


// ==============================================================================
// === SECTION 7: CONTROL STATE SYNCHRONIZERS ===
// ==============================================================================

function setTradeMode(mode) {
  tradeMode = mode;
  const isReal = mode === "REAL";
  document.querySelector("#paperMode").classList.toggle("active", !isReal);
  document.querySelector("#realMode").classList.toggle("active", isReal);
  document.querySelector("#realMode").classList.toggle("real", isReal);
  document.querySelector("#startRunner").textContent = isReal ? "Start Real Algo" : "Start Paper Algo";
  document.querySelector("#ordersHeading").textContent = isReal ? "Real Orders" : "Paper Orders";
  document.querySelector("#modeNote").textContent = isReal
    ? "Real Trade mode sends live broker limit orders only when LIVE_TRADING_ENABLED=true."
    : "Paper Trade mode follows the same order-management states locally.";
  renderModeBadge(mode, runnerActive);
  speakAlert(`Trade mode updated to ${isReal ? "Real Trade" : "Paper Trade"}.`);
}

function renderModeBadge(mode, isActive = false, updateTime = "") {
  const isReal = mode === "REAL";
  modeBadge.classList.toggle("real", isReal);
  modeBadge.classList.toggle("paper", !isReal);
  modeBadge.classList.toggle("running", Boolean(isActive));
  modeBadge.classList.toggle("stopped", !isActive);
  const state = isActive ? "RUN" : "STOP";
  modeBadge.textContent = `${isReal ? "REAL" : "PAPER"} ${state}${updateTime ? ` ${updateTime}` : ""}`;
  modeBadge.title = `${isReal ? "REAL" : "PAPER"} TRADE ${isActive ? "RUNNING" : "STOPPED"}${updateTime ? ` at ${updateTime}` : ""}`;
}

function updateQuantityDefaults() {
  const underlying = document.querySelector("#underlying").value.trim().toUpperCase();
  const instrument = document.querySelector("#symbol").value;
  const lotSize = instrument === "Spot" ? 1 : (lotSizes[underlying] || Number(document.querySelector("#lotSize").value) || 1);
  const strikeGap = strikeGaps[underlying] || 50;
  
  document.querySelector("#lotSize").value = lotSize;
  document.querySelector("#qty").value = instrument === "Spot"
    ? 1
    : Number(document.querySelector("#optionQtyLots").value) * lotSize;
  document.querySelector("#strikeInterval").value = strikeGap;
  
  updateControlStates();
  scheduleInstrumentMetadata();
}

function scheduleInstrumentMetadata() {
  if (instrumentMetaTimer) clearTimeout(instrumentMetaTimer);
  instrumentMetaTimer = setTimeout(loadInstrumentMetadata, 250);
}

function updateControlStates() {
  const instrument = document.querySelector("#symbol").value;
  const optionControlsEnabled = instrument === "Option";
  const datedInstrument = instrument === "Option" || instrument === "Future";
  const strategySltp = document.querySelector("#sltpInstrument").value === "strategy";
  
  document.querySelector("#optionExpiry").disabled = !datedInstrument;
  document.querySelector("#optionMoneyness").disabled = instrument !== "Option";
  document.querySelector("#strikeInterval").disabled = !optionControlsEnabled;
  document.querySelector("#entryLimitPrice").disabled = !["Limit_Below", "Limit_Above"].includes(document.querySelector("#entryOrderMode").value);
  document.querySelector("#exitLimitPrice").disabled = !["Limit_Below", "Limit_Above"].includes(document.querySelector("#exitOrderMode").value);
  document.querySelector("#moveSlToCostPoints").disabled = document.querySelector("#moveSlToCost").value !== "YES";
  
  const chaseEnabled = document.querySelector("#enablePriceChasing").value === "YES";
  ["#chaseSlippagePct", "#chaseMaxRetries", "#chaseTimeoutSeconds", "#chaseSweepMarket"].forEach((selector) => {
    const el = document.querySelector(selector);
    if (el) el.disabled = !chaseEnabled;
  });
  
  ["#slType", "#targetType", "#stoplossValue", "#targetValue", "#trailingStoploss", "#trailStartType", "#trailStartValue", "#trailWhenMovesBy", "#trailMoveSlBy", "#moveSlToCost", "#moveSlToCostPoints"].forEach((selector) => {
    const el = document.querySelector(selector);
    if (el) el.disabled = strategySltp || (selector === "#moveSlToCostPoints" && document.querySelector("#moveSlToCost").value !== "YES");
  });

  const adjustTrailEl = document.querySelector("#adjustTrail");
  if (adjustTrailEl) {
    const trailActive = adjustTrailEl.value === "YES";
    ["#adjustTrailStart", "#adjustTrailWhen", "#adjustTrailMove"].forEach((sel) => {
      const el = document.querySelector(sel);
      if (el) el.disabled = !trailActive;
    });
  }
  const adjustMoveToCostEl = document.querySelector("#adjustMoveToCost");
  if (adjustMoveToCostEl) {
    const costActive = adjustMoveToCostEl.checked;
    const el = document.querySelector("#adjustMoveToCostPoints");
    if (el) el.disabled = costActive;
  }
}

function startOrderTimer() {
  if (orderTimer) clearInterval(orderTimer);
  orderTimer = setInterval(refreshOrdersOnly, 500);
}

function stopOrderTimer() {
  if (orderTimer) clearInterval(orderTimer);
  orderTimer = null;
}


// ==============================================================================
// === SECTION 8: DOM EVENT LISTENERS ===
// ==============================================================================

document.querySelector("#submitAdjust").addEventListener("click", async () => {
  if (!activeOrderKeyToAdjust) return;
  enableVoiceAlerts();
  
  // Blur any focused element inside the activeOrderAdjustRow block to allow subsequent poll sync
  const adjustRow = document.querySelector("#activeOrderAdjustRow");
  if (adjustRow && document.activeElement && adjustRow.contains(document.activeElement)) {
    document.activeElement.blur();
  }
  
  const slVal = document.querySelector("#adjustSL").value.trim();
  const tgtVal = document.querySelector("#adjustTarget").value.trim();
  const trailActive = document.querySelector("#adjustTrail").value === "YES";
  const trailStart = document.querySelector("#adjustTrailStart").value.trim();
  const trailWhen = document.querySelector("#adjustTrailWhen").value.trim();
  const trailMoveEl = document.querySelector("#adjustTrailMove");
  const trailMove = trailMoveEl ? trailMoveEl.value.trim() : "";
  const costActive = document.querySelector("#adjustMoveToCost").checked;
  const costPointsEl = document.querySelector("#adjustMoveToCostPoints");
  const costPoints = costPointsEl ? costPointsEl.value.trim() : "";

  if (slVal === "" || tgtVal === "") {
    alert("Please enter SL and Target prices.");
    return;
  }
  const payload = {
    order_key: activeOrderKeyToAdjust,
    trailing_stoploss: trailActive,
    trail_start_value: trailStart === "" ? 0 : Number(trailStart),
    trail_when_moves_by: trailWhen === "" ? 0 : Number(trailWhen),
    move_sl_to_cost: costActive,
  };
  if (trailMoveEl) {
    payload.trail_move_sl_by = trailMove === "" ? 0 : Number(trailMove);
  }
  if (costPointsEl) {
    payload.move_sl_to_cost_points = costPoints === "" ? 0 : Number(costPoints);
  }
  if (adjustSLDirty) {
    payload.stoploss = Number(slVal);
  }
  if (adjustTargetDirty) {
    payload.target = Number(tgtVal);
  }
  const res = await apiFetch("/api/intraday/order-adjust", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) return showError(res);
  adjustmentsDirty = false;
  adjustSLDirty = false;
  adjustTargetDirty = false;
  await refreshOrdersOnly();
});

["#adjustSL", "#adjustTarget", "#adjustTrailStart", "#adjustTrailWhen", "#adjustTrailMove", "#adjustMoveToCostPoints"].forEach((selector) => {
  const el = document.querySelector(selector);
  if (el) {
    el.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.target.blur();
        document.querySelector("#submitAdjust").click();
      }
    });
    el.addEventListener("input", () => {
      adjustmentsDirty = true;
      if (selector === "#adjustSL") adjustSLDirty = true;
      if (selector === "#adjustTarget") adjustTargetDirty = true;
    });
  }
});

["#adjustTrail", "#adjustMoveToCost"].forEach((selector) => {
  const el = document.querySelector(selector);
  if (el) {
    el.addEventListener("change", () => {
      adjustmentsDirty = true;
    });
  }
});

// Control Button Triggers
document.querySelector("#startRunner").addEventListener("click", async () => {
  enableVoiceAlerts();
  const payload = runnerPayload();
  if (!payload.underlying) {
    alert("Enter underlying.");
    return;
  }
  const res = await apiFetch("/api/intraday/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) return showError(res);
  await refreshRunner();
  if (runnerTimer) clearInterval(runnerTimer);
  runnerTimer = setInterval(refreshRunner, 3000);
  startOrderTimer();
});

document.querySelector("#exitOpenOrder").addEventListener("click", async () => {
  enableVoiceAlerts();
  const res = await apiFetch("/api/intraday/exit-open", { method: "POST" });
  if (!res.ok) return showError(res);
  await refreshRunner();
});

document.querySelector("#stopRunner").addEventListener("click", async () => {
  enableVoiceAlerts();
  const res = await apiFetch("/api/intraday/stop", { method: "POST" });
  if (!res.ok) return showError(res);
  await refreshRunner();
});

document.querySelector("#checkConnection").addEventListener("click", async () => {
  enableVoiceAlerts();
  const isConnected = lastConnectionState;
  const brokerName = (window.selectedBroker || "zebu").toUpperCase();

  if (isConnected) {
    // Perform Logout
    connectionBadge.textContent = `DISCONNECTING FROM ${brokerName}...`;
    connectionBadge.classList.add("checking");
    try {
      const res = await apiFetch("/api/disconnect", { method: "POST" });
      if (res.ok) {
        if (loginStatusText) {
          loginStatusText.textContent = "";
          loginStatusText.className = "loginStatusText";
        }
        await refreshStatus();
      }
    } catch (err) {
      console.error("Logout failed:", err);
    }
  } else {
    // Perform Login using credentials
    connectionBadge.textContent = `${brokerName} CONNECTION NOT LIVE - checking...`;
    connectionBadge.classList.add("checking");
    
    const res = await apiFetch("/api/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ broker: window.selectedBroker || "zebu" })
    });
    const json = await res.json();
    if (!res.ok || !json.ok) {
      renderConnectionStatus(json.data || { connected: false, creds_txt_loaded: true, live_trading_enabled: false, last_error: json.error });
      if (json.error) alert(json.error);
      return;
    }
    renderConnectionStatus(json.data);
    await loadInstrumentMetadata();
    await loadUnderlyingSuggestions();
  }
});

document.querySelector("#runZebu").addEventListener("click", async () => {
  enableVoiceAlerts();
  const payload = {
    exchange: document.querySelector("#exchange").value,
    symbol: document.querySelector("#underlying").value,
    interval: Number(document.querySelector("#timeframe").value),
  };
  
  const res = await apiFetch("/api/bars/zebu", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) return showError(res);
  const json = await res.json();
  await refreshStatus();
  renderResult(json.data, payload.interval);
});

// Mode Switches & Toggles
document.querySelector("#paperMode").addEventListener("click", () => {
  enableVoiceAlerts();
  setTradeMode("PAPER");
});

document.querySelector("#realMode").addEventListener("click", () => {
  enableVoiceAlerts();
  setTradeMode("REAL");
});

voiceToggle?.addEventListener("click", () => {
  setVoiceEnabled(!voiceEnabled);
});

// Auto defaults change bindings
["#underlying", "#symbol", "#optionQtyLots", "#lotSize", "#exchange"].forEach((selector) => {
  document.querySelector(selector).addEventListener("input", updateQuantityDefaults);
  document.querySelector(selector).addEventListener("change", updateQuantityDefaults);
});
document.querySelector("#exchange").addEventListener("change", loadUnderlyingSuggestions);
document.querySelector("#underlying").addEventListener("input", (e) => {
  updateUnderlyingDatalist(e.target.value);
});

["#entryOrderMode", "#exitOrderMode", "#enablePriceChasing", "#moveSlToCost", "#trailStartType", "#sltpInstrument", "#adjustTrail", "#adjustMoveToCost"].forEach((selector) => {
  document.querySelector(selector).addEventListener("change", updateControlStates);
});

// Manual Trade adjustments block handler bindings
let userEditingManualQty = false;
let userEditingManualPrice = false;

function getActiveOrderLotSize() {
  const activeOrder = window.lastOrdersList?.find((o) =>
    ["Active", "Entry_Pending", "Exit_Pending", "Idle"].includes(o.status)
  );
  if (activeOrder) {
    if (activeOrder.trade_contract && activeOrder.trade_contract.lot_size) {
      return Number(activeOrder.trade_contract.lot_size);
    }
    const scrip = activeOrder.scripname;
    if (scrip && lotSizes[scrip]) {
      return lotSizes[scrip];
    }
  }
  return Number(document.querySelector("#lotSize").value) || 50;
}

const adjustQtyInput = document.querySelector("#adjustQty");
const adjustQtyLotsLabel = document.querySelector("#adjustQtyLots");
const qtyDecBtn = document.querySelector("#qtyDecBtn");
const qtyIncBtn = document.querySelector("#qtyIncBtn");
const adjustPriceInput = document.querySelector("#adjustPrice");

function updateAdjustQtyLotsLabel() {
  if (!adjustQtyInput || !adjustQtyLotsLabel) return;
  const qty = Number(adjustQtyInput.value) || 0;
  const lotSize = getActiveOrderLotSize();
  const lots = Math.max(1, Math.round(qty / lotSize));
  adjustQtyLotsLabel.textContent = `${lots} lot${lots > 1 ? "s" : ""}`;
}

if (adjustQtyInput) {
  adjustQtyInput.addEventListener("input", () => {
    userEditingManualQty = true;
    updateAdjustQtyLotsLabel();
  });
  adjustQtyInput.addEventListener("blur", () => {
    userEditingManualQty = false;
  });
}

if (adjustPriceInput) {
  adjustPriceInput.addEventListener("input", () => {
    userEditingManualPrice = true;
  });
  adjustPriceInput.addEventListener("blur", () => {
    userEditingManualPrice = false;
  });
}

if (qtyDecBtn) {
  qtyDecBtn.addEventListener("click", () => {
    if (!adjustQtyInput) return;
    const lotSize = getActiveOrderLotSize();
    const currentQty = Number(adjustQtyInput.value) || lotSize;
    const newQty = Math.max(lotSize, currentQty - lotSize);
    adjustQtyInput.value = newQty;
    updateAdjustQtyLotsLabel();
    userEditingManualQty = true;
    setTimeout(() => { userEditingManualQty = false; }, 2000);
  });
}

if (qtyIncBtn) {
  qtyIncBtn.addEventListener("click", () => {
    if (!adjustQtyInput) return;
    const lotSize = getActiveOrderLotSize();
    const currentQty = Number(adjustQtyInput.value) || 0;
    const newQty = currentQty + lotSize;
    adjustQtyInput.value = newQty;
    updateAdjustQtyLotsLabel();
    userEditingManualQty = true;
    setTimeout(() => { userEditingManualQty = false; }, 2000);
  });
}

async function handleManualTrade(action) {
  if (!activeOrderKeyToAdjust) {
    alert("No active trade to adjust.");
    return;
  }
  const qtyVal = Number(adjustQtyInput?.value) || 0;
  if (qtyVal <= 0) {
    alert("Please enter a valid quantity.");
    return;
  }
  const priceVal = adjustPriceInput?.value?.trim() || "At Mkt";
  
  enableVoiceAlerts();
  
  const payload = {
    order_key: activeOrderKeyToAdjust,
    action: action,
    quantity: qtyVal,
    price: priceVal
  };
  
  // Disable buttons while placing order
  const buyBtn = document.querySelector("#manualBuyBtn");
  const sellBtn = document.querySelector("#manualSellBtn");
  if (buyBtn) buyBtn.disabled = true;
  if (sellBtn) sellBtn.disabled = true;
  
  try {
    const res = await apiFetch("/api/intraday/manual-trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      await showError(res);
    } else {
      await refreshOrdersOnly();
    }
  } catch (err) {
    alert(`Error: ${err.message || err}`);
  } finally {
    if (buyBtn) buyBtn.disabled = false;
    if (sellBtn) sellBtn.disabled = false;
  }
}

document.querySelector("#manualBuyBtn")?.addEventListener("click", () => handleManualTrade("BUY"));
document.querySelector("#manualSellBtn")?.addEventListener("click", () => handleManualTrade("SELL"));

async function handleCancelPendingManualTrade() {
  if (!activeOrderKeyToAdjust) return;
  
  const payload = {
    order_key: activeOrderKeyToAdjust
  };
  
  const cancelBtn = document.querySelector("#cancelPendingManualBtn");
  if (cancelBtn) cancelBtn.disabled = true;
  
  try {
    const res = await apiFetch("/api/intraday/cancel-manual-trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      await showError(res);
    } else {
      await refreshOrdersOnly();
    }
  } catch (err) {
    alert(`Error: ${err.message || err}`);
  } finally {
    if (cancelBtn) cancelBtn.disabled = false;
  }
}

document.querySelector("#cancelPendingManualBtn")?.addEventListener("click", handleCancelPendingManualTrade);



// ==============================================================================
// === SECTION 9: TIMERS INITIALIZATION & BOOTSTRAP ===
// ==============================================================================

refreshStatus();
refreshRunner();
refreshIndexQuotes();
quoteTimer = setInterval(refreshIndexQuotes, 1000);

// Set default values and state triggers on start
updateQuantityDefaults();
updateControlStates();
loadInstrumentMetadata();
loadUnderlyingSuggestions();
setTradeMode("PAPER");
setVoiceEnabled(false);

const announceableInputs = {
  "#underlying": "Underlying",
  "#symbol": "Instrument type",
  "#timeframe": "Timeframe",
  "#tradingMode": "Trading mode",
  "#optionStrategy": "Option strategy",
  "#sltpInstrument": "S L T P instrument",
  "#trailingStoploss": "Trailing stop loss",
  "#moveSlToCost": "Move S L to cost",
  "#optionExpiry": "Option expiry",
  "#optionMoneyness": "Option moneyness",
};

Object.entries(announceableInputs).forEach(([selector, label]) => {
  const el = document.querySelector(selector);
  if (el) {
    el.addEventListener("change", (e) => {
      if (appInitialized) {
        const val = e.target.value;
        const displayVal = val === "YES" ? "enabled" : (val === "NO" ? "disabled" : val);
        speakAlert(`${label} updated to ${displayVal}`);
      }
    });
  }
});

// Interactive Chart Zooming and Panning listeners
if (chart) {
  chart.addEventListener("mousedown", (e) => {
    if (!chartState.bars || chartState.bars.length === 0) return;
    chartState.isDragging = true;
    chartState.dragStartPos = e.clientX;
    chartState.dragStartIdxs = { start: chartState.startIdx, end: chartState.endIdx };
    chart.style.cursor = "grabbing";
  });

  chart.addEventListener("mousemove", (e) => {
    if (!chartState.isDragging || !chartState.bars || chartState.bars.length === 0) return;
    const dx = e.clientX - chartState.dragStartPos;
    const visibleCount = chartState.dragStartIdxs.end - chartState.dragStartIdxs.start;
    const colW = (chart.width - 56) / Math.max(1, visibleCount);
    const candleDiff = Math.round(dx / colW);

    let newStart = chartState.dragStartIdxs.start - candleDiff;
    let newEnd = chartState.dragStartIdxs.end - candleDiff;

    if (newStart < 0) {
      newEnd += (0 - newStart);
      newStart = 0;
    }
    if (newEnd > chartState.bars.length) {
      newStart -= (newEnd - chartState.bars.length);
      newEnd = chartState.bars.length;
      if (newStart < 0) newStart = 0;
    }

    chartState.startIdx = newStart;
    chartState.endIdx = newEnd;
    drawChart(chartState.bars, chartState.signals);
  });

  const stopDrag = () => {
    if (chartState.isDragging) {
      chartState.isDragging = false;
      chart.style.cursor = "grab";
    }
  };

  chart.addEventListener("mouseup", stopDrag);
  chart.addEventListener("mouseleave", stopDrag);

  chart.addEventListener("wheel", (e) => {
    if (!chartState.bars || chartState.bars.length === 0) return;
    e.preventDefault();
    const rect = chart.getBoundingClientRect();
    const mouseX = e.clientX - rect.left;
    const mouseRatio = Math.max(0, Math.min(1, (mouseX - 28) / (chart.width - 56)));

    const zoomIntensity = 0.05;
    const currentRange = chartState.endIdx - chartState.startIdx;
    const delta = e.deltaY < 0 ? -1 : 1;
    const change = Math.max(1, Math.round(currentRange * zoomIntensity)) * delta;

    let newStart = chartState.startIdx - Math.round(change * mouseRatio);
    let newEnd = chartState.endIdx + Math.round(change * (1 - mouseRatio));

    if (newEnd - newStart < 10) return;
    if (newStart < 0) newStart = 0;
    if (newEnd > chartState.bars.length) newEnd = chartState.bars.length;

    chartState.startIdx = newStart;
    chartState.endIdx = newEnd;
    drawChart(chartState.bars, chartState.signals);
  }, { passive: false });
}

// ==============================================================================
// === SECTION 8.5: WELCOME LANDING PAGE EVENT HANDLERS ===
// ==============================================================================
window.selectedBroker = "zebu";

const cardZebu = document.querySelector("#brokerCardZebu");
const cardFlattrade = document.querySelector("#brokerCardFlattrade");
const cardUpstox = document.querySelector("#brokerCardUpstox");
const loginSubmitBtn = document.querySelector("#loginSubmitBtn");
const loginStatusText = document.querySelector("#loginStatusText");
const closeLandingBtn = document.querySelector("#closeLandingBtn");

function setSelectBroker(broker) {
  window.selectedBroker = broker;
  if (cardZebu) cardZebu.classList.toggle("active", broker === "zebu");
  if (cardFlattrade) cardFlattrade.classList.toggle("active", broker === "flattrade");
  if (cardUpstox) cardUpstox.classList.toggle("active", broker === "upstox");
}

if (cardZebu) {
  cardZebu.addEventListener("click", () => setSelectBroker("zebu"));
}
if (cardFlattrade) {
  cardFlattrade.addEventListener("click", () => setSelectBroker("flattrade"));
}
if (cardUpstox) {
  cardUpstox.addEventListener("click", () => setSelectBroker("upstox"));
}

if (loginSubmitBtn) {
  loginSubmitBtn.addEventListener("click", async () => {
    enableVoiceAlerts();
    if (loginStatusText) {
      loginStatusText.textContent = "Establishing session, please wait...";
      loginStatusText.className = "loginStatusText";
    }
    loginSubmitBtn.disabled = true;
    try {
      const res = await apiFetch("/api/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ broker: window.selectedBroker })
      });
      const json = await res.json();
      loginSubmitBtn.disabled = false;
      
      if (!res.ok || !json.ok) {
        if (json.error && json.error.startsWith("AUTH_REQUIRED:")) {
          const authUrl = json.error.split("AUTH_REQUIRED:", 2)[1];
          if (loginStatusText) {
            loginStatusText.textContent = "Redirecting to Upstox for manual authorization...";
            loginStatusText.className = "loginStatusText success";
          }
          setTimeout(() => {
            window.location.href = authUrl;
          }, 1500);
          return;
        }
        if (loginStatusText) {
          loginStatusText.textContent = json.error || "Login connection failed. Check credentials or callback redirect.";
          loginStatusText.className = "loginStatusText error";
        }
        renderConnectionStatus(json.data || { connected: false });
      } else {
        if (loginStatusText) {
          loginStatusText.textContent = "Login Successful! Loading dashboard...";
          loginStatusText.className = "loginStatusText success";
        }
        renderConnectionStatus(json.data);
        await refreshRunner();
        await loadInstrumentMetadata();
        await loadUnderlyingSuggestions();
      }
    } catch (err) {
      loginSubmitBtn.disabled = false;
      if (loginStatusText) {
        loginStatusText.textContent = `Error: ${err.message || err}`;
        loginStatusText.className = "loginStatusText error";
      }
    }
  });
}

const homeBtn = document.querySelector("#homeBtn");
if (homeBtn) {
  homeBtn.addEventListener("click", () => {
    const landingPage = document.querySelector("#landingPage");
    if (landingPage) {
      landingPage.classList.remove("hidden");
    }
  });
}

if (closeLandingBtn) {
  closeLandingBtn.addEventListener("click", () => {
    const landingPage = document.querySelector("#landingPage");
    if (landingPage) {
      landingPage.classList.add("hidden");
    }
  });
}

appInitialized = true;

document.querySelector("#reconcileOrders")?.addEventListener("click", async () => {
  const response = await apiFetch("/api/intraday/reconcile", {method: "POST"});
  if (!response.ok) return showError(response);
  await refreshRunner();
});
