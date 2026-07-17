package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strconv"
	"sync"
	"time"

	"github.com/corentings/chess/v2"
	"github.com/segmentio/kafka-go"
)

type ChessMoveEvent struct {
	GameID      string  `json:"game_id"`
	Move        string  `json:"move"`
	MoveNumber  int     `json:"move_number"`
	WhiteRating string  `json:"White Rating"`
	BlackRating string  `json:"Black Rating"`
	Time        float64 `json:"time"`
}

type FeatureVector struct {
	GameID          string  `json:"game_id"`
	MoveNumber      int     `json:"move_number"`
	MaterialBalance int     `json:"material_balance"`
	ComplexityScore int     `json:"complexity_score"`
	TimeDelta       float64 `json:"time_delta"`
	GameComplete    bool    `json:"game_complete,omitempty"`
	RatingDiff      int     `json:"rating_diff"`
	NumMinorPieces  int     `json:"num_minor_pieces"`
	NumMajorPieces  int     `json:"num_major_pieces"`
	MaterialSwings  int `json:"material_swings"`
}

type GameInfo struct {
	Game *chess.Game
	time float64
}

type SafeMap struct {
	mu   sync.RWMutex
	data map[string]GameInfo
}

func NewSafeMap() *SafeMap {
	return &SafeMap{
		data: make(map[string]GameInfo),
	}
}

// Set adds or updates an item in the map.
func (sm *SafeMap) Set(key string, value GameInfo) {
	sm.mu.Lock()         // Acquire write lock
	defer sm.mu.Unlock() // Ensure lock is released when function exits
	sm.data[key] = value
}

// Get retrieves an item from the map.
func (sm *SafeMap) Get(key string) (GameInfo, bool) {
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
		Brokers:     []string{"localhost:19092"},
		Topic:       "chess-moves",
		GroupID:     "chess-state-managers",
		MinBytes:    10e3,
		MaxBytes:    10e6,
		StartOffset: kafka.LastOffset,
	})

	writer := kafka.Writer{
		Addr:         kafka.TCP("localhost:19092"),
		Topic:        "chess-features",
		Balancer:     &kafka.LeastBytes{},
		BatchSize:    1,
		BatchTimeout: 7 * time.Millisecond,
	}

	defer func() {

		if err := reader.Close(); err != nil {
			log.Printf("failed to close reader: %v", err)
		}

	}()

	defer func() {
		if err := writer.Close(); err != nil {
			log.Fatalf("failed to close writer: %v", err)
		}
	}()

	ctx := context.Background()

	for {

		msg, err := reader.ReadMessage(ctx)

		if err != nil {
			log.Printf("error while reading message: %v", err)
			break
		}

		var event ChessMoveEvent

		if err := json.Unmarshal(msg.Value, &event); err != nil {
			log.Println(err)
			continue
		}

		//fmt.Printf("Game %s, made move: %d. %s\n", event.GameID, event.MoveNumber,event.Move)

		var gameInfo GameInfo
		var exists bool
		var game *chess.Game
		var time float64

		gameInfo, exists = chessGames.Get(event.GameID)

		if !exists || event.MoveNumber == 1 {
			chessGames.Set(event.GameID, GameInfo{chess.NewGame(), event.Time})
			gameInfo, _ = chessGames.Get(event.GameID)

			fmt.Printf("Game %s\n", event.GameID)
		}

		game, time = gameInfo.Game, gameInfo.time

		var err2 error = game.PushNotationMove(event.Move, chess.AlgebraicNotation{}, nil)

		if err2 != nil {
			fmt.Printf("game %s skipped move %s: %v\n", event.GameID, event.Move, err2)
			continue
		}

		var materialImbalance int = findMaterialImbalance(game)

		var multiplier int = -1

		if event.MoveNumber % 2 == 1{
			multiplier = 1
		}

		whiteRating, err := strconv.Atoi(event.WhiteRating)
		blackRating, err := strconv.Atoi(event.BlackRating)

		var currFeatureVector FeatureVector = FeatureVector{GameID: event.GameID,
			MoveNumber:      event.MoveNumber,
			MaterialBalance: materialImbalance, ComplexityScore: len(game.ValidMoves()),
			TimeDelta: time - event.Time,
		RatingDiff: multiplier*(whiteRating-blackRating),
		NumMajorPieces: findNumMajorPieces(game, event.MoveNumber), NumMinorPieces: findNumMinorPieces(game, event.MoveNumber)}

		//fmt.Println(game.Position().Board().Draw())
		//fmt.Println(currFeatureVector)

		jsonData, err := json.Marshal(currFeatureVector)
		if err != nil {
			log.Fatalf("Error marshaling to JSON: %v", err)
		}

		err = writer.WriteMessages(ctx, kafka.Message{Key: []byte(event.GameID), Value: jsonData})

		if err != nil {
			fmt.Printf("Could not write message to writer: %v", err)
		}

		if err != nil {
			log.Fatalf("could not write messages: %v", err)
		}

		gameInfo.time = event.Time
		chessGames.Set(event.GameID, gameInfo)

		if game.Outcome() != chess.NoOutcome {
			completedGame, err := json.Marshal(FeatureVector{
				GameID:       event.GameID,
				GameComplete: true,
			})
			if err != nil {
				log.Printf("could not marshal completed game: %v", err)
			} else if err := writer.WriteMessages(ctx, kafka.Message{
				Key:   []byte(event.GameID),
				Value: completedGame,
			}); err != nil {
				log.Printf("could not publish completed game: %v", err)
			}
			chessGames.Delete(event.GameID)
		}
	}

}

