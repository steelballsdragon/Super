// Run with: node --test web/odds.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const { parsePrice, american, settle, money } = require("./odds.js");

const close = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} != ${b}`);

test("parses the same prices as the bot", () => {
  close(parsePrice("+450"), 5.5);
  close(parsePrice("450"), 5.5);
  close(parsePrice("-120"), 1 + 100 / 120);
  close(parsePrice("5.5"), 5.5);
  close(parsePrice(" + 150 "), 2.5);
  close(parsePrice("100"), 2);
  close(parsePrice("-100"), 2);
});

test("parses fractions and evens", () => {
  close(parsePrice("9/2"), 5.5);
  close(parsePrice("1/2"), 1.5);
  close(parsePrice("Evens"), 2);
  close(parsePrice("EV"), 2);
});

test("rejects what isn't a price", () => {
  for (const bad of ["", "abc", "-50", "+50", "1", "0.9", "1.0", "5/0", "0/3", "--110", "1e3"]) {
    assert.equal(parsePrice(bad), null, bad);
  }
});

test("formats American prices like the bot", () => {
  assert.equal(american(2), "+100");
  assert.equal(american(5.5), "+450");
  assert.equal(american(1.5), "-200");
  assert.equal(american(1 + 100 / 110), "-110");
});

test("prices an open parlay", () => {
  const r = settle([{ price: 2, result: "open" }, { price: 3, result: "open" }], 10);
  assert.equal(r.status, "open");
  close(r.price, 6);
  close(r.payout, 60);
  close(r.profit, 50);
  close(r.chance, 1 / 6);
});

test("a lost leg loses the bet", () => {
  const r = settle([{ price: 2, result: "won" }, { price: 3, result: "lost" }], 10);
  assert.equal(r.status, "lost");
  close(r.payout, 0);
  close(r.profit, -10);
  close(r.chance, 0);
});

test("a push comes off the ticket", () => {
  const r = settle([{ price: 2, result: "won" }, { price: 3, result: "push" }], 10);
  assert.equal(r.status, "won");
  close(r.price, 2);
  close(r.payout, 20);
  assert.equal(r.legs, 1);
});

test("all pushes refund the stake", () => {
  const r = settle([{ price: 2, result: "push" }, { price: 3, result: "push" }], 10);
  assert.equal(r.status, "refund");
  close(r.payout, 10);
  close(r.profit, 0);
});

test("only open legs count toward the chance", () => {
  const r = settle([{ price: 2, result: "won" }, { price: 4, result: "open" }], 5);
  close(r.chance, 0.25);
  close(r.payout, 40);
});

test("formats money", () => {
  assert.equal(money(1234.5), "$1,234.50");
  assert.equal(money(-10), "-$10.00");
});
