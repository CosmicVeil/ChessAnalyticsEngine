package main

// Local Stockfish analysis for the four engine features.
//
// These rules must match ml-model/engine_features.py, which filled the same
// columns in the training data:
//   - scores are centipawns from White's side, clamped to +/-1000;
//   - a forced mate counts as the clamp value;
//   - a checkmated position is scored directly (the side to move has lost),
//     and a stalemate is 0, because terminal positions have no search score;
//   - every search uses a fixed node budget so results do not depend on speed.

import (
	"errors"
	"math"
	"os"
	"os/exec"

	"github.com/corentings/chess/v2"
	"github.com/corentings/chess/v2/uci"
)

const (
	evaluationClamp = 1000
	analysisNodes   = 5000
	winChanceSlope  = 0.00368208
)

// positionScore is one position's evaluation from White's side.
type positionScore struct {
	Centipawns int // clamped to +/-evaluationClamp
	Mate       int // moves to mate from White's side, 0 when none
}

type positionEvaluator interface {
	Evaluate(position *chess.Position) (positionScore, error)
}

// stockfishEvaluator runs searches in one long-lived Stockfish process.
type stockfishEvaluator struct {
	engine *uci.Engine
}

// stockfishPath honours STOCKFISH_PATH, then looks for stockfish on PATH.
func stockfishPath() (string, error) {
	if path := os.Getenv("STOCKFISH_PATH"); path != "" {
		return path, nil
	}
	return exec.LookPath("stockfish")
}

func newStockfishEvaluator(path string) (*stockfishEvaluator, error) {
	engine, err := uci.New(path)
	if err != nil {
		return nil, err
	}
	// One thread and a small hash keep searches comparable to training.
	setup := []uci.Cmd{
		uci.CmdUCI,
		uci.CmdSetOption{Name: "Threads", Value: "1"},
		uci.CmdSetOption{Name: "Hash", Value: "16"},
		uci.CmdIsReady,
		uci.CmdUCINewGame,
	}
	if err := engine.Run(setup...); err != nil {
		_ = engine.Close()
		return nil, err
	}
	return &stockfishEvaluator{engine: engine}, nil
}

func (s *stockfishEvaluator) Evaluate(position *chess.Position) (positionScore, error) {
	if score, terminal := terminalScore(position); terminal {
		return score, nil
	}
	err := s.engine.Run(uci.CmdPosition{Position: position}, uci.CmdGo{Nodes: analysisNodes})
	if err != nil {
		return positionScore{}, err
	}
	score := s.engine.SearchResults().Info.Score
	return whiteScore(score.CP, score.Mate, position.Turn() == chess.White), nil
}

func (s *stockfishEvaluator) Close() error {
	return s.engine.Close()
}

// terminalScore scores positions that have no legal moves without searching.
func terminalScore(position *chess.Position) (positionScore, bool) {
	switch position.Status() {
	case chess.Checkmate:
		if position.Turn() == chess.White {
			return positionScore{Centipawns: -evaluationClamp}, true
		}
		return positionScore{Centipawns: evaluationClamp}, true
	case chess.Stalemate:
		return positionScore{}, true
	}
	return positionScore{}, false
}

// whiteScore converts a side-to-move UCI score to White's side and clamps it.
func whiteScore(centipawns, mate int, whiteToMove bool) positionScore {
	if !whiteToMove {
		centipawns, mate = -centipawns, -mate
	}
	if mate > 0 {
		return positionScore{Centipawns: evaluationClamp, Mate: mate}
	}
	if mate < 0 {
		return positionScore{Centipawns: -evaluationClamp, Mate: mate}
	}
	return positionScore{Centipawns: max(-evaluationClamp, min(evaluationClamp, centipawns))}
}

func winChance(evaluation int) float64 {
	return 50 + 50*(2/(1+math.Exp(-winChanceSlope*float64(evaluation)))-1)
}

// analyzeLastMove computes the engine features for the game's latest move.
func analyzeLastMove(evaluator positionEvaluator, game *chess.Game) (Analysis, error) {
	if game == nil {
		return Analysis{}, errors.New("chess game is nil")
	}
	history := game.MoveHistory()
	if len(history) == 0 {
		return Analysis{}, errors.New("chess game has no moves")
	}
	last := history[len(history)-1]
	before, err := evaluator.Evaluate(last.PrePosition)
	if err != nil {
		return Analysis{}, err
	}
	after, err := evaluator.Evaluate(last.PostPosition)
	if err != nil {
		return Analysis{}, err
	}

	loss := before.Centipawns - after.Centipawns
	if last.PrePosition.Turn() == chess.Black {
		loss = -loss
	}
	mate := after.Mate
	return Analysis{
		CentipawnLoss: max(0, loss),
		Evaluation:    after.Centipawns,
		WinChance:     winChance(after.Centipawns),
		Mate:          &mate,
	}, nil
}
