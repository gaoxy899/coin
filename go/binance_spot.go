package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
)

type BinanceSpotSource struct{}

func (b *BinanceSpotSource) SourceID() int {
	return 3
}

type BinanceSpotPrice struct {
	Symbol string `json:"symbol"`
	Price  string `json:"price"`
}

func (b *BinanceSpotSource) FetchPriceAndOI() (price float64, oi float64, funding float64, err error) {
	// 1. Fetch Spot Price
	resp, err := http.Get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
	if err != nil {
		return 0, 0, 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0, 0, 0, fmt.Errorf("unexpected status from spot ticker price API: %d", resp.StatusCode)
	}

	var data BinanceSpotPrice
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return 0, 0, 0, err
	}

	if _, err := fmt.Sscanf(data.Price, "%f", &price); err != nil {
		return 0, 0, 0, fmt.Errorf("error parsing spot Price: %v", err)
	}

	// Spot markets do not have Open Interest or Funding Rates, so we default them to 0.0
	return price, 0.0, 0.0, nil
}

func (b *BinanceSpotSource) StartCVDListener(onTrade func(sz float64, isBuy bool)) error {
	dialer := websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
		Proxy:            http.ProxyFromEnvironment,
	}

	// Connect to Binance Spot trade stream for btcusdt
	ws, _, err := dialer.Dial("wss://stream.binance.com:9443/ws/btcusdt@trade", nil)
	if err != nil {
		return err
	}
	defer ws.Close()

	log.Println("Binance Spot WebSocket connected, subscribed to btcusdt@trade")

	for {
		_, message, err := ws.ReadMessage()
		if err != nil {
			return err
		}

		var trade BinanceWSTrade
		if err := json.Unmarshal(message, &trade); err == nil {
			var size float64
			fmt.Sscanf(trade.Quantity, "%f", &size)
			// 'm' is true means buyer is market maker, which is a taker sell side (isBuy = false).
			// 'm' is false means seller is market maker, which is a taker buy side (isBuy = true).
			isBuy := !trade.IsBuyerMaker
			onTrade(size, isBuy)
		}
	}
}
