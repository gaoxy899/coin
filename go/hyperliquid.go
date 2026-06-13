package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

type HyperliquidSource struct{}

func (h *HyperliquidSource) SourceID() int {
	return 2
}

type HLMetaResponse struct {
	Universe []struct {
		Name string `json:"name"`
	} `json:"universe"`
}

type HLAssetCtx struct {
	MarkPx       string `json:"markPx"`
	OpenInterest string `json:"openInterest"`
	Funding      string `json:"funding"`
}

type HLMetaAndAssetCtxsResponse []json.RawMessage

type HLTrade struct {
	Side string `json:"side"`
	Sz   string `json:"sz"`
}

type HLWSTradeMsg struct {
	Channel string    `json:"channel"`
	Data    []HLTrade `json:"data"`
}

func (h *HyperliquidSource) FetchPriceAndOI() (price float64, oi float64, funding float64, err error) {
	reqBody := map[string]string{"type": "metaAndAssetCtxs"}
	bodyBytes, _ := json.Marshal(reqBody)

	resp, err := http.Post("https://api.hyperliquid.xyz/info",
		"application/json", strings.NewReader(string(bodyBytes)))
	if err != nil {
		return 0, 0, 0, err
	}
	defer resp.Body.Close()

	var data HLMetaAndAssetCtxsResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		return 0, 0, 0, err
	}

	if len(data) < 2 {
		return 0, 0, 0, fmt.Errorf("invalid response")
	}

	var meta HLMetaResponse
	if err := json.Unmarshal(data[0], &meta); err != nil {
		return 0, 0, 0, err
	}

	btcIndex := -1
	for i, asset := range meta.Universe {
		if asset.Name == "BTC" {
			btcIndex = i
			break
		}
	}

	if btcIndex == -1 {
		return 0, 0, 0, fmt.Errorf("BTC not found in universe")
	}

	var assetCtxs []json.RawMessage
	if err := json.Unmarshal(data[1], &assetCtxs); err != nil {
		return 0, 0, 0, err
	}

	if btcIndex >= len(assetCtxs) {
		return 0, 0, 0, fmt.Errorf("BTC index out of range")
	}

	var assetCtx HLAssetCtx
	if err := json.Unmarshal(assetCtxs[btcIndex], &assetCtx); err != nil {
		return 0, 0, 0, err
	}

	if _, err := fmt.Sscanf(assetCtx.MarkPx, "%f", &price); err != nil {
		return 0, 0, 0, fmt.Errorf("error parsing markPx: %v", err)
	}
	if _, err := fmt.Sscanf(assetCtx.OpenInterest, "%f", &oi); err != nil {
		return 0, 0, 0, fmt.Errorf("error parsing openInterest: %v", err)
	}
	if _, err := fmt.Sscanf(assetCtx.Funding, "%f", &funding); err != nil {
		return 0, 0, 0, fmt.Errorf("error parsing funding: %v", err)
	}

	return price, oi, funding, nil
}

func (h *HyperliquidSource) StartCVDListener(onTrade func(sz float64, isBuy bool)) error {
	dialer := websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
	}

	ws, _, err := dialer.Dial("wss://api.hyperliquid.xyz/ws", nil)
	if err != nil {
		return err
	}
	defer ws.Close()

	subscribeTrades := `{"method":"subscribe","subscription":{"type":"trades","coin":"BTC"}}`
	if err := ws.WriteMessage(websocket.TextMessage, []byte(subscribeTrades)); err != nil {
		return err
	}

	log.Println("Hyperliquid WebSocket connected, subscribed to trades")

	for {
		_, message, err := ws.ReadMessage()
		if err != nil {
			return err
		}

		if strings.Contains(string(message), "\"trades\"") {
			var msg HLWSTradeMsg
			if err := json.Unmarshal(message, &msg); err == nil {
				if msg.Channel == "trades" && len(msg.Data) > 0 {
					for _, trade := range msg.Data {
						var size float64
						fmt.Sscanf(trade.Sz, "%f", &size)
						isBuy := trade.Side == "B"
						onTrade(size, isBuy)
					}
				}
			}
		}
	}
}
