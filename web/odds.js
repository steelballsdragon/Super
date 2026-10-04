// Odds math for the parlay calculator. Runs in the browser (window.Odds) and in node (require).
// parsePrice accepts what the bot's /bet command does (sportsbot/bankroll.py parse_price), plus fractions.
(function (root) {
  "use strict";

  /** A price as decimal odds: American (+450, 450, -120), decimal (5.5), fractional (9/2) or "evens". null if it isn't one. */
  function parsePrice(text) {
    const t = String(text || "").trim().toLowerCase().replace(/\s+/g, "");
    if (t === "ev" || t === "even" || t === "evens") return 2;
    const frac = t.match(/^(\d+(?:\.\d+)?)\/(\d+(?:\.\d+)?)$/);
    if (frac) {
      const num = parseFloat(frac[1]), den = parseFloat(frac[2]);
      return num > 0 && den > 0 ? 1 + num / den : null;
    }
    if (!/^[+-]?\d+(\.\d+)?$/.test(t)) return null;
    const value = parseFloat(t);
    const signed = t[0] === "+" || t[0] === "-";
    if (!signed && value > 1 && value < 100) return value; // decimal odds
    if (value >= 100) return 1 + value / 100;
    if (value <= -100) return 1 + 100 / -value;
    return null;
  }

  /** Decimal odds as an American price, rounded like the bot: 2.5 → "+150", 1.5 → "-200". */
  function american(decimal) {
    if (decimal >= 2) return "+" + Math.round(100 * (decimal - 1));
    return "-" + Math.round(100 / (decimal - 1));
  }

  /**
   * Prices a parlay. Each leg is {price: decimal odds, result: "open" | "won" | "push" | "lost"}.
   * A pushed leg comes off the ticket (pays 1.0), as at the book; a lost leg loses the whole bet.
   * Returns the full-ticket price, what's left to win, and the bet's status.
   */
  function settle(legs, stake) {
    const live = legs.filter((leg) => leg.result !== "push");
    const price = live.reduce((p, leg) => p * leg.price, 1);
    const open = legs.filter((leg) => leg.result === "open");
    const lost = legs.some((leg) => leg.result === "lost");
    let status;
    if (lost) status = "lost";
    else if (open.length) status = "open";
    else if (live.length) status = "won";
    else status = "refund";
    const chance = lost ? 0 : open.reduce((c, leg) => c / leg.price, 1);
    const payout = lost ? 0 : stake * price;
    return {
      status,
      price,
      payout,
      profit: payout - stake,
      chance, // implied chance the legs still open all win (1 once settled)
      legs: live.length,
      open: open.length,
    };
  }

  function money(x) {
    const s = "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return x < 0 ? "-" + s : s;
  }

  const api = { parsePrice, american, settle, money };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Odds = api;
})(this);
