package main

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/corentings/chess/v2"
	"github.com/segmentio/kafka-go"
)

type recordingCommitter struct {
	committed []kafka.Message
	err       error
}

func (committer *recordingCommitter) CommitMessages(_ context.Context, messages ...kafka.Message) error {
	committer.committed = append(committer.committed, messages...)
	return committer.err
}

func TestCommitProcessedMessageCommitsOnlyTheFetchedMessage(t *testing.T) {
	message := kafka.Message{Topic: "chess-moves", Partition: 2, Offset: 14}
	committer := &recordingCommitter{}

	if err := commitProcessedMessage(context.Background(), committer, message); err != nil {
		t.Fatalf("commitProcessedMessage() error = %v", err)
	}

	if len(committer.committed) != 1 {
		t.Fatalf("committed %d messages, want 1", len(committer.committed))
	}
	got := committer.committed[0]
	if got.Topic != message.Topic || got.Partition != message.Partition || got.Offset != message.Offset {
		t.Fatalf("committed message = %#v, want %#v", got, message)
	}
}

func TestCommitProcessedMessageReturnsCommitError(t *testing.T) {
	wantErr := errors.New("broker unavailable")
	committer := &recordingCommitter{err: wantErr}

	if err := commitProcessedMessage(context.Background(), committer, kafka.Message{}); !errors.Is(err, wantErr) {
		t.Fatalf("commitProcessedMessage() error = %v, want %v", err, wantErr)
	}
}

func TestFindMaterialSwingsReturnsCaptureValue(t *testing.T) {
	game := chess.NewGame()
	for _, move := range []string{"e4", "d5", "exd5"} {
		if err := game.PushNotationMove(move, chess.AlgebraicNotation{}, nil); err != nil {
			t.Fatalf("PushNotationMove(%q) error = %v", move, err)
		}
	}

	if got := findMaterialSwings(game); got != 1 {
		t.Fatalf("findMaterialSwings() = %d, want 1 for a pawn capture", got)
	}
}

func TestFindMaterialSwingsReturnsZeroForNonCapture(t *testing.T) {
	game := chess.NewGame()
	if err := game.PushNotationMove("e4", chess.AlgebraicNotation{}, nil); err != nil {
		t.Fatalf("PushNotationMove() error = %v", err)
	}

	if got := findMaterialSwings(game); got != 0 {
		t.Fatalf("findMaterialSwings() = %d, want 0 for a non-capture", got)
	}
}

func TestChessAPIClientHasBoundedTimeout(t *testing.T) {
	if chessAPIClient.Timeout <= 0 {
		t.Fatal("chess API client must have a timeout")
	}
}

func TestEvaluateFENReturnsAPIError(t *testing.T) {
	response, err := evaluateFEN(
		context.Background(),
		"http://127.0.0.1:1",
		chessAPIClient,
		"invalid-fen",
	)
	if err == nil {
		t.Fatalf("evaluateFEN() response = %+v, want connection error", response)
	}
	if !strings.Contains(err.Error(), "connect") && !strings.Contains(err.Error(), "connection") {
		t.Fatalf("evaluateFEN() error = %v, want connection error", err)
	}
}
