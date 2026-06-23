<?php
declare(strict_types=1);
require_once 'vendor/autoload.php';

// ============================================================
//  CONFIG — High Win-Rate Scalping v4
//
//  STRATEGY : Weighted Confluence Scoring (11 indicators)
//  ─────────────────────────────────────────────────────────
//  Score breakdown (max 11):
//    EMA 9/21 cross  + body-confirmed candle ......... +2
//    EMA 50 trend filter (sloping) ................... +2
//    ADX directional bias (+DI vs -DI) ............... +1
//    Candle confirmation (1 consecutive) ............. +1
//    MACD histogram rising + on correct zero side .... +1
//    RSI mid-zone (45-68 long / 32-55 short) ......... +1
//    StochRSI oversold/overbought cross .............. +1
//    Volume spike ≥1.2× avg20 ....................... +1
//    Bollinger Band edge proximity ................... +1
//    Higher-TF 15m EMA21 slope (bonus) .............. +1
//  ─────────────────────────────────────────────────────────
//  Entry  : score ≥5 immediately | score ≥4 after 120s
//  Exit   : ATR trail only (no fixed TP) — trail from tick 1
//  Trail  : BE@+0.15% → lock+0.15%@+0.35% → ATR×0.7 continuous
//  Timeout: force-close after 15 min if BE never reached
//  Goal   : more trades, smaller risk/trade, higher total PnL
//  DRY_RUN: true = paper | false = live
// ============================================================
const DRY_RUN              = true;
const DRY_PREFIX           = '[DRY-RUN] ';
const SYMBOL               = 'WLD/USDT';
const INTERVAL             = '5m';
const INTERVAL_HTF         = '15m';
const INTERVAL_HTF2        = '1h';    // 1h = hard directional gate (no longs in 1h downtrend)
const KLINE_LIMIT          = 1000;
const KLINE_LIMIT_HTF      = 200;
const KLINE_LIMIT_HTF2     = 100;
const POSITION_USDT        = 20;
const LEVERAGE             = 10;

// ── Indicators ──────────────────────────────────────────────
const EMA_FAST             = 9;
const EMA_SLOW             = 21;
const EMA_TREND            = 50;
const MACD_FAST            = 12;
const MACD_SLOW            = 26;
const MACD_SIGNAL_P        = 9;
const RSI_PERIOD           = 14;
const ATR_PERIOD           = 14;
const BB_PERIOD            = 20;
const BB_STDDEV            = 2.0;

// ── RSI zones ───────────────────────────────────────────────
const RSI_LONG_MIN         = 45;   // widened: was 48
const RSI_LONG_MAX         = 68;   // widened: was 65
const RSI_SHORT_MIN        = 32;   // widened: was 35
const RSI_SHORT_MAX        = 55;   // widened: was 52

// ── Volume ──────────────────────────────────────────────────
const VOL_SPIKE_MULT       = 1.2;   // was 1.5 — fires more often, still filters dead vol

// ── ATR-based risk ──────────────────────────────────────────
// SL multiplier scales with ADX: tight in strong trends, wider in choppy markets.
const ATR_SL_MULT          = 1.0;   // base SL multiplier (used when ADX is in middle range)
const ATR_SL_MULT_STRONG   = 0.8;   // ADX > ADX_STRONG_TREND (25) → tighter SL
const ATR_SL_MULT_WEAK     = 1.3;   // ADX 20-25 (just above chop gate) → wider SL
// No fixed TP — trailing stop is the sole profit-side exit.
const ATR_FALLBACK_SL_PCT  = 0.35;  // % fallback if ATR=0

// ── Trailing stop ────────────────────────────────────────────
const TRAIL_BE_PCT         = 0.15;  // move SL to breakeven sooner (was 0.25)
const TRAIL_LOCK_PCT       = 0.35;  // lock gain sooner (was 0.45)
const TRAIL_LOCKED_GAIN    = 0.15;  // locked gain amount (was 0.20)

// ── Candle quality ───────────────────────────────────────────
const BODY_RATIO_MIN       = 0.55;  // body ≥ 55% of (high-low)

// ── Wick rejection filter ────────────────────────────────────
// If the wick pointing AGAINST the signal direction is > this fraction
// of the total candle range, skip entry — price was rejected at that level.
// E.g. long upper wick > 35% of range on a long signal = bearish rejection.
const WICK_REJECT_MAX      = 0.35;  // upper wick > 35% → reject long; lower wick > 35% → reject short

// ── Bollinger Band proximity & squeeze ───────────────────────
const BB_PROX_RATIO        = 0.20;  // within 20% of band width from the edge (was 0.15)
// BB squeeze: band width as % of price below this = choppy, skip entry
const BB_SQUEEZE_PCT       = 0.60;  // BB width < 0.60% of price → squeeze (was 0.80)

// ── Stochastic RSI ───────────────────────────────────────────
const STOCH_RSI_PERIOD     = 14;    // RSI lookback for StochRSI
const STOCH_RSI_SMOOTH_K   = 3;     // %K smoothing
const STOCH_RSI_SMOOTH_D   = 3;     // %D smoothing
// Zones: long when %K crosses above %D in oversold; short = opposite
const STOCH_LONG_MAX       = 40;    // %K must be below this (oversold) for long bonus
const STOCH_SHORT_MIN      = 60;    // %K must be above this (overbought) for short bonus

// ── Support / Resistance (pivot) ─────────────────────────────
// Looks back this many bars to find recent swing highs/lows
const SR_LOOKBACK          = 50;
// Price must NOT be within this % of a S/R wall on entry
const SR_BUFFER_PCT        = 0.30;  // 0.30% buffer zone around S/R levels

// ── ATR trailing stop (continuous) ───────────────────────────
// After breakeven, trail SL by ATR_TRAIL_MULT × ATR below/above highest seen price
const ATR_TRAIL_MULT       = 0.7;   // trail distance = 0.7 × ATR (tighter for scalping)

// ── ADX (trend strength) ─────────────────────────────────────
const ADX_PERIOD           = 14;
// Minimum ADX value to confirm a real trend exists (not ranging)
// ADX < 20 = ranging/choppy → skip entry entirely
// ADX > 25 = confirmed trend → full scoring applies
const ADX_MIN_TREND        = 20;    // below this → HALT(CHOP)
const ADX_STRONG_TREND     = 25;    // above this → trend confirmed, no penalty

// ── EMA50 slope filter ───────────────────────────────────────
// EMA50 must be sloping (not flat) to count as a real trend
// Measured as % change over last N bars
const EMA_TREND_SLOPE_BARS = 5;     // bars to measure EMA50 slope over
const EMA_TREND_SLOPE_MIN  = 0.05;  // EMA50 must move ≥ 0.05% over 5 bars

// ── Candle confirmation ──────────────────────────────────────
// Require N consecutive candles closing in the signal direction
// before entry — filters single-bar fakeouts.
// 1 = enter on first confirmed candle; trail handles the rest.
const CONFIRM_CANDLES      = 1;     // was 2 — enter earlier, let trail capture the move

// ── Scoring thresholds ───────────────────────────────────────
// Max score is now 10 pts (StochRSI added).
const SCORE_STRONG         = 5;     // enter immediately  (lowered for more entries)
const SCORE_NORMAL         = 4;     // enter after cooldown (lowered for more entries)

// ── EMA cross staleness guard ────────────────────────────────
// Cross must have happened within this many closed bars (else it's stale)
const CROSS_MAX_AGE_BARS   = 5;    // widened: was 3 (more valid entries at tight TP)



// ── Fee / spread filter ──────────────────────────────────────
// Minimum ATR as a % of price to justify entry after fees (0.04% × 2 legs)
// Entry is skipped when market is too flat to cover round-trip cost
const MIN_ATR_PCT          = 0.08;  // lowered: was 0.12 (tighter SL needs less ATR to cover fees)

// ── Circuit-breaker: consecutive losses ──────────────────────
const MAX_CONSEC_LOSSES    = 3;     // pause after this many losses in a row
const CIRCUIT_BREAK_SECS   = 900;  // cool-off: 15 min (was 30 — recover faster at high freq)

// ── Max trade duration (stale position timeout) ───────────────
// If a position is still open after this many seconds with no BE reached,
// close it — it's not moving, capital is locked, and fees are accumulating.
const MAX_TRADE_SECS       = 900;  // 15 minutes = 3 × 5m candles max hold

// ── Daily risk caps ──────────────────────────────────────────
const MAX_DAILY_LOSS_USDT  = 15.0;  // stop trading for the day beyond this loss
const MAX_DAILY_TRADES     = 40;    // max trades per calendar day (raised for scalping)

