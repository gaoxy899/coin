package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
)

type BinanceSource struct{}

func (b *BinanceSource) SourceID() int {
	return 1
}

type BinancePremiumIndex struct {
	Symbol          string `json:"symbol"`
	MarkPrice       string `json:"markPrice"`
	LastFundingRate string `json:"lastFundingRate"`
}

type BinanceOpenInterest struct {
	Symbol       string `json:"symbol"`
	OpenInterest string `json:"openInterest"`
}

type BinanceWSTrade struct {
	EventType    string `json:"e"` // Event type (e.g. aggTrade)
	EventTime    int64  `json:"E"`
	Symbol       string `json:"s"`
	Price        string `json:"p"`
	Quantity     string `json:"q"`
	IsBuyerMaker bool   `json:"m"` // True = Sell (Market Maker is Buyer), False = Buy (Market Maker is Seller)
}

func (b *BinanceSource) FetchPriceAndOI() (price float64, oi float64, funding float64, err error) {
	// 1. Fetch Mark Price and Last Funding Rate
	resp, err := http.Get("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")
	if err != nil {
		return 0, 0, 0, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return 0, 0, 0, fmt.Errorf("unexpected status from premiumIndex: %d", resp.StatusCode)
	}

	var index BinancePremiumIndex
	if err := json.NewDecoder(resp.Body).Decode(&index); err != nil {
		return 0, 0, 0, err
	}

	// 2. Fetch Open Interest
	respOI, err := http.Get("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT")
	if err != nil {
		return 0, 0, 0, err
	}
	defer respOI.Body.Close()

	if respOI.StatusCode != http.StatusOK {
		return 0, 0, 0, fmt.Errorf("unexpected status from openInterest: %d", respOI.StatusCode)
	}

	var oiData BinanceOpenInterest
	if err := json.NewDecoder(respOI.Body).Decode(&oiData); err != nil {
		return 0, 0, 0, err
	}

	// Parse fields
	if _, err := fmt.Sscanf(index.MarkPrice, "%f", &price); err != nil {
		return 0, 0, 0, fmt.Errorf("error parsing MarkPrice: %v", err)
	}
	if _, err := fmt.Sscanf(index.LastFundingRate, "%f", &funding); err != nil {
		return 0, 0, 0, fmt.Errorf("error parsing LastFundingRate: %v", err)
	}
	if _, err := fmt.Sscanf(oiData.OpenInterest, "%f", &oi); err != nil {
		return 0, 0, 0, fmt.Errorf("error parsing OpenInterest: %v", err)
	}

	return price, oi, funding, nil
}

func (b *BinanceSource) StartCVDListener(onTrade func(sz float64, isBuy bool)) error {
	dialer := websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
		Proxy:            http.ProxyFromEnvironment,
	}

	// Connect to Binance USD-M Futures trade stream for btcusdt
	ws, _, err := dialer.Dial("wss://fstream.binance.com/market/ws/btcusdt@aggTrade", nil)
	if err != nil {
		return err
	}
	defer ws.Close()

	log.Println("Binance WebSocket connected, subscribed to btcusdt@aggTrade")

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
