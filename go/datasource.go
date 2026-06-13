package main

// DataSource defines the interface representing a crypto feed source
type DataSource interface {
	SourceID() int
	FetchPriceAndOI() (price float64, oi float64, funding float64, err error)
	StartCVDListener(onTrade func(sz float64, isBuy bool)) error
}