// ── Timing ──────────────────────────────────────────────────
const COOLDOWN_STRONG      = 60;    // seconds between strong entries (was 180)
const COOLDOWN_NORMAL      = 120;   // seconds for normal entries (was 300)
const LOOP_SLEEP           = 10;   // check market 3× more often (was 30s)
const MIN_NOTIONAL         = 5.5;
const LOG_FILE             = __DIR__ . '/trades.log';

// ============================================================
//  CREDENTIALS
// ============================================================
$apiKey    = getenv('BINANCE_API_KEY');
$apiSecret = getenv('BINANCE_SECRET_KEY');

if (!$apiKey || !$apiSecret) {
  // exit() is acceptable here: this is a CLI entry-point guard, not a class method (S1799)
  fwrite(STDOUT, "[-] Error: set BINANCE_API_KEY and BINANCE_SECRET_KEY in .env\n");
  exit(1);
}

// ============================================================
//  LOGGING
// ============================================================
function logToFile(string $message): void
{
  $line = '[' . date('Y-m-d H:i:s') . '] ' . $message . PHP_EOL;
  echo $line;
  file_put_contents(LOG_FILE, $line, FILE_APPEND | LOCK_EX);
}

// ============================================================
//  EXCHANGE INIT
// ============================================================
$exchange = new \ccxt\binance([
  'apiKey'  => $apiKey,
  'secret'  => $apiSecret,
  'options' => ['defaultType' => 'future'],
]);

// Cache market precision once at boot — avoids load_markets() on every trade open
$cachedAmountPrecision = 2;
if (!DRY_RUN) {
  try {
    $exchange->set_leverage(LEVERAGE, SYMBOL);
    logToFile('[*] Leverage set to x' . LEVERAGE . ' for ' . SYMBOL);
    $markets = $exchange->load_markets();
    $cachedAmountPrecision = (int)($markets[SYMBOL]['precision']['amount'] ?? 2);
    logToFile('[*] Amount precision cached: ' . $cachedAmountPrecision);
  } catch (\Exception $e) {
    logToFile('[!] Boot init: ' . $e->getMessage());
  }
} else {
  logToFile(DRY_PREFIX . 'Leverage x' . LEVERAGE . ' would be set (skipped).');
}

// ============================================================
//  INDICATORS
// ============================================================

/** EMA with proper SMA seed. @param float[] $prices oldest→newest */
function calcEMA(array $prices, int $period): array
{
  $count = count($prices);
  if ($count < $period) {
    return array_fill(0, $count, $prices[0] ?? 0.0);
  }
  $k    = 2 / ($period + 1);
  $seed = array_sum(array_slice($prices, 0, $period)) / $period;
  $ema  = array_fill(0, $period, $seed);
  for ($i = $period; $i < $count; $i++) {
    $ema[] = $prices[$i] * $k + $ema[$i - 1] * (1 - $k);
  }
  return $ema;
}

/** RSI via Wilder smoothing. @param float[] $closes oldest→newest */
function calcRSI(array $closes, int $period): float
{
  if (count($closes) < $period + 2) { return 50.0; }
  $slice = array_slice($closes, -(($period + 1) + $period));
  $total = count($slice);
  $gains = $losses = 0.0;
  for ($i = 1; $i <= $period; $i++) {
    $d = $slice[$i] - $slice[$i - 1];
    if ($d >= 0) { $gains += $d; } else { $losses -= $d; }
  }
  $avgGain = $gains / $period;
  $avgLoss = $losses / $period;
  for ($i = $period + 1; $i < $total; $i++) {
    $d       = $slice[$i] - $slice[$i - 1];
    $avgGain = ($avgGain * ($period - 1) + max($d,  0)) / $period;
    $avgLoss = ($avgLoss * ($period - 1) + max(-$d, 0)) / $period;
  }
  if ($avgLoss < 0.00001) { return 100.0; }
  return 100.0 - (100.0 / (1.0 + $avgGain / $avgLoss));
}

/**
 * ATR via Wilder smoothing.
 * @param float[] $highs  oldest→newest
 * @param float[] $lows   oldest→newest
 * @param float[] $closes oldest→newest
 */
function calcATR(array $highs, array $lows, array $closes, int $period): float
{
  $count = count($closes);
  if ($count < $period + 1) { return 0.0; }
  $trList = [];
  for ($i = 1; $i < $count; $i++) {
    $trList[] = max(
      $highs[$i] - $lows[$i],
      abs($highs[$i] - $closes[$i - 1]),
      abs($lows[$i]  - $closes[$i - 1])
    );
  }
  if (count($trList) < $period) { return 0.0; }
  $atr = array_sum(array_slice($trList, 0, $period)) / $period;
  for ($i = $period; $i < count($trList); $i++) {
    $atr = ($atr * ($period - 1) + $trList[$i]) / $period;
  }
  return $atr;
}

/**
 * Bollinger Bands.
 * @param  float[] $closes oldest→newest
 * @return array{upper:float,middle:float,lower:float,width:float}
 */
function calcBB(array $closes, int $period, float $mult): array
{
  $empty = ['upper' => 0.0, 'middle' => 0.0, 'lower' => 0.0, 'width' => 0.0];
  if (count($closes) < $period) { return $empty; }
  $slice    = array_slice($closes, -$period);
  $mean     = array_sum($slice) / $period;
  $variance = 0.0;
  foreach ($slice as $v) { $variance += ($v - $mean) ** 2; }
  $stddev = sqrt($variance / $period);
  $upper  = $mean + $mult * $stddev;
  $lower  = $mean - $mult * $stddev;
  return ['upper' => $upper, 'middle' => $mean, 'lower' => $lower, 'width' => $upper - $lower];
}

/**
 * MACD histogram + prevHistogram.
 * @param  float[] $closes oldest→newest
 * @return array{macd:float,signal:float,histogram:float,prevHistogram:float}
 */
function calcMACD(array $closes, int $fast, int $slow, int $signal): array
{
  $empty = ['macd' => 0.0, 'signal' => 0.0, 'histogram' => 0.0, 'prevHistogram' => 0.0];
  if (count($closes) < $slow + $signal + 2) { return $empty; }
  $emaFast  = calcEMA($closes, $fast);
  $emaSlow  = calcEMA($closes, $slow);
  $macdLine = [];
  foreach ($emaFast as $i => $v) { $macdLine[] = $v - $emaSlow[$i]; }
  $sigLine  = calcEMA($macdLine, $signal);
  $n        = count($macdLine);
  return [
    'macd'          => $macdLine[$n - 1],
    'signal'        => $sigLine[$n - 1],
    'histogram'     => $macdLine[$n - 1] - $sigLine[$n - 1],
    'prevHistogram' => $macdLine[$n - 2] - $sigLine[$n - 2],
  ];
}

/** Average volume of the prior $period bars (excludes the last closed bar). */
function calcAvgVolume(array $volumes, int $period = 20): float
{
  $slice = array_slice($volumes, -($period + 1), $period);
  if (empty($slice)) { return 0.0; }
  return array_sum($slice) / count($slice);
}

/**
 * Candle body ratio = |close-open| / (high-low).
 * Returns 0 if the candle has no range (doji / flat).
 */
function candleBodyRatio(float $open, float $high, float $low, float $close): float
{
  $range = $high - $low;
  if ($range < 0.000001) { return 0.0; }
  return abs($close - $open) / $range;
}

/**
 * Stochastic RSI — returns %K and %D (both 0-100).
 * Steps: 1) compute RSI array, 2) Stoch of RSI, 3) smooth K and D.
 * Returns ['k'=>50.0,'d'=>50.0] on insufficient data.
 *
 * @param  float[] $closes oldest→newest
 * @return array{k:float,d:float}
 */
