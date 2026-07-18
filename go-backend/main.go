package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/corentings/chess/v2"
	"github.com/segmentio/kafka-go"
	"log"
	"net/http"
	"strconv"
	"sync"
	"time"
)

const chessAPIURL = "https://chess-api.com/v1"

var chessAPIClient = &http.Client{Timeout: 5 * time.Second}

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
	MaterialSwings  int     `json:"material_swings"`
	CentipawnLoss   int     `json:"centipawn_loss"`
	Evaluation      int     `json:"evaluation"`
	WinChance       float64 `json:"win_chance"`
	Mate            int     `json:"mate"`
}

type GameInfo struct {
	Game       *chess.Game
	time       float64
	MoveNumber int
}

type messageCommitter interface {
	CommitMessages(context.Context, ...kafka.Message) error
}

type SafeMap struct {
	mu   sync.RWMutex
	data map[string]GameInfo
}

type Analysis struct {
	CentipawnLoss int
	Evaluation    int
	WinChance     float64
	Mate          *int
}

type apiResponse struct {
	Centipawns int
	WinChance  float64
	Mate       *int
}

type apiWireResponse struct {
	Centipawns string  `json:"centipawns"`
	WinChance  float64 `json:"winChance"`
	Mate       *int    `json:"mate"`
	Error      string  `json:"error"`
}

func NewSafeMap() *SafeMap {
	return &SafeMap{
		data: make(map[string]GameInfo),
	}
}

func commitProcessedMessage(ctx context.Context, committer messageCommitter, message kafka.Message) error {
	return committer.CommitMessages(ctx, message)
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
		Balancer:     &kafka.CRC32Balancer{},
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

		msg, err := reader.FetchMessage(ctx)

		if err != nil {
			log.Printf("error while reading message: %v", err)
			break
		}

		var event ChessMoveEvent

		if err := json.Unmarshal(msg.Value, &event); err != nil {
			log.Printf("invalid chess-move message: %v", err)
			if err := commitProcessedMessage(ctx, reader, msg); err != nil {
				log.Printf("could not acknowledge invalid chess-move message: %v", err)
				return
			}
			continue
		}

		//fmt.Printf("Game %s, made move: %d. %s\n", event.GameID, event.MoveNumber,event.Move)

		var gameInfo GameInfo
		var exists bool
		var game *chess.Game
		var time float64

		gameInfo, exists = chessGames.Get(event.GameID)

		if exists && event.MoveNumber <= gameInfo.MoveNumber {
			if err := commitProcessedMessage(ctx, reader, msg); err != nil {
				log.Printf("could not acknowledge replayed game %s move %d: %v", event.GameID, event.MoveNumber, err)
				return
			}
			continue
		}

		if !exists {
			if event.MoveNumber != 1 {
				log.Printf("game %s started at move %d; waiting for a replay from move 1", event.GameID, event.MoveNumber)
				return
			}
			gameInfo = GameInfo{Game: chess.NewGame(), time: event.Time}
			fmt.Printf("Game %s\n", event.GameID)
		} else if event.MoveNumber != gameInfo.MoveNumber+1 {
			log.Printf("game %s received move %d after move %d; waiting for ordered replay", event.GameID, event.MoveNumber, gameInfo.MoveNumber)
			return
		}

		game, time = gameInfo.Game.Clone(), gameInfo.time

		var err2 error = game.PushNotationMove(event.Move, chess.AlgebraicNotation{}, nil)

		if err2 != nil {
			log.Printf("game %s has invalid move %s: %v", event.GameID, event.Move, err2)
			if err := commitProcessedMessage(ctx, reader, msg); err != nil {
				log.Printf("could not acknowledge invalid game %s move %d: %v", event.GameID, event.MoveNumber, err)
				return
			}
			continue
		}

		// Temporarily disabled: the Chess API rate limit cannot support one request
		// for the position before and after every move. Re-enable this block when a
		// higher request allowance or a local engine is available.
		var currAnalysis Analysis
		// currAnalysis, analysisErr := AnalyzeLastMove(ctx, game)
		// if analysisErr != nil {
		// 	log.Printf(
		// 		"game %s move %d: Chess API analysis unavailable: %v",
		// 		event.GameID,
		// 		event.MoveNumber,
		// 		analysisErr,
		// 	)
		// }

		var materialImbalance int = findMaterialImbalance(game)

		var multiplier int = -1

		if event.MoveNumber%2 == 1 {
			multiplier = 1
		}

		whiteRating, err := strconv.Atoi(event.WhiteRating)
		blackRating, err := strconv.Atoi(event.BlackRating)

		var mate int = 0

		if currAnalysis.Mate != nil {
			mate = *currAnalysis.Mate
		}

		var currFeatureVector FeatureVector = FeatureVector{GameID: event.GameID,
			MoveNumber:      event.MoveNumber,
			MaterialBalance: materialImbalance, ComplexityScore: len(game.ValidMoves()),
			TimeDelta:      time - event.Time,
			RatingDiff:     multiplier * (whiteRating - blackRating),
			NumMajorPieces: findNumMajorPieces(game, event.MoveNumber), NumMinorPieces: findNumMinorPieces(game, event.MoveNumber),
			MaterialSwings: findMaterialSwings(game),
			CentipawnLoss:  currAnalysis.CentipawnLoss, WinChance: currAnalysis.WinChance, Mate: mate, Evaluation: currAnalysis.Evaluation}

		//fmt.Println(game.Position().Board().Draw())
		//fmt.Println(currFeatureVector)

		jsonData, err := json.Marshal(currFeatureVector)
		if err != nil {
			log.Printf("could not marshal feature for game %s move %d: %v", event.GameID, event.MoveNumber, err)
			return
		}

		err = writer.WriteMessages(ctx, kafka.Message{Key: []byte(event.GameID), Value: jsonData})

		if err != nil {
			log.Printf("could not publish feature for game %s move %d: %v", event.GameID, event.MoveNumber, err)
			return
		}

		if game.Outcome() != chess.NoOutcome {
			completedGame, err := json.Marshal(FeatureVector{
				GameID:       event.GameID,
				GameComplete: true,
			})
			if err != nil {
				log.Printf("could not marshal completed game %s: %v", event.GameID, err)
				return
			} else if err := writer.WriteMessages(ctx, kafka.Message{
				Key:   []byte(event.GameID),
				Value: completedGame,
			}); err != nil {
				log.Printf("could not publish completed game %s: %v", event.GameID, err)
				return
			}
		} else {
			chessGames.Set(event.GameID, GameInfo{
				Game:       game,
				time:       event.Time,
				MoveNumber: event.MoveNumber,
			})
		}

		if err := commitProcessedMessage(ctx, reader, msg); err != nil {
			log.Printf("could not acknowledge game %s move %d: %v", event.GameID, event.MoveNumber, err)
			return
		}

		if game.Outcome() != chess.NoOutcome {
			chessGames.Delete(event.GameID)
		}
	}

}

