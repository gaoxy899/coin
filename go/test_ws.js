const WebSocket = require('ws');

const ws1 = new WebSocket('wss://fstream.binance.com/ws/btcusdt@aggTrade');
ws1.on('open', () => console.log('ws1 opened'));
ws1.on('message', (data) => { console.log('ws1 msg:', data.toString()); ws1.close(); });
ws1.on('error', (e) => console.log('ws1 err', e.message));

const ws2 = new WebSocket('wss://fstream.binance.com/market/ws/btcusdt@aggTrade');
ws2.on('open', () => console.log('ws2 opened'));
ws2.on('message', (data) => { console.log('ws2 msg:', data.toString()); ws2.close(); });
ws2.on('error', (e) => console.log('ws2 err', e.message));
