package main

import (
	"database/sql"
	"fmt"
	"log"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	_ "github.com/go-sql-driver/mysql"
)

// Database connection
var db *sql.DB

// Config loader
type Config struct {
	DBHost     string
	DBPort     string
	DBUser     string
	DBPassword string
	DBName     string
	DataSource string // Options: "hyperliquid", "binance", or "binance_spot"
}

func LoadConfig() *Config {
	return &Config{
		DBHost:     getEnv("DB_HOST", "localhost"),
		DBPort:     getEnv("DB_PORT", "3306"),
		DBUser:     getEnv("DB_USER", "root"),
		DBPassword: getEnv("DB_PASSWORD", "password"),
		DBName:     getEnv("DB_NAME", "btc_tracker"),
		DataSource: getEnv("DATA_SOURCE", "binance"),
	}
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

// Current data & synchronization
var (
	dataMu         sync.RWMutex
	currentPrice   float64
	currentOI      float64
	currentFunding float64
	sessionCVD     float64
)

// Database initialization
func initDB(cfg *Config) error {
	dsn := fmt.Sprintf("%s:%s@tcp(%s:%s)/?parseTime=true&charset=utf8mb4",
		cfg.DBUser, cfg.DBPassword, cfg.DBHost, cfg.DBPort)

	var err error
	db, err = sql.Open("mysql", dsn)
	if err != nil {
		return err
	}

	// Create database if not exists
	_, err = db.Exec(fmt.Sprintf("CREATE DATABASE IF NOT EXISTS %s", cfg.DBName))
	if err != nil {
		return err
	}

	// Use the database
	_, err = db.Exec(fmt.Sprintf("USE %s", cfg.DBName))
	if err != nil {
		return err
	}

	// Check and create btc_minutely table if not exists
	var count int
	err = db.QueryRow("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = 'btc_minutely'", cfg.DBName).Scan(&count)
	if err != nil {
		return err
	}
	if count == 0 {
		_, err = db.Exec(`
		CREATE TABLE btc_minutely (
			id BIGINT AUTO_INCREMENT PRIMARY KEY,
			minute_timestamp DATETIME NOT NULL,
			source_id INT NOT NULL,
			timestamp_sec BIGINT NOT NULL,
			price DECIMAL(20, 8) NOT NULL,
			oi DECIMAL(20, 8) NOT NULL,
			funding_rate DECIMAL(20, 8) NOT NULL,
			cvd DECIMAL(20, 8) NOT NULL,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			UNIQUE KEY idx_minute_src (minute_timestamp, source_id),
			INDEX idx_timestamp_sec (timestamp_sec)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
		`)
		if err != nil {
			return err
		}
		log.Println("Created table: btc_minutely")
	} else {
		// Migrate existing table to add timestamp_sec column if missing
		var hasColumn int
		err = db.QueryRow(`
			SELECT COUNT(*) FROM information_schema.columns
			WHERE table_schema = ? AND table_name = 'btc_minutely' AND column_name = 'timestamp_sec'
		`, cfg.DBName).Scan(&hasColumn)
		if err != nil {
			return err
		}
		if hasColumn == 0 {
			_, err = db.Exec("ALTER TABLE btc_minutely ADD COLUMN timestamp_sec BIGINT NOT NULL DEFAULT 0, ADD INDEX idx_timestamp_sec (timestamp_sec)")
			if err != nil {
				return err
			}
			log.Println("Migrated table btc_minutely: added timestamp_sec column")
		}

		// Migrate existing table to add source_id if missing
		var hasSourceID int
		err = db.QueryRow(`
			SELECT COUNT(*) FROM information_schema.columns
			WHERE table_schema = ? AND table_name = 'btc_minutely' AND column_name = 'source_id'
		`, cfg.DBName).Scan(&hasSourceID)
		if err != nil {
			return err
		}
		if hasSourceID == 0 {
			_, err = db.Exec("ALTER TABLE btc_minutely ADD COLUMN source_id INT NOT NULL DEFAULT 2")
			if err != nil {
				return err
			}
			log.Println("Migrated table btc_minutely: added source_id column")
		}

		// Drop unique key idx_minute_timestamp if it exists
		var hasOldIdx int
		err = db.QueryRow(`
			SELECT COUNT(*) FROM information_schema.statistics
			WHERE table_schema = ? AND table_name = 'btc_minutely' AND index_name = 'idx_minute_timestamp'
		`, cfg.DBName).Scan(&hasOldIdx)
		if err != nil {
			return err
		}
		if hasOldIdx > 0 {
			_, err = db.Exec("ALTER TABLE btc_minutely DROP KEY idx_minute_timestamp")
			if err != nil {
				return err
			}
			log.Println("Migrated table btc_minutely: dropped idx_minute_timestamp key")
		}

		// Add unique key idx_minute_src if it doesn't exist
		var hasNewIdx int
		err = db.QueryRow(`
			SELECT COUNT(*) FROM information_schema.statistics
			WHERE table_schema = ? AND table_name = 'btc_minutely' AND index_name = 'idx_minute_src'
		`, cfg.DBName).Scan(&hasNewIdx)
		if err != nil {
			return err
		}
		if hasNewIdx == 0 {
			_, err = db.Exec("ALTER TABLE btc_minutely ADD UNIQUE KEY idx_minute_src (minute_timestamp, source_id)")
			if err != nil {
				return err
			}
			log.Println("Migrated table btc_minutely: added idx_minute_src key")
		}
	}

	log.Println("Database initialized successfully")
	return nil
}

// Fetch price and OI details from implementation
func updateMarketData(src DataSource) error {
	priceVal, oiVal, fundingVal, err := src.FetchPriceAndOI()
	if err != nil {
		return err
	}

	dataMu.Lock()
	currentPrice = priceVal
	currentOI = oiVal
	currentFunding = fundingVal
	dataMu.Unlock()

	log.Printf("Price: %.2f, OI: %.2f, Funding: %.6f", priceVal, oiVal, fundingVal)
	return nil
}

// Save data to database
func saveToDatabase(sourceID int) {
	dataMu.RLock()
	price := currentPrice
	oi := currentOI
	funding := currentFunding
	cvd := sessionCVD
	dataMu.RUnlock()

	if price == 0 {
		return
	}

	now := time.Now()
	minute := time.Date(now.Year(), now.Month(), now.Day(), now.Hour(), now.Minute(), 0, 0, time.Local)
	timestampSec := minute.Unix()

	_, err := db.Exec(`
		INSERT INTO btc_minutely (minute_timestamp, source_id, timestamp_sec, price, oi, funding_rate, cvd)
		VALUES (?, ?, ?, ?, ?, ?, ?)
		ON DUPLICATE KEY UPDATE timestamp_sec = VALUES(timestamp_sec), price = VALUES(price),
			oi = VALUES(oi), funding_rate = VALUES(funding_rate), cvd = VALUES(cvd)
	`, minute, sourceID, timestampSec, price, oi, funding, cvd)

	if err != nil {
		log.Printf("Error saving to btc_minutely: %v", err)
	} else {
		log.Printf("Data saved: SourceID=%d, Price=%.2f, OI=%.2f, Funding=%.6f, CVD=%.2f",
			sourceID, price, oi, funding, cvd)
	}
}

func main() {
	cfg := LoadConfig()

	if err := initDB(cfg); err != nil {
		log.Fatalf("Failed to initialize database: %v", err)
	}
	defer db.Close()

	// Instantiate the dynamic source
	var source DataSource
	switch cfg.DataSource {
	case "binance":
		source = &BinanceSource{}
		log.Println("Crypto Source Chosen: Binance USD-M Futures")
	case "binance_spot":
		source = &BinanceSpotSource{}
		log.Println("Crypto Source Chosen: Binance BTC Spot")
	default:
		source = &HyperliquidSource{}
		log.Println("Crypto Source Chosen: Hyperliquid")
	}

	// Fetch initial values
	if err := updateMarketData(source); err != nil {
		log.Printf("Error fetching initial details: %v", err)
	}

	// Start CVD WebSocket trade listening
	go func() {
		for {
			err := source.StartCVDListener(func(sz float64, isBuy bool) {
				dataMu.Lock()
				if isBuy {
					sessionCVD += sz
				} else {
					sessionCVD -= sz
				}
				currentCVD := sessionCVD
				dataMu.Unlock()
				log.Printf("CVD Update: %.2f", currentCVD)
			})
			if err != nil {
				log.Printf("WebSocket connection error: %v. Retrying in 5 seconds...", err)
				time.Sleep(5 * time.Second)
			}
		}
	}()

	// Parallel thread updates every 10s
	go func() {
		for {
			time.Sleep(10 * time.Second)
			if err := updateMarketData(source); err != nil {
				log.Printf("Error retrieving price/OI snapshot: %v", err)
			}
		}
	}()

	// Database Minutely Closing alignment writes
	go func() {
		for {
			now := time.Now()
			nextMinute := now.Truncate(time.Minute).Add(time.Minute)
			time.Sleep(time.Until(nextMinute))

			// Align closing values
			if err := updateMarketData(source); err != nil {
				log.Printf("Error retrieving close snapshot: %v", err)
			}
			saveToDatabase(source.SourceID())
		}
	}()

	log.Println("BTC Tracker started. Press Ctrl+C to stop.")

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	log.Println("Shutting down...")
}