func findMaterialImbalance(game *chess.Game) int {

	var materialImbalance int = 0
	var pieceMap map[chess.Square]chess.Piece = game.Position().Board().SquareMap()

	for key := range pieceMap {

		if pieceMap[key].Color() == chess.Black {

			switch pieceMap[key].Type() {
			case chess.Pawn:
				materialImbalance -= 1
			case chess.Knight:
				materialImbalance -= 3
			case chess.Bishop:
				materialImbalance -= 3
			case chess.Rook:
				materialImbalance -= 5
			case chess.Queen:
				materialImbalance -= 10
			case chess.King:
				materialImbalance -= 0
			}

		} else {
			switch pieceMap[key].Type() {
			case chess.Pawn:
				materialImbalance += 1
			case chess.Knight:
				materialImbalance += 3
			case chess.Bishop:
				materialImbalance += 3
			case chess.Rook:
				materialImbalance += 5
			case chess.Queen:
				materialImbalance += 10
			case chess.King:
				materialImbalance += 0
			}
		}
	}

	return materialImbalance
}

func findNumMinorPieces(game *chess.Game, moveNumber int) int {
	var numPieces int = 0
	var pieceMap map[chess.Square]chess.Piece = game.Position().Board().SquareMap()

	for key := range pieceMap {

		if pieceMap[key].Color() == chess.Black && moveNumber %2 ==0{

			switch pieceMap[key].Type() {
				case chess.Knight:
					numPieces += 1
				case chess.Bishop:
					numPieces += 1
			}

		} else if pieceMap[key].Color() == chess.White && moveNumber %2 == 1{
			switch pieceMap[key].Type() {
			case chess.Knight:
				numPieces += 1
			case chess.Bishop:
				numPieces += 1
			}
		}
	}

	return numPieces
}

func findNumMajorPieces(game *chess.Game, moveNumber int) int {
	var numPieces int = 0
	var pieceMap map[chess.Square]chess.Piece = game.Position().Board().SquareMap()

	for key := range pieceMap {

		if pieceMap[key].Color() == chess.Black && moveNumber %2 ==0{

			switch pieceMap[key].Type() {
			case chess.Rook:
				numPieces += 1
			case chess.Queen:
				numPieces += 1
			}

		} else if pieceMap[key].Color() == chess.White && moveNumber %2 == 1{
			switch pieceMap[key].Type() {
			case chess.Rook:
				numPieces += 1
			case chess.Queen:
				numPieces += 1
			}
		}
	}

	return numPieces
}
