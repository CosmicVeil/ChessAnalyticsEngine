package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"github.com/segmentio/kafka-go"
	"github.com/corentings/chess/v2"
)

type ChessMoveEvent struct {
	GameID      string  `json:"game_id"`
	Move        string  `json:"move"`
	MoveNumber  int     `json:"move_number"`
	WhiteRating string  `json:"White Rating"`
	BlackRating string  `json:"Black Rating"`
	Time        float64 `json:"time"`
}

type SafeMap struct {
	mu   sync.RWMutex
	data map[string]*chess.Game
}

func NewSafeMap() *SafeMap {
	return &SafeMap{
		data: make(map[string]*chess.Game),
	}
}

// Set adds or updates an item in the map.
func (sm *SafeMap) Set(key string, value *chess.Game) {
	sm.mu.Lock()         // Acquire write lock
	defer sm.mu.Unlock() // Ensure lock is released when function exits
	sm.data[key] = value
}

// Get retrieves an item from the map.
func (sm *SafeMap) Get(key string) (*chess.Game, bool) {
	sm.mu.RLock()         // Acquire read lock (allows multiple simultaneous readers)
	defer sm.mu.RUnlock() // Ensure read lock is released
	val, exists := sm.data[key]
	return val, exists
}

// Delete removes an item from the map.
func (sm *SafeMap) Delete(key string) {
	sm.mu.Lock()         // Acquire write lock
	defer sm.mu.Unlock() // Ensure lock is released
	delete(sm.data, key)
}

var chessGames SafeMap = *NewSafeMap()

func main() {

    reader := kafka.NewReader(kafka.ReaderConfig{
    		Brokers:  []string{"localhost:19092"}, // List of bootstrap brokers
    		Topic:    "chess-moves",       // The target Kafka topic
    		GroupID:  "chess-state-managers",        // Enables consumer group offset tracking
    		MinBytes: 10e3,                       // 10KB (minimum batch fetch size)
    		MaxBytes: 10e6,                       // 10MB (maximum batch fetch size)
    		StartOffset: kafka.LastOffset,
    	})

    defer func() {

        if err := reader.Close(); err != nil {
            log.Printf("failed to close reader: %v", err)
        }

    } ()

    ctx := context.Background()
	for {

		msg, err := reader.ReadMessage(ctx)


		if err != nil {
			log.Printf("error while reading message: %v", err)
			break
		}

        var event ChessMoveEvent
		err = json.Unmarshal(msg.Value, &event)

		// 4. Process the message payload
		fmt.Printf("Game %s, made move: %s", event.GameID, event.Move)

		var game *chess.Game
		var exists bool

		game, exists  = chessGames.Get(event.GameID)

		if !exists{
			chessGames.Set(event.GameID, chess.NewGame())
			game,_ = chessGames.Get(event.GameID)
		}

		var err2 error = game.PushNotationMove(event.Move, chess.AlgebraicNotation{}, nil)

		if err2 != nil {
			fmt.Printf("game %s skipped move %s\n", event.GameID, event.Move)
			continue
		}

		fmt.Println(game.Position().Board().Draw())
    }



}