func findMaterialImbalance(game *chess.Game) int {
	return findMaterialImbalanceForPosition(game.Position())
}

func findMaterialImbalanceForPosition(position *chess.Position) int {

	var materialImbalance int = 0
	var pieceMap map[chess.Square]chess.Piece = position.Board().SquareMap()

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

func findMaterialSwings(game *chess.Game) int {
	history := game.MoveHistory()
	if len(history) == 0 {
		return 0
	}

	lastMove := history[len(history)-1]
	change := findMaterialImbalanceForPosition(lastMove.PostPosition) -
		findMaterialImbalanceForPosition(lastMove.PrePosition)
	if change < 0 {
		return -change
	}
	return change
}

func findNumMinorPieces(game *chess.Game, moveNumber int) int {
	var numPieces int = 0
	var pieceMap map[chess.Square]chess.Piece = game.Position().Board().SquareMap()

	for key := range pieceMap {

		if pieceMap[key].Color() == chess.Black && moveNumber%2 == 0 {

			switch pieceMap[key].Type() {
			case chess.Knight:
				numPieces += 1
			case chess.Bishop:
				numPieces += 1
			}

		} else if pieceMap[key].Color() == chess.White && moveNumber%2 == 1 {
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

		if pieceMap[key].Color() == chess.Black && moveNumber%2 == 0 {

			switch pieceMap[key].Type() {
			case chess.Rook:
				numPieces += 1
			case chess.Queen:
				numPieces += 1
			}

		} else if pieceMap[key].Color() == chess.White && moveNumber%2 == 1 {
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

func AnalyzeLastMove(ctx context.Context, game *chess.Game) (Analysis, error) {
	return analyzeLastMove(ctx, game, chessAPIURL, chessAPIClient)
}

func analyzeLastMove(ctx context.Context, game *chess.Game, endpoint string, client *http.Client) (Analysis, error) {
	if game == nil {
		return Analysis{}, errors.New("chess game is nil")
	}
	history := game.MoveHistory()
	if len(history) == 0 {
		return Analysis{}, errors.New("chess game has no moves")
	}

	last := history[len(history)-1]
	before, err := evaluateFEN(ctx, endpoint, client, last.PrePosition.String())
	if err != nil {
		return Analysis{}, err
	}
	after, err := evaluateFEN(ctx, endpoint, client, last.PostPosition.String())
	if err != nil {
		return Analysis{}, err
	}

	loss := before.Centipawns - after.Centipawns
	if last.PrePosition.Turn() == chess.Black {
		loss = -loss
	}
	if loss < 0 {
		loss = 0
	}
	return Analysis{
		CentipawnLoss: loss,
		Evaluation:    after.Centipawns,
		WinChance:     after.WinChance,
		Mate:          after.Mate,
	}, nil
}

func evaluateFEN(ctx context.Context, endpoint string, client *http.Client, fen string) (apiResponse, error) {
	body, err := json.Marshal(struct {
		FEN string `json:"fen"`
	}{FEN: fen})
	if err != nil {
		return apiResponse{}, err
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return apiResponse{}, err
	}
	request.Header.Set("Content-Type", "application/json")

	response, err := client.Do(request)
	if err != nil {
		return apiResponse{}, err
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return apiResponse{}, fmt.Errorf("chess API returned %s", response.Status)
	}

	var wire apiWireResponse
	if err := json.NewDecoder(response.Body).Decode(&wire); err != nil {
		return apiResponse{}, err
	}
	if wire.Error != "" {
		return apiResponse{}, errors.New(wire.Error)
	}
	centipawns, err := strconv.Atoi(wire.Centipawns)
	if err != nil {
		return apiResponse{}, fmt.Errorf("invalid centipawns: %w", err)
	}

	return apiResponse{
		Centipawns: centipawns,
		WinChance:  wire.WinChance,
		Mate:       wire.Mate,
	}, nil
}