function calcStochRSI(array $closes, int $rsiPeriod, int $stochPeriod, int $smoothK, int $smoothD): array
{
  $empty = ['k' => 50.0, 'd' => 50.0];
  $need  = $rsiPeriod * 2 + $stochPeriod + $smoothK + $smoothD + 5;
  if (count($closes) < $need) { return $empty; }

  // Build full RSI array
  $rsiArr = [];
  $slice  = array_slice($closes, -($rsiPeriod * 2 + $stochPeriod + $smoothK + $smoothD + 5));
  $total  = count($slice);
  $gains  = $losses = 0.0;
  for ($i = 1; $i <= $rsiPeriod; $i++) {
    $d = $slice[$i] - $slice[$i - 1];
    if ($d >= 0) { $gains += $d; } else { $losses -= $d; }
  }
  $ag = $gains / $rsiPeriod;
  $al = $losses / $rsiPeriod;
  $rsiArr[] = ($al < 0.00001) ? 100.0 : 100.0 - (100.0 / (1.0 + $ag / $al));
  for ($i = $rsiPeriod + 1; $i < $total; $i++) {
    $d  = $slice[$i] - $slice[$i - 1];
    $ag = ($ag * ($rsiPeriod - 1) + max($d,  0)) / $rsiPeriod;
    $al = ($al * ($rsiPeriod - 1) + max(-$d, 0)) / $rsiPeriod;
    $rsiArr[] = ($al < 0.00001) ? 100.0 : 100.0 - (100.0 / (1.0 + $ag / $al));
  }

  // Stoch of RSI: rolling min/max over $stochPeriod
  $rawK = [];
  $rsiCount = count($rsiArr);
  for ($i = $stochPeriod - 1; $i < $rsiCount; $i++) {
    $window  = array_slice($rsiArr, $i - $stochPeriod + 1, $stochPeriod);
    $loRsi   = min($window);
    $hiRsi   = max($window);
    $rawK[]  = ($hiRsi - $loRsi) < 0.00001 ? 50.0 : (($rsiArr[$i] - $loRsi) / ($hiRsi - $loRsi)) * 100.0;
  }

  // Smooth %K with SMA($smoothK)
  $smoothedK = [];
  for ($i = $smoothK - 1; $i < count($rawK); $i++) {
    $smoothedK[] = array_sum(array_slice($rawK, $i - $smoothK + 1, $smoothK)) / $smoothK;
  }
  // Smooth %D with SMA($smoothD) of smoothed %K
  $smoothedD = [];
  for ($i = $smoothD - 1; $i < count($smoothedK); $i++) {
    $smoothedD[] = array_sum(array_slice($smoothedK, $i - $smoothD + 1, $smoothD)) / $smoothD;
  }

  $kN = count($smoothedK);
  $dN = count($smoothedD);
  if ($kN < 1 || $dN < 1) { return $empty; }
  return ['k' => $smoothedK[$kN - 1], 'd' => $smoothedD[$dN - 1]];
}

/**
 * ADX (Average Directional Index) via Wilder smoothing.
 * Returns ADX value (0–100). ADX < 20 = ranging/choppy; > 25 = trending.
 *
 * Also returns +DI and -DI so the caller can derive directional bias.
 *
 * @param  float[] $highs  oldest→newest
 * @param  float[] $lows   oldest→newest
 * @param  float[] $closes oldest→newest
 * @return array{adx:float,plusDI:float,minusDI:float}
 */
function calcADX(array $highs, array $lows, array $closes, int $period): array
{
  $empty = ['adx' => 0.0, 'plusDI' => 0.0, 'minusDI' => 0.0];
  $n = count($closes);
  if ($n < $period * 2 + 2) { return $empty; }

  $plusDM  = [];
  $minusDM = [];
  $trList  = [];
  for ($i = 1; $i < $n; $i++) {
    $upMove   = $highs[$i]  - $highs[$i - 1];
    $downMove = $lows[$i - 1] - $lows[$i];
    $plusDM[]  = ($upMove > $downMove && $upMove > 0) ? $upMove : 0.0;
    $minusDM[] = ($downMove > $upMove && $downMove > 0) ? $downMove : 0.0;
    $trList[]  = max(
      $highs[$i] - $lows[$i],
      abs($highs[$i] - $closes[$i - 1]),
      abs($lows[$i]  - $closes[$i - 1])
    );
  }

  // Wilder smooth initial sums
  $smTR   = array_sum(array_slice($trList,   0, $period));
  $smPlus = array_sum(array_slice($plusDM,   0, $period));
  $smMinus= array_sum(array_slice($minusDM,  0, $period));

  $dxArr = [];
  for ($i = $period; $i < count($trList); $i++) {
    $smTR    = $smTR   - ($smTR   / $period) + $trList[$i];
    $smPlus  = $smPlus - ($smPlus / $period) + $plusDM[$i];
    $smMinus = $smMinus- ($smMinus/ $period) + $minusDM[$i];

    $pDI = $smTR > 0.0 ? ($smPlus  / $smTR) * 100.0 : 0.0;
    $mDI = $smTR > 0.0 ? ($smMinus / $smTR) * 100.0 : 0.0;
    $sum = $pDI + $mDI;
    $dxArr[] = $sum > 0.0 ? abs($pDI - $mDI) / $sum * 100.0 : 0.0;
  }

  if (count($dxArr) < $period) { return $empty; }
  $adx = array_sum(array_slice($dxArr, 0, $period)) / $period;
  for ($i = $period; $i < count($dxArr); $i++) {
    $adx = ($adx * ($period - 1) + $dxArr[$i]) / $period;
  }

  // Final DI values for the last bar
  $lastPDI = 0.0;
  $lastMDI = 0.0;
  if ($smTR > 0.0) {
    $lastPDI = ($smPlus  / $smTR) * 100.0;
    $lastMDI = ($smMinus / $smTR) * 100.0;
  }
  return ['adx' => $adx, 'plusDI' => $lastPDI, 'minusDI' => $lastMDI];
}

/**
 * Detect BB squeeze: band width as % of mid price.
 * Returns true when width % < BB_SQUEEZE_PCT (market is coiling, skip entry).
 */
function isBBSqueeze(array $bb, float $price): bool
{
  if ($price < 0.000001 || $bb['middle'] < 0.000001) { return false; }
  $widthPct = ($bb['width'] / $bb['middle']) * 100.0;
  return $widthPct < BB_SQUEEZE_PCT;
}

/**
 * Find the nearest swing high and swing low over the last $lookback bars.
 * Uses a simple pivot: a bar is a swing high if its high is the highest
 * among the 2 bars on each side.
 *
 * @param  float[] $highs oldest→newest
 * @param  float[] $lows  oldest→newest
 * @param  int     $lookback number of recent bars to scan
 * @return array{resistances:float[],supports:float[]}
 */
function findSRLevels(array $highs, array $lows, int $lookback = 50): array
{
  $n    = count($highs);
  $scan = min($lookback, $n - 4);
  $res  = [];
  $sup  = [];
  for ($i = $n - $scan; $i < $n - 2; $i++) {
    // Swing high pivot
    if ($highs[$i] > $highs[$i - 1] && $highs[$i] > $highs[$i - 2]
     && $highs[$i] > $highs[$i + 1] && $highs[$i] > $highs[$i + 2]) {
      $res[] = $highs[$i];
    }
    // Swing low pivot
    if ($lows[$i] < $lows[$i - 1] && $lows[$i] < $lows[$i - 2]
     && $lows[$i] < $lows[$i + 1] && $lows[$i] < $lows[$i + 2]) {
      $sup[] = $lows[$i];
    }
  }
  return ['resistances' => $res, 'supports' => $sup];
}

/**
 * Check if price is within SR_BUFFER_PCT of any S/R level.
 * Returns true = too close to a wall → skip entry.
 */
function isNearSRWall(float $price, array $sr, string $side): bool
{
  $bufferFrac = SR_BUFFER_PCT / 100.0;
  // For longs: check if a resistance level is very close above (within buffer)
  if ($side === 'long') {
    foreach ($sr['resistances'] as $r) {
      if ($r > $price && ($r - $price) / $price <= $bufferFrac) {
        return true; // resistance too close above
      }
    }
  }
  // For shorts: check if a support level is very close below (within buffer)
  if ($side === 'short') {
    foreach ($sr['supports'] as $s) {
      if ($s < $price && ($price - $s) / $price <= $bufferFrac) {
        return true; // support too close below
      }
    }
  }
  return false;
}

/**
 * Find how many bars ago the last EMA9/EMA21 cross occurred.
 * Returns 0 if no cross found within $maxLookback bars.
 * Used to reject stale crosses (cross happened too long ago).
 *
 * @param float[] $fastArr  Full EMA-fast array (oldest→newest)
 * @param float[] $slowArr  Full EMA-slow array (oldest→newest)
 * @param int     $maxLookback  How far back to search
 * @return array{bullAge:int,bearAge:int}  bars since last bull/bear cross (0=none found)
 */
