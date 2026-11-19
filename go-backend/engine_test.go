package main

import (
	"math"
	"testing"

	"github.com/corentings/chess/v2"
)

// fakeEvaluator returns scores by FEN so analysis can be tested without Stockfish.
type fakeEvaluator map[string]positionScore

func (f fakeEvaluator) Evaluate(position *chess.Position) (positionScore, error) {
	return f[position.String()], nil
}

func playMoves(t *testing.T, moves ...string) *chess.Game {
	t.Helper()
	game := chess.NewGame()
	for _, move := range moves {
		if err := game.PushNotationMove(move, chess.AlgebraicNotation{}, nil); err != nil {
			t.Fatalf("move %s: %v", move, err)
		}
	}
	return game
}

func TestWhiteScoreConvertsSideToMoveAndClamps(t *testing.T) {
	cases := []struct {
		name        string
		cp, mate    int
		whiteToMove bool
		want        positionScore
	}{
		{"white to move keeps sign", 120, 0, true, positionScore{Centipawns: 120}},
		{"black to move flips sign", 120, 0, false, positionScore{Centipawns: -120}},
		{"large scores clamp", 5000, 0, true, positionScore{Centipawns: evaluationClamp}},
		{"mate for side to move", 0, 3, false, positionScore{Centipawns: -evaluationClamp, Mate: -3}},
		{"mate against side to move", 0, -2, false, positionScore{Centipawns: evaluationClamp, Mate: 2}},
	}
	for _, tc := range cases {
		if got := whiteScore(tc.cp, tc.mate, tc.whiteToMove); got != tc.want {
			t.Errorf("%s: whiteScore() = %+v, want %+v", tc.name, got, tc.want)
		}
	}
}

func TestTerminalScoreHandlesCheckmateAndStalemate(t *testing.T) {
	mated := playMoves(t, "f3", "e5", "g4", "Qh4#").Position()
	if score, terminal := terminalScore(mated); !terminal || score.Centipawns != -evaluationClamp {
		t.Fatalf("checkmated White: got %+v terminal=%v", score, terminal)
	}

	stalemateFEN, err := chess.FEN("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
	if err != nil {
		t.Fatal(err)
	}
	stalemate := chess.NewGame(stalemateFEN).Position()
	if score, terminal := terminalScore(stalemate); !terminal || score != (positionScore{}) {
		t.Fatalf("stalemate: got %+v terminal=%v", score, terminal)
	}

	if _, terminal := terminalScore(chess.NewGame().Position()); terminal {
		t.Fatal("starting position must not be terminal")
	}
}

func TestAnalyzeLastMoveMeasuresLossFromTheMoversSide(t *testing.T) {
	game := playMoves(t, "e4", "e5")
	last := game.MoveHistory()[1]
	// Black's move takes White from +30 to +90: a 60 centipawn loss for Black.
	evaluator := fakeEvaluator{
		last.PrePosition.String():  {Centipawns: 30},
		last.PostPosition.String(): {Centipawns: 90},
	}
	analysis, err := analyzeLastMove(evaluator, game)
	if err != nil {
		t.Fatal(err)
	}
	if analysis.CentipawnLoss != 60 || analysis.Evaluation != 90 || *analysis.Mate != 0 {
		t.Fatalf("analysis = %+v", analysis)
	}

	// The same swing is a gain for White, so White's loss would be zero.
	evaluator[last.PostPosition.String()] = positionScore{Centipawns: -40}
	analysis, _ = analyzeLastMove(evaluator, game)
	if analysis.CentipawnLoss != 0 {
		t.Fatalf("an improving move must have zero loss, got %d", analysis.CentipawnLoss)
	}
}

func TestAnalyzeLastMoveRejectsEmptyGames(t *testing.T) {
	if _, err := analyzeLastMove(fakeEvaluator{}, chess.NewGame()); err == nil {
		t.Fatal("expected an error for a game with no moves")
	}
	if _, err := analyzeLastMove(fakeEvaluator{}, nil); err == nil {
		t.Fatal("expected an error for a nil game")
	}
}

func TestWinChanceMatchesTheTrainingFormula(t *testing.T) {
	// Values from ml-model/engine_features.py's win_chance.
	for evaluation, want := range map[int]float64{0: 50, -70: 43.59179701815651, 1000: 97.54474363414323} {
		if got := winChance(evaluation); math.Abs(got-want) > 1e-9 {
			t.Errorf("winChance(%d) = %v, want %v", evaluation, got, want)
		}
	}
}

func TestStockfishEvaluatorFindsFoolsMate(t *testing.T) {
	path, err := stockfishPath()
	if err != nil {
		t.Skip("Stockfish is not installed")
	}
	evaluator, err := newStockfishEvaluator(path)
	if err != nil {
		t.Fatal(err)
	}
	defer evaluator.Close()

	// After 3. g4 Black mates in one; the Python dataset tool scores this
	// move as a 921 centipawn loss (+79 before, -1000 after).
	blunder, err := analyzeLastMove(evaluator, playMoves(t, "f3", "e5", "g4"))
	if err != nil {
		t.Fatal(err)
	}
	if blunder.Evaluation != -evaluationClamp || *blunder.Mate != -1 || blunder.CentipawnLoss < 900 {
		t.Fatalf("blunder analysis = %+v mate=%d", blunder, *blunder.Mate)
	}

	mate, err := analyzeLastMove(evaluator, playMoves(t, "f3", "e5", "g4", "Qh4#"))
	if err != nil {
		t.Fatal(err)
	}
	if mate.Evaluation != -evaluationClamp || mate.CentipawnLoss != 0 {
		t.Fatalf("mating move analysis = %+v", mate)
	}
}
