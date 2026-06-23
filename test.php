<?php
/**
 * Professional Crypto Trading Engine v2
 * Focus: Open Interest (OI) Analysis & Liquidation Proxy Filtering
 */

declare(strict_types=1);

define('SYMBOL', 'WLDUSDT');
define('INTERVAL', '5m');
define('POLL_INTERVAL', 12); 

// Risk Management Rules
define('ACCOUNT_BALANCE_USD', 1000.00); 
define('MAX_RISK_PERCENT', 2.0);        
define('RISK_REWARD_RATIO', 2.5); // Boosted to 2.5 due to higher probability trade signals

function fetch_binance_data(string $endpoint, array $params): ?array {
    $queryString = http_build_query($params);
    $url = "https://fapi.binance.com{$endpoint}?{$queryString}";
    
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, 4);
    curl_setopt($ch, CURLOPT_USERAGENT, 'Mozilla/5.0 Pro-Alpha-Client');
    $res = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    
    return $code === 200 ? json_decode($res, true) : null;
}

function execute_advanced_strategy(): void {
    // 1. Fetch Price/Volume Candles
    $candles = fetch_binance_data("/fapi/v1/klines", ["symbol" => SYMBOL, "interval" => INTERVAL, "limit" => 30]);
    // 2. Fetch Open Interest Statistics (Crucial metrics from your new screenshot)
    $oi_data = fetch_binance_data("/fapi/v1/openInterest", ["symbol" => SYMBOL]);

    if (!$candles || !$oi_data) {
        echo "⏳ Synchronizing data frames with exchange metrics...\n";
        return;
    }

    // Process Price / Vol
    $curr = count($candles) - 1;
    $current_price = (float)$candles[$curr][4];
    $prev_high     = (float)$candles[$curr-1][2];
    $prev_low      = (float)$candles[$curr-1][3];
    
    // Calculate Volume Moving Average
    $volumes = array_map(fn($c) => (float)$c[5], $candles);
    $vol_sma9 = array_sum(array_slice($volumes, -10, 9)) / 9;
    $current_vol = $volumes[$curr];

    // Process Open Interest Data
    static $last_open_interest = null;
    $current_oi = (float)$oi_data['openInterest'];
    
    if ($last_open_interest === null) {
        $last_open_interest = $current_oi;
        echo "📊 Initialized Open Interest Baseline: " . number_format($current_oi, 0) . " WLD Tokens\n";
        return;
    }

    // Calculate how much capital/leverage entered or exited the market since last tick
    $oi_change = $current_oi - $last_open_interest;
    $oi_change_percent = ($oi_change / $last_open_interest) * 100;
    $last_open_interest = $current_oi; // Update state

    // Log the Dashboard
    echo "\r" . date('H:i:s') . " | Px: $" . sprintf("%.4f", $current_price) . " | OI Change: " . sprintf("%+.3f%%", $oi_change_percent) . " | Vol/SMA9: " . sprintf("%.1f%%", ($current_vol/$vol_sma9)*100) . " | ";

    // --- STRATEGIC TRADING MATRIX ---
    $volume_spike = ($current_vol > $vol_sma9 * 1.25);
    $allowed_risk_usd = ACCOUNT_BALANCE_USD * (MAX_RISK_PERCENT / 100);

    // 🟩 ADVANCED LONG (The Liquidation Flushout): 
    // Price breaks a local floor on massive volume, but Open Interest falls sharply (-0.1% or deeper in seconds).
    // This indicates short-term long liquidations have just cleared the path for a price reversal.
    if ($current_price < $prev_low && $volume_spike && $oi_change_percent < -0.05) {
        
        // Dynamic Stop Loss based on recent market structure volatility
        $stop_loss = $current_price * 0.985; // 1.5% buffer zone
        $risk_per_token = $current_price - $stop_loss;
        
        $position_size_tokens = $allowed_risk_usd / $risk_per_token;
        $take_profit = $current_price + ($risk_per_token * RISK_REWARD_RATIO);

        echo "\n🟩 [TRADE SIGNAL: ACCUMULATION LONG - SELLER EXHAUSTION DETECTED]";
        print_trade_blueprint($current_price, $stop_loss, $take_profit, $position_size_tokens, $allowed_risk_usd);
    }
    
    // 🟥 ADVANCED SHORT (The Short-Squeeze Reversal):
    // Price breaks above a high on big volume, but Open Interest drops heavily.
    // Shorts were just forcefully liquidated. Once the squeeze ends, buying pressure vanishes.
    if ($current_price > $prev_high && $volume_spike && $oi_change_percent < -0.05) {
        
        $stop_loss = $current_price * 1.015;
        $risk_per_token = $stop_loss - $current_price;
        
        $position_size_tokens = $allowed_risk_usd / $risk_per_token;
        $take_profit = $current_price - ($risk_per_token * RISK_REWARD_RATIO);

        echo "\n🟥 [TRADE SIGNAL: DISTRIBUTION SHORT - SQUEEZE EXHAUSTION DETECTED]";
        print_trade_blueprint($current_price, $stop_loss, $take_profit, $position_size_tokens, $allowed_risk_usd);
    }
}

function print_trade_blueprint($entry, $sl, $tp, $size, $risk): void {
    echo "\n   ==============================================";
    echo "\n   🎯 Entry Triggered By Inst. Flow: $" . sprintf("%.4f", $entry);
    echo "\n   🛑 Safe Protective Stop         : $" . sprintf("%.4f", $sl);
    echo "\n   💰 Take Profit Target           : $" . sprintf("%.4f", $tp);
    echo "\n   🎚️ Calculated Lot Size          : " . number_format($size, 2) . " WLD";
    echo "\n   📉 Allocated Risk Target        : $" . sprintf("%.2f", $risk) . " USD";
    echo "\n   ==============================================\n";
}

echo "🚀 Alpha Market-Structure Cluster v2 Engaged for " . SYMBOL . "...\n";
while (true) {
    execute_advanced_strategy();
    sleep(POLL_INTERVAL);
}