function findCrossAge(array $fastArr, array $slowArr, int $maxLookback = 5): array
{
  $n       = count($fastArr);
  $bullAge = 0;
  $bearAge = 0;
  for ($i = 1; $i <= $maxLookback && ($n - 1 - $i) >= 1; $i++) {
    $cur  = $n - 1 - $i;       // bar at age $i
    $prev = $cur - 1;
    if ($bullAge === 0 && $fastArr[$prev] <= $slowArr[$prev] && $fastArr[$cur] > $slowArr[$cur]) {
      $bullAge = $i;
    }
    if ($bearAge === 0 && $fastArr[$prev] >= $slowArr[$prev] && $fastArr[$cur] < $slowArr[$cur]) {
      $bearAge = $i;
    }
  }
  return ['bullAge' => $bullAge, 'bearAge' => $bearAge];
}

/**
 * Weighted confluence scorer.
 *
 * Max score = 9 pts:
 *   EMA cross (body-confirmed, fresh ≤ CROSS_MAX_AGE_BARS) → +2
 *   EMA50 trend filter ..............................        → +2
 *   MACD histogram rising + on correct zero side ...        → +1
 *   RSI mid-zone .....................................       → +1
 *   Volume spike ≥1.5× avg20 ........................       → +1
 *   Bollinger Band proximity .........................       → +1
 *   Higher-TF 15m EMA21 slope (bonus) ...............       → +1
 *
 * Conflict guard: if bull-score and bear-score are within 1 pt of each other,
 * both are zeroed — ambiguous market, no trade is safer than a coin-flip.
 *
 * @param array<string,mixed> $ind
 * @return array{long:int,short:int,longReason:string,shortReason:string}
 */
function scoreSignals(array $ind): array
{
  $ls = $ss = 0;
  $lp = $sp = [];

  // EMA cross: fresh + body-confirmed
  $bullFresh = $ind['bullAge'] > 0 && $ind['bullAge'] <= CROSS_MAX_AGE_BARS;
  $bearFresh = $ind['bearAge'] > 0 && $ind['bearAge'] <= CROSS_MAX_AGE_BARS;
  if ($bullFresh && $ind['bodyRatio'] >= BODY_RATIO_MIN) { $ls += 2; $lp[] = 'EMA✅(+2,age:' . $ind['bullAge'] . ')'; }
  if ($bearFresh && $ind['bodyRatio'] >= BODY_RATIO_MIN) { $ss += 2; $sp[] = 'EMA✅(+2,age:' . $ind['bearAge'] . ')'; }

  // EMA50: sloping trend confirmed (+2), or price on correct side but flat slope (+1)
  if ($ind['trendBull']) { $ls += 2; $lp[] = 'EMA50✅(+2,sloping)'; }
  if ($ind['trendBear']) { $ss += 2; $sp[] = 'EMA50✅(+2,sloping)'; }

  // Consecutive candle confirmation: all CONFIRM_CANDLES bars close in signal direction
  if ($ind['confirmBull']) { $ls += 1; $lp[] = 'Confirm✅(+1)'; }
  if ($ind['confirmBear']) { $ss += 1; $sp[] = 'Confirm✅(+1)'; }

  if ($ind['macdBull'] && $ind['macd']['macd'] > 0) { $ls += 1; $lp[] = 'MACD✅(+1)'; }
  if ($ind['macdBear'] && $ind['macd']['macd'] < 0) { $ss += 1; $sp[] = 'MACD✅(+1)'; }

  // RSI mid-zone + StochRSI confirmation (+1 each, max +2 combined)
  if ($ind['rsiBull']) { $ls += 1; $lp[] = 'RSI✅(+1)'; }
  if ($ind['rsiBear']) { $ss += 1; $sp[] = 'RSI✅(+1)'; }
  // StochRSI: %K oversold and crossing above %D → long bonus
  if ($ind['stochLong']) { $ls += 1; $lp[] = 'StochRSI✅(+1)'; }
  if ($ind['stochShort']){ $ss += 1; $sp[] = 'StochRSI✅(+1)'; }

  // Directional volume: only reward the side matching the candle colour
  if ($ind['volBull']) { $ls += 1; $lp[] = 'Vol🟢(+1)'; }
  if ($ind['volBear']) { $ss += 1; $sp[] = 'Vol🔴(+1)'; }

  if ($ind['bbLong'])  { $ls += 1; $lp[] = 'BB✅(+1)'; }
  if ($ind['bbShort']) { $ss += 1; $sp[] = 'BB✅(+1)'; }

  if ($ind['htfBull']) { $ls += 1; $lp[] = 'HTF✅(+1)'; }
  if ($ind['htfBear']) { $ss += 1; $sp[] = 'HTF✅(+1)'; }

  // ADX directional: +DI > -DI confirms bullish pressure (already computed, zero cost)
  if ($ind['adxBull']) { $ls += 1; $lp[] = 'ADX+DI✅(+1)'; }
  if ($ind['adxBear']) { $ss += 1; $sp[] = 'ADX-DI✅(+1)'; }

  // ── Wick rejection veto: strong wick against signal → kill it ─
  if ($ind['wickRejectLong']  && $ls > 0) { $lp[] = '🕯️WICK-REJECT'; $ls = 0; }
  if ($ind['wickRejectShort'] && $ss > 0) { $sp[] = '🕯️WICK-REJECT'; $ss = 0; }

  // ── Conflict guard: only block when scores are equal ─────
  if ($ls > 0 && $ss > 0 && $ls === $ss) {
    $lp[] = '⚠️CONFLICT';
    $sp[] = '⚠️CONFLICT';
    $ls   = 0;
    $ss   = 0;
  }

  return [
    'long'        => $ls,
    'short'       => $ss,
    'longReason'  => implode(' ', $lp) ?: 'none',
    'shortReason' => implode(' ', $sp) ?: 'none',
  ];
}

// ============================================================
//  POSITION MANAGEMENT
// ============================================================

function getOpenPositionSide(\ccxt\binance $ex): ?string
{
  if (DRY_RUN) { return null; }
  try {
    foreach ($ex->fetch_positions([SYMBOL]) as $pos) {
      if (abs((float)$pos['contracts']) > 0.00001) { return $pos['side']; }
    }
  } catch (\Exception $e) {
    logToFile('[!] fetch_positions: ' . $e->getMessage());
  }
  return null;
}

function getEntryPrice(\ccxt\binance $ex): ?float
{
  if (DRY_RUN) { return null; }
  try {
    foreach ($ex->fetch_positions([SYMBOL]) as $pos) {
      if (abs((float)$pos['contracts']) > 0.00001) { return (float)$pos['entryPrice']; }
    }
  } catch (\Exception $e) {
    logToFile('[!] getEntryPrice: ' . $e->getMessage());
  }
  return null;
}

function closePosition(\ccxt\binance $ex, string $side, string $reason, float $price = 0.0): void
{
  if (DRY_RUN) {
    $qty = ($price > 0.0) ? round((POSITION_USDT * LEVERAGE) / $price, 2) : 0;
    logToFile(DRY_PREFIX . "🔄 CLOSE {$side} | qty:~{$qty} | price:~{$price} | {$reason}");
    return;
  }
  logToFile("🔄 Closing {$side} | {$reason}");
  try {
    foreach ($ex->fetch_positions([SYMBOL]) as $pos) {
      if ((string)$pos['side'] !== $side) { continue; }
      $amt = (float)$pos['contracts'];
      if (abs($amt) < 0.00001) { continue; }
      $ex->create_order(SYMBOL, 'market', ($side === 'long') ? 'sell' : 'buy', abs($amt), null, ['reduceOnly' => true]);
      logToFile("✅ Closed {$side} | contracts:{$amt}");
    }
  } catch (\Exception $e) {
    logToFile('[!] closePosition: ' . $e->getMessage());
  }
}

function openPosition(\ccxt\binance $ex, string $side, float $price, int $precision = 2): bool
{
  $notional  = POSITION_USDT * LEVERAGE;
  if ($notional < MIN_NOTIONAL) {
    logToFile("[!] Notional {$notional} below min " . MIN_NOTIONAL . ' — skipped.');
    return false;
  }
  $qty = round($notional / $price, $precision);
  if ($qty <= 0.0) {
    logToFile("[!] Invalid qty {$qty} at price {$price} — skipped.");
    return false;
  }
  if (DRY_RUN) {
    logToFile(DRY_PREFIX . "🚀 OPEN {$side} | qty:{$qty} | price:{$price} | notional:{$notional} USDT");
    return true;
  }
  try {
    $order  = $ex->create_order(SYMBOL, 'market', ($side === 'long') ? 'buy' : 'sell', $qty);
    $filled = number_format((float)($order['average'] ?? $price), 4);
    logToFile("🚀 Opened {$side} | qty:{$qty} | fill:{$filled} | notional:{$notional} USDT");
    return true;
  } catch (\Exception $e) {
    logToFile('[!] openPosition: ' . $e->getMessage());
    return false;
  }
}

