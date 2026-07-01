import chess.pgn

pgn = open("../data/sample.pgn")

first_game = chess.pgn.read_game(pgn)

board = first_game.board()

for move in first_game.mainline_moves():

    san_move = board.san(move)
    print(san_move)

    board.push(move)


