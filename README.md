## BTC 交易策略

### go 
获取btc 价格、OI、CVD，按分钟存入数据库
运行
```
DB_HOST=dbhost DB_USER=dbuser DB_PASSWORD=password  DATA_SOURCE=binance ./run.sh
```

### api 
前端页面显示提供接口

```
DB_HOST=dbhost DB_USER=dbuser DB_PASSWORD=password ./run.sh
```

### html
前端页面， 支持部署到cloudflare pages