function recordPaperTrade(float $pnl, int &$trades, float &$totalPnl, int &$wins, float &$peakPnl, float &$maxDrawdown): void
{
  $trades++;
  $totalPnl += $pnl;
  if ($pnl > 0) { $wins++; }
  // Update peak and max drawdown
  if ($totalPnl > $peakPnl) { $peakPnl = $totalPnl; }
  $dd = $peakPnl - $totalPnl;
  if ($dd > $maxDrawdown) { $maxDrawdown = $dd; }
  $winRate = $trades > 0 ? round($wins / $trades * 100, 1) : 0;
  logToFile(sprintf(
    DRY_PREFIX . '📊 Trade #%d | PnL:%s%s USDT | WinRate:%s%% | TotalPnL:%s USDT | MaxDD:%.2f USDT',
    $trades,
    $pnl >= 0 ? '+' : '',
    number_format($pnl, 2),
    $winRate,
    number_format($totalPnl, 2),
    $maxDrawdown
  ));
}

// ============================================================
//  GRACEFUL SHUTDOWN
// ============================================================
$running = true;
if (function_exists('pcntl_signal')) {
  pcntl_signal(SIGINT,  function () use (&$running) { $running = false; });
  pcntl_signal(SIGTERM, function () use (&$running) { $running = false; });
}

// ============================================================
//  BOOT
// ============================================================
$warmupCandles = EMA_TREND + MACD_SLOW + MACD_SIGNAL_P + ATR_PERIOD + BB_PERIOD + 10;
$mode = DRY_RUN ? '🧪 DRY-RUN (paper)' : '🔴 LIVE TRADING';
logToFile('╔══════════════════════════════════════════════════════╗');
logToFile('║   WLD/USDT Futures — High Win-Rate Scalper v2        ║');
logToFile('║   Mode    : ' . str_pad($mode, 43) . '║');
logToFile('║   Signal  : Score ≥' . SCORE_STRONG . '=strong  ≥' . SCORE_NORMAL . '=normal (9pts max)    ║');
logToFile('║   Risk    : ATR×' . ATR_SL_MULT . ' SL / ATR×' . ATR_TP_MULT . ' TP  (2:1 R:R)         ║');
logToFile('║   Trail   : BE@+' . TRAIL_BE_PCT . '%  Lock+' . TRAIL_LOCKED_GAIN . '%@+' . TRAIL_LOCK_PCT . '%              ║');
logToFile('║   Filters : EMA9/21 + EMA50 + MACD + RSI + BB + HTF  ║');
logToFile('╚══════════════════════════════════════════════════════╝');

// ============================================================
//  STATE
// ============================================================
$currentSide     = DRY_RUN ? null : getOpenPositionSide($exchange);
$lastSignalTime  = 0;
$paperEntryPrice = 0.0;
$paperTrades     = 0;
$paperWins       = 0;
$paperPnlUsdt    = 0.0;

// ATR-based dynamic SL/TP price levels (updated per position open)
$activeSL       = 0.0;
$activeTP       = 0.0;
// Trailing state flags
$trailBreakeven = false;
$trailLocked    = false;
// Position open timestamp (for stale timeout)
$positionOpenAt = 0;

// ── Circuit-breaker state ────────────────────────────────────
$consecLosses      = 0;
$circuitBrokenAt   = 0;  // timestamp when circuit tripped (0 = not tripped)

// ── Daily risk-cap state ─────────────────────────────────────
$dailyDate         = date('Y-m-d');  // track calendar day
$dailyLossUsdt     = 0.0;
$dailyTradeCount   = 0;

// ── Peak equity tracking (for max drawdown) ───────────────────
$peakPnl           = 0.0;
$maxDrawdown       = 0.0;

// ── Rolling win-rate (last 10 trades) ────────────────────────
// Used to adaptively tighten score threshold when performance degrades.
$rollingResults    = [];   // circular buffer of 1=win / 0=loss, max 10 entries
$rollingThrottled  = false; // tracks whether we're currently in throttled mode
const ROLLING_WINDOW      = 10;
const ROLLING_WINRATE_MIN = 0.45;  // if win-rate drops below 45% → raise score threshold by 1

// ── S/R level cache (recompute only on new candle, not every 10s tick) ───
$srLevels          = ['resistances' => [], 'supports' => []];
$srLastBarCount    = 0;

// ── 1h HTF hard gate ─────────────────────────────────────────
// true=1h trend is bullish, false=bearish, null=unknown (allow both)
$htf2Bull          = null;
$htf2Bear          = null;



logToFile('[*] Warmup candles : ' . $warmupCandles);
logToFile('[*] Open position  : ' . ($currentSide ?? 'none'));

// ============================================================
//  MAIN LOOP
// ============================================================
while ($running) {
  if (function_exists('pcntl_signal_dispatch')) { pcntl_signal_dispatch(); }

  try {
    // ── 1. Fetch 5m klines ───────────────────────────────────
    $ohlcv   = $exchange->fetch_ohlcv(SYMBOL, INTERVAL, null, KLINE_LIMIT);
    $opens   = array_column($ohlcv, 1);
    $highs   = array_column($ohlcv, 2);
    $lows    = array_column($ohlcv, 3);
    $closes  = array_column($ohlcv, 4);
    $volumes = array_column($ohlcv, 5);
    // Drop the still-forming (unclosed) candle
    array_pop($opens); array_pop($highs); array_pop($lows);
    array_pop($closes); array_pop($volumes);

    $n            = count($closes);
    $currentPrice = (float)$closes[$n - 1];

    // ── 2. Fetch 15m klines — higher-timeframe bias ──────────
    $htfBull = false;
    $htfBear = false;
    try {
      $htfOhlcv  = $exchange->fetch_ohlcv(SYMBOL, INTERVAL_HTF, null, KLINE_LIMIT_HTF);
      $htfCloses = array_column($htfOhlcv, 4);
      array_pop($htfCloses);
      $htfEma = calcEMA($htfCloses, EMA_SLOW);
      $htfN   = count($htfEma);
      if ($htfN >= 4) {
        $htfBull = $htfEma[$htfN - 1] > $htfEma[$htfN - 4]; // 15m EMA21 sloping up
        $htfBear = $htfEma[$htfN - 1] < $htfEma[$htfN - 4]; // 15m EMA21 sloping down
      }
    } catch (\Exception $e) {
      logToFile('[!] HTF fetch: ' . $e->getMessage());
    }

    // ── 2b. Fetch 1h klines — hard directional gate ──────────
    // Only re-fetch when a new 5m candle closes (1h candle = 12 × 5m bars)
    if ($n !== $srLastBarCount) {  // reuse new-candle tick from S/R cache logic
      try {
        $htf2Ohlcv  = $exchange->fetch_ohlcv(SYMBOL, INTERVAL_HTF2, null, KLINE_LIMIT_HTF2);
        $htf2Closes = array_column($htf2Ohlcv, 4);
        array_pop($htf2Closes); // drop forming candle
        $htf2Ema  = calcEMA($htf2Closes, EMA_SLOW); // EMA21 on 1h
        $htf2N    = count($htf2Ema);
        if ($htf2N >= 4) {
          $htf2Bull = $htf2Ema[$htf2N - 1] > $htf2Ema[$htf2N - 4]; // 1h EMA21 sloping up
          $htf2Bear = $htf2Ema[$htf2N - 1] < $htf2Ema[$htf2N - 4]; // 1h EMA21 sloping down
        }
      } catch (\Exception $e) {
        logToFile('[!] HTF2 1h fetch: ' . $e->getMessage());
      }
    }

    // ── 3. Indicators ────────────────────────────────────────
    $emaFastArr  = calcEMA($closes, EMA_FAST);
    $emaSlowArr  = calcEMA($closes, EMA_SLOW);
    $emaTrendArr = calcEMA($closes, EMA_TREND);

    $emaFastNow  = (float)$emaFastArr[$n - 1];
    $emaFastPrev = (float)$emaFastArr[$n - 2];
    $emaSlowNow  = (float)$emaSlowArr[$n - 1];
    $emaSlowPrev = (float)$emaSlowArr[$n - 2];
    $emaTrend    = (float)$emaTrendArr[$n - 1];

    // EMA50 slope: must move ≥ EMA_TREND_SLOPE_MIN% over EMA_TREND_SLOPE_BARS bars
    $emaTrendSloping = false;
    $emaTrendSlopeBull = false;
    $emaTrendSlopeBear = false;
    if ($n > EMA_TREND_SLOPE_BARS + 1) {
      $emaTrendPast  = (float)$emaTrendArr[$n - 1 - EMA_TREND_SLOPE_BARS];
      $emaTrendSlope = ($emaTrend - $emaTrendPast) / $emaTrendPast * 100.0;
      $emaTrendSlopeBull = $emaTrendSlope >= EMA_TREND_SLOPE_MIN;
      $emaTrendSlopeBear = $emaTrendSlope <= -EMA_TREND_SLOPE_MIN;
      $emaTrendSloping   = $emaTrendSlopeBull || $emaTrendSlopeBear;
    }

    $rsi       = calcRSI($closes, RSI_PERIOD);
    $stochRsi  = calcStochRSI($closes, STOCH_RSI_PERIOD, STOCH_RSI_PERIOD, STOCH_RSI_SMOOTH_K, STOCH_RSI_SMOOTH_D);
    $macd      = calcMACD($closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL_P);
    $atr       = calcATR($highs, $lows, $closes, ATR_PERIOD);
    $adx       = calcADX($highs, $lows, $closes, ADX_PERIOD);
    $bb        = calcBB($closes, BB_PERIOD, BB_STDDEV);
    // Recompute S/R only when a new candle has closed (not every 10s tick)
    if ($n !== $srLastBarCount) {
      $srLevels       = findSRLevels($highs, $lows, SR_LOOKBACK);
      $srLastBarCount = $n;
    }
    $volNow    = (float)$volumes[$n - 1];
    $volAvg    = calcAvgVolume($volumes);
    $bodyRatio  = candleBodyRatio((float)$opens[$n - 1], (float)$highs[$n - 1], (float)$lows[$n - 1], $currentPrice);
    // Wick rejection: fraction of range that is upper/lower wick
    $candleRange      = (float)$highs[$n - 1] - (float)$lows[$n - 1];
    $upperWickRatio   = $candleRange > 0.0 ? ((float)$highs[$n - 1] - max((float)$opens[$n - 1], $currentPrice)) / $candleRange : 0.0;
    $lowerWickRatio   = $candleRange > 0.0 ? (min((float)$opens[$n - 1], $currentPrice) - (float)$lows[$n - 1]) / $candleRange : 0.0;
    $wickRejectLong   = $upperWickRatio > WICK_REJECT_MAX;  // bearish wick rejection → skip long
    $wickRejectShort  = $lowerWickRatio > WICK_REJECT_MAX;  // bullish wick rejection → skip short

    // Consecutive candle confirmation: last CONFIRM_CANDLES closed bars must all
    // close in the signal direction (bullish = close > open; bearish = close < open)
    $consecutiveBull = true;
    $consecutiveBear = true;
    for ($ci = 1; $ci <= CONFIRM_CANDLES; $ci++) {
      $cIdx = $n - $ci;
      if ($cIdx < 1) { $consecutiveBull = false; $consecutiveBear = false; break; }
      if ((float)$closes[$cIdx] <= (float)$opens[$cIdx]) { $consecutiveBull = false; }
      if ((float)$closes[$cIdx] >= (float)$opens[$cIdx]) { $consecutiveBear = false; }
    }

    // ── 4. ATR-based SL distance for NEW positions ───────────
    // Scale SL tightness with ADX: stronger trend = tighter SL = better R:R.
    $adxVal     = $adx['adx'];
    $slMult     = $adxVal >= ADX_STRONG_TREND ? ATR_SL_MULT_STRONG
                : ($adxVal >= ADX_MIN_TREND   ? ATR_SL_MULT_WEAK
                :                               ATR_SL_MULT);
    $slDist     = $atr > 0.0 ? $atr * $slMult : $currentPrice * (ATR_FALLBACK_SL_PCT / 100.0);

    // ── 5. Trailing stop (ATR-based continuous) + SL/TP check ─
    if ($currentSide !== null) {
      $entry = DRY_RUN ? $paperEntryPrice : getEntryPrice($exchange);

      if ($entry !== null && $entry > 0.0) {
        $pnlPct  = ($currentSide === 'long')
          ? (($currentPrice - $entry) / $entry) * 100.0
          : (($entry - $currentPrice) / $entry) * 100.0;
        $pnlUsdt = POSITION_USDT * LEVERAGE * ($pnlPct / 100.0);

        // ── Step 1: move SL to breakeven ────────────────────
        if (!$trailBreakeven && $pnlPct >= TRAIL_BE_PCT) {
          $activeSL       = $entry;
          $trailBreakeven = true;
          logToFile((DRY_RUN ? DRY_PREFIX : '') . "🔒 BE: SL→entry @ " . number_format($entry, 4));
        }
        // ── Step 2: lock partial gain ────────────────────────
        if (!$trailLocked && $pnlPct >= TRAIL_LOCK_PCT) {
          $lockGainDist = $entry * (TRAIL_LOCKED_GAIN / 100.0);
          $activeSL     = ($currentSide === 'long') ? $entry + $lockGainDist : $entry - $lockGainDist;
          $trailLocked  = true;
          logToFile((DRY_RUN ? DRY_PREFIX : '') . '🔒 Lock: SL→+' . TRAIL_LOCKED_GAIN . '% @ ' . number_format($activeSL, 4));
        }
        // ── Step 3: ATR continuous trail (always active, tightest wins) ─
        // Runs from the very first tick — before BE too.
        // The "never widen" rule ensures it can only improve the initial SL.
        if ($atr > 0.0) {
          $atrTrailDist = $atr * ATR_TRAIL_MULT;
          if ($currentSide === 'long') {
            $newSL = $currentPrice - $atrTrailDist;
            if ($newSL > $activeSL) {      // only move SL up, never down
              $activeSL = $newSL;
            }
          } else {
            $newSL = $currentPrice + $atrTrailDist;
            if ($newSL < $activeSL) {      // only move SL down, never up
              $activeSL = $newSL;
            }
          }
        }

        // ── Stale position timeout ───────────────────────────
        // If BE not reached and held longer than MAX_TRADE_SECS → force close.
        // Prevents capital being locked in a dead trade while fees tick up.
        $staleTimeout = !$trailBreakeven
                     && $positionOpenAt > 0
                     && (time() - $positionOpenAt) >= MAX_TRADE_SECS;
        if ($staleTimeout) {
          closePosition($exchange, $currentSide, 'TIMEOUT ' . number_format($pnlPct, 2) . '% (' . number_format($pnlUsdt, 2) . ' USDT)', $currentPrice);
          if (DRY_RUN) { recordPaperTrade($pnlUsdt, $paperTrades, $paperPnlUsdt, $paperWins, $peakPnl, $maxDrawdown); }
          logToFile((DRY_RUN ? DRY_PREFIX : '') . '⏱️ Stale position closed after ' . MAX_TRADE_SECS . 's with no BE.');
          $dailyTradeCount++;
          if ($pnlUsdt < 0.0) {
            $dailyLossUsdt += $pnlUsdt;
            $consecLosses++;
            if ($consecLosses >= MAX_CONSEC_LOSSES && $circuitBrokenAt === 0) {
              $circuitBrokenAt = time();
              logToFile('🔴 Circuit-breaker TRIPPED — ' . MAX_CONSEC_LOSSES . ' consecutive losses. Pausing ' . (CIRCUIT_BREAK_SECS / 60) . ' min.');
            }
          } else { $consecLosses = 0; }
          $rollingResults[] = ($pnlUsdt >= 0.0) ? 1 : 0;
          if (count($rollingResults) > ROLLING_WINDOW) { array_shift($rollingResults); }
          $currentSide = null; $paperEntryPrice = 0.0; $activeSL = 0.0; $activeTP = 0.0;
          $trailBreakeven = false; $trailLocked = false; $positionOpenAt = 0; $lastSignalTime = 0;
        }

        // ── Hit trailing SL (only exit on the profit side) ───
        $hitSL = !$staleTimeout && ($activeSL > 0.0) && (
          ($currentSide === 'long'  && $currentPrice <= $activeSL) ||
          ($currentSide === 'short' && $currentPrice >= $activeSL)
        );

        if ($hitSL) {
          $tag = $pnlUsdt >= 0.0 ? 'TRAIL-EXIT' : 'STOP-LOSS';
          closePosition(
            $exchange, $currentSide,
            "{$tag} " . number_format($pnlPct, 2) . "% (" . number_format($pnlUsdt, 2) . " USDT) SL={$activeSL}",
            $currentPrice
          );
          if (DRY_RUN) { recordPaperTrade($pnlUsdt, $paperTrades, $paperPnlUsdt, $paperWins, $peakPnl, $maxDrawdown); }

          // ── Update daily + circuit-breaker counters ────────
          $dailyTradeCount++;
          if ($pnlUsdt < 0.0) {
            $dailyLossUsdt += $pnlUsdt;
            $consecLosses++;
            if ($consecLosses >= MAX_CONSEC_LOSSES && $circuitBrokenAt === 0) {
              $circuitBrokenAt = time();
              logToFile('🔴 Circuit-breaker TRIPPED — ' . MAX_CONSEC_LOSSES . ' consecutive losses. Pausing ' . (CIRCUIT_BREAK_SECS / 60) . ' min.');
            }
          } else {
            $consecLosses = 0;
          }
          if ($dailyLossUsdt <= -MAX_DAILY_LOSS_USDT) {
            logToFile('🔴 Daily loss cap hit (' . number_format($dailyLossUsdt, 2) . ' USDT) — no more trades today.');
          }

          // ── Update rolling win-rate buffer ────────────────
          $rollingResults[] = ($pnlUsdt >= 0.0) ? 1 : 0;
          if (count($rollingResults) > ROLLING_WINDOW) {
            array_shift($rollingResults);
          }

          $currentSide     = null;
          $paperEntryPrice = 0.0;
          $activeSL        = 0.0;
          $activeTP        = 0.0;
          $trailBreakeven  = false;
          $trailLocked     = false;
          $positionOpenAt  = 0;
          $lastSignalTime  = 0;
        }
      }
    }

    // ── 6. Daily cap reset at midnight ───────────────────────
    $todayDate = date('Y-m-d');
    if ($todayDate !== $dailyDate) {
      logToFile("📅 New day {$todayDate} — resetting daily counters (was: loss={$dailyLossUsdt} trades={$dailyTradeCount})");
      $dailyDate       = $todayDate;
      $dailyLossUsdt   = 0.0;
      $dailyTradeCount = 0;
      $consecLosses    = 0;
      $circuitBrokenAt = 0;
    }

    // ── 7. Circuit-breaker auto-reset check ──────────────────
    if ($circuitBrokenAt > 0 && (time() - $circuitBrokenAt) >= CIRCUIT_BREAK_SECS) {
      logToFile('🔁 Circuit-breaker expired — resuming trading.');
      $circuitBrokenAt = 0;
      $consecLosses    = 0;
    }

    // ── 8. Build indicator map for scorer ────────────────────
    // EMA50 trend: price must be on correct side AND EMA50 must be sloping
    $trendBull   = $currentPrice > $emaTrend && $emaTrendSlopeBull;
    $trendBear   = $currentPrice < $emaTrend && $emaTrendSlopeBear;
    $macdBull    = $macd['histogram'] > 0 && $macd['histogram'] > $macd['prevHistogram'];
    $macdBear    = $macd['histogram'] < 0 && $macd['histogram'] < $macd['prevHistogram'];
    $rsiBull     = $rsi >= RSI_LONG_MIN  && $rsi <= RSI_LONG_MAX;
    $rsiBear     = $rsi >= RSI_SHORT_MIN && $rsi <= RSI_SHORT_MAX;
    // Directional volume: spike on a bullish close = long confirmation, bearish = short
    $lastClose   = (float)$closes[$n - 1];
    $lastOpen    = (float)$opens[$n - 1];
    $volSpike    = $volAvg > 0.00001 && $volNow >= $volAvg * VOL_SPIKE_MULT;
    $volBull     = $volSpike && $lastClose >= $lastOpen;   // green candle with volume
    $volBear     = $volSpike && $lastClose <= $lastOpen;   // red candle with volume
    $volOk       = $volSpike; // kept for legacy status-log reference
    $bbBandWidth = $bb['width'];
    $bbLong      = $bbBandWidth > 0.0 && ($currentPrice - $bb['lower']) <= $bbBandWidth * BB_PROX_RATIO;
    $bbShort     = $bbBandWidth > 0.0 && ($bb['upper'] - $currentPrice) <= $bbBandWidth * BB_PROX_RATIO;
    // StochRSI: oversold K crossing D → long; overbought K crossing D → short
    $stochLong   = $stochRsi['k'] <= STOCH_LONG_MAX  && $stochRsi['k'] > $stochRsi['d'];
    $stochShort  = $stochRsi['k'] >= STOCH_SHORT_MIN && $stochRsi['k'] < $stochRsi['d'];

    // ADX directional bias: +DI > -DI = bulls in control; -DI > +DI = bears.
    // Already computed for free — use it as a free score bonus.
    $adxBull     = $adx['plusDI']  > $adx['minusDI'];
    $adxBear     = $adx['minusDI'] > $adx['plusDI'];

    $crossAge = findCrossAge($emaFastArr, $emaSlowArr, CROSS_MAX_AGE_BARS + 2);

    $indMap = [
      'bullAge'        => $crossAge['bullAge'],
      'bearAge'        => $crossAge['bearAge'],
      'bodyRatio'      => $bodyRatio,
      'trendBull'      => $trendBull,
      'trendBear'      => $trendBear,
      'macdBull'       => $macdBull,
      'macdBear'       => $macdBear,
      'rsiBull'        => $rsiBull,
      'rsiBear'        => $rsiBear,
      'stochLong'      => $stochLong,
      'stochShort'     => $stochShort,
      'adxBull'        => $adxBull,
      'adxBear'        => $adxBear,
      'volOk'          => $volOk,   // for status log
      'volBull'        => $volBull,
      'volBear'        => $volBear,
      'bbLong'         => $bbLong,
      'bbShort'        => $bbShort,
      'htfBull'        => $htfBull,
      'htfBear'        => $htfBear,
      'macd'           => $macd,
      'confirmBull'    => $consecutiveBull,
      'confirmBear'    => $consecutiveBear,
      'wickRejectLong' => $wickRejectLong,
      'wickRejectShort'=> $wickRejectShort,
    ];

    $scores = scoreSignals($indMap);

    // ── 9. Pre-trade guards ───────────────────────────────────
    $atrPct        = $currentPrice > 0.0 ? ($atr / $currentPrice) * 100.0 : 0.0;
    $marketTooFlat = $atrPct < MIN_ATR_PCT;
    $bbSqueeze     = isBBSqueeze($bb, $currentPrice);          // low-vol squeeze
    $circuitOpen   = $circuitBrokenAt > 0;
    $dailyLossCap  = $dailyLossUsdt   <= -MAX_DAILY_LOSS_USDT;
    $dailyTradeCap = $dailyTradeCount >= MAX_DAILY_TRADES;
    // ADX trend-strength gate: skip entry when market is ranging/choppy
    $adxChop       = $adx['adx'] < ADX_MIN_TREND;
    // S/R wall proximity (per side — evaluated at entry time below)
    $srWallLong    = isNearSRWall($currentPrice, $srLevels, 'long');
    $srWallShort   = isNearSRWall($currentPrice, $srLevels, 'short');

    // 1h hard gate: block longs in 1h downtrend, block shorts in 1h uptrend.
    // null = unknown (e.g. first fetch failed) → allow both sides.
    $htf2BlockLong  = $htf2Bear === true;   // 1h bearish → no longs
    $htf2BlockShort = $htf2Bull === true;   // 1h bullish → no shorts

    $tradingHalted = $circuitOpen || $dailyLossCap || $dailyTradeCap || $marketTooFlat || $bbSqueeze || $adxChop;

    // ── Adaptive score threshold (rolling win-rate) ───────────
    // If recent win-rate is poor, demand a stronger signal before entering.
    $rollingWinRate   = count($rollingResults) >= 5
                      ? array_sum($rollingResults) / count($rollingResults)
                      : 1.0;  // assume good until we have 5 trades of data
    $adaptiveStrong   = $rollingWinRate < ROLLING_WINRATE_MIN
                      ? SCORE_STRONG + 1   // tighten: need one extra confirmation
                      : SCORE_STRONG;
    $adaptiveNormal   = $rollingWinRate < ROLLING_WINRATE_MIN
                      ? SCORE_NORMAL + 1
                      : SCORE_NORMAL;
    $nowThrottled = $rollingWinRate < ROLLING_WINRATE_MIN && count($rollingResults) >= 5;
    if ($nowThrottled && !$rollingThrottled) {
      logToFile(sprintf('%s⚠️ Win-rate dropped to %.0f%% — score threshold raised to %d/%d',
        DRY_RUN ? DRY_PREFIX : '', $rollingWinRate * 100, $adaptiveStrong, $adaptiveNormal));
    } elseif (!$nowThrottled && $rollingThrottled) {
      logToFile(sprintf('%s✅ Win-rate recovered to %.0f%% — score threshold back to %d/%d',
        DRY_RUN ? DRY_PREFIX : '', $rollingWinRate * 100, SCORE_STRONG, SCORE_NORMAL));
    }
    $rollingThrottled = $nowThrottled;

    // ── 10. Status log ────────────────────────────────────────
    $px     = DRY_RUN ? DRY_PREFIX : '';
    $vr     = $volAvg > 0.00001 ? number_format($volNow / $volAvg, 2) . '×' : 'n/a';
    $pnlStr = '';
    $entryShow = DRY_RUN ? $paperEntryPrice : (getEntryPrice($exchange) ?? 0.0);
    if ($currentSide !== null && $entryShow > 0.0) {
      $livePct = ($currentSide === 'long')
        ? (($currentPrice - $entryShow) / $entryShow) * 100.0
        : (($entryShow - $currentPrice) / $entryShow) * 100.0;
      $pnlStr = ' PnL:' . number_format($livePct, 2) . '%';
    }
    $haltReason = $adxChop ? 'CHOP' : ($marketTooFlat ? 'FLAT' : ($bbSqueeze ? 'SQUEEZE' :
                  ($circuitOpen ? 'CB' : ($dailyLossCap ? 'DLOSS' : 'DTRADE'))));
    $haltStr    = $tradingHalted ? " 🚫HALT({$haltReason})" : '';

    logToFile(sprintf(
      '%sP:%-8s ADX:%-5s ATR%%:%-5s RSI:%-5s StRSI:K%-4s BB:%-6s Vol:%-5s Scores:L%d/S%d Str:%d DayL:%.2f Pos:%s%s%s',
      $px,
      number_format($currentPrice, 4),
      number_format($adx['adx'], 1),
      number_format($atrPct, 3),
      number_format($rsi, 1),
      number_format($stochRsi['k'], 1),
      $bbBandWidth > 0 ? ($bbSqueeze ? 'SQZ' : ($bbLong ? 'LOW' : ($bbShort ? 'HI' : 'MID'))) : 'n/a',
      $vr,
      $scores['long'],
      $scores['short'],
      -$consecLosses,
      $dailyLossUsdt,
      $currentSide ?? 'none',
      $pnlStr,
      $haltStr
    ));

    // ── 11. Execute trades ────────────────────────────────────
    $ready         = $n > $warmupCandles;
    $elapsed       = time() - $lastSignalTime;

    $canStrongLong = $ready && !$tradingHalted && !$srWallLong  && !$htf2BlockLong  && $elapsed >= COOLDOWN_STRONG && $scores['long']  >= $adaptiveStrong;
    $canNormLong   = $ready && !$tradingHalted && !$srWallLong  && !$htf2BlockLong  && $elapsed >= COOLDOWN_NORMAL && $scores['long']  >= $adaptiveNormal && $scores['long']  < $adaptiveStrong;
    $canStrongShort= $ready && !$tradingHalted && !$srWallShort && !$htf2BlockShort && $elapsed >= COOLDOWN_STRONG && $scores['short'] >= $adaptiveStrong;
    $canNormShort  = $ready && !$tradingHalted && !$srWallShort && !$htf2BlockShort && $elapsed >= COOLDOWN_NORMAL && $scores['short'] >= $adaptiveNormal && $scores['short'] < $adaptiveStrong;

    $longSignal  = $currentSide !== 'long'  && ($canStrongLong  || $canNormLong);
    $shortSignal = $currentSide !== 'short' && ($canStrongShort || $canNormShort);

    if ($longSignal) {
      $strengthTag = $canStrongLong ? '⚡STRONG' : '📶NORMAL';
      logToFile("{$px}🟢 LONG {$strengthTag} score:{$scores['long']} | {$scores['longReason']}");

      if ($currentSide === 'short') {
        closePosition($exchange, 'short', 'flip→long', $currentPrice);
        if (DRY_RUN && $paperEntryPrice > 0.0) {
          $flipPnl = POSITION_USDT * LEVERAGE * (($paperEntryPrice - $currentPrice) / $paperEntryPrice);
          recordPaperTrade($flipPnl, $paperTrades, $paperPnlUsdt, $paperWins, $peakPnl, $maxDrawdown);
          $dailyTradeCount++;
          $dailyLossUsdt += min($flipPnl, 0.0);
          if ($flipPnl >= 0.0) { $consecLosses = 0; } else { $consecLosses++; }
        }
        $currentSide = null; $paperEntryPrice = 0.0;
        $activeSL = 0.0; $activeTP = 0.0; $trailBreakeven = false; $trailLocked = false;
      }

      if ($currentSide === null && openPosition($exchange, 'long', $currentPrice, $cachedAmountPrecision)) {
        $currentSide     = 'long';
        $paperEntryPrice = $currentPrice;
        $activeSL        = $currentPrice - $slDist;
        $activeTP        = 0.0;  // no fixed TP — trail is the exit
        $trailBreakeven  = false;
        $trailLocked     = false;
        $lastSignalTime  = time();
        $positionOpenAt  = time();
        logToFile(sprintf('%s   SL:%.4f (trail-only) ATR:%.5f ATR%%:%.3f%%', $px, $activeSL, $atr, $atrPct));
      }

    } elseif ($shortSignal) {
      $strengthTag = $canStrongShort ? '⚡STRONG' : '📶NORMAL';
      logToFile("{$px}🔴 SHORT {$strengthTag} score:{$scores['short']} | {$scores['shortReason']}");

      if ($currentSide === 'long') {
        closePosition($exchange, 'long', 'flip→short', $currentPrice);
        if (DRY_RUN && $paperEntryPrice > 0.0) {
          $flipPnl = POSITION_USDT * LEVERAGE * (($currentPrice - $paperEntryPrice) / $paperEntryPrice);
          recordPaperTrade($flipPnl, $paperTrades, $paperPnlUsdt, $paperWins, $peakPnl, $maxDrawdown);
          $dailyTradeCount++;
          $dailyLossUsdt += min($flipPnl, 0.0);
          if ($flipPnl >= 0.0) { $consecLosses = 0; } else { $consecLosses++; }
        }
        $currentSide = null; $paperEntryPrice = 0.0;
        $activeSL = 0.0; $activeTP = 0.0; $trailBreakeven = false; $trailLocked = false;
      }

      if ($currentSide === null && openPosition($exchange, 'short', $currentPrice, $cachedAmountPrecision)) {
        $currentSide     = 'short';
        $paperEntryPrice = $currentPrice;
        $activeSL        = $currentPrice + $slDist;
        $activeTP        = 0.0;  // no fixed TP — trail is the exit
        $trailBreakeven  = false;
        $trailLocked     = false;
        $lastSignalTime  = time();
        $positionOpenAt  = time();
        logToFile(sprintf('%s   SL:%.4f (trail-only) ATR:%.5f ATR%%:%.3f%%', $px, $activeSL, $atr, $atrPct));
      }
    }

    // Sync from exchange (guards against liquidation / manual close)
    if (!DRY_RUN) { $currentSide = getOpenPositionSide($exchange); }

  } catch (\Exception $e) {
    logToFile('[!] Loop error: ' . $e->getMessage());
  }

  sleep(LOOP_SLEEP);
}

// ============================================================
//  SESSION SUMMARY
// ============================================================
if (DRY_RUN && $paperTrades > 0) {
  $finalWinRate = round($paperWins / $paperTrades * 100, 1);
  $avgPnl       = round($paperPnlUsdt / $paperTrades, 2);
  logToFile('');
  logToFile('╔══════════════════════════════════════════════════════╗');
  logToFile('║              DRY-RUN SESSION SUMMARY                 ║');
  logToFile('╠══════════════════════════════════════════════════════╣');
  logToFile('║  Trades   : ' . str_pad((string)$paperTrades, 43)                         . '║');
  logToFile('║  Wins     : ' . str_pad($paperWins . ' (' . $finalWinRate . '%)', 43)    . '║');
  logToFile('║  Total PnL: ' . str_pad(number_format($paperPnlUsdt, 2) . ' USDT', 43)  . '║');
  logToFile('║  Avg/trade: ' . str_pad(number_format($avgPnl, 2) . ' USDT', 43)        . '║');
  logToFile('║  Max DD   : ' . str_pad('-' . number_format($maxDrawdown, 2) . ' USDT', 43) . '║');
  logToFile('║  Peak PnL : ' . str_pad(number_format($peakPnl, 2) . ' USDT', 43)      . '║');
  logToFile('╚══════════════════════════════════════════════════════╝');
}

logToFile('[*] Bot shutdown gracefully.');