#!/usr/bin/env python
import os
from random import choice
from confluent_kafka import Producer
import chess.pgn
import time
import json

if __name__ == '__main__':

    config = {
        # User-specific properties that you must set
        'bootstrap.servers': 'localhost:19092',

        # Fixed properties
        'acks': 'all'
    }

    pgn = open(os.path.dirname(os.getcwd()) + "/data/sample.pgn")
    pgn2 = open(os.path.dirname(os.getcwd()) + "/data/AI_sample.pgn")



    # Create Producer instance
    producer = Producer(config)

    # Optional per-message delivery callback (triggered by poll() or flush())
    # when a message has been successfully delivered or permanently
    # failed delivery (after retries).
    def delivery_callback(err, msg):
        if err:
            print('ERROR: Message failed delivery: {}'.format(err))
        else:
            print("Produced event to topic {topic}: key = {key:12} value = {value:12}".format(
                topic=msg.topic(), key=msg.key().decode('utf-8'), value=msg.value().decode('utf-8')))


    while True:
        curr_game = chess.pgn.read_game(pgn)


        if curr_game is None:
            break

        board = curr_game.board()

        num_moves = 0
        game_node = curr_game


        for move in curr_game.mainline_moves():

            san_move = board.san(move)
            num_moves+=1

            game_node = game_node.next()

            payload = {
                "game_id" : curr_game.headers.get("White") + " vs. " + curr_game.headers.get("Black") + " Clean",
                "move": san_move,
                "move_number": num_moves,
                "White Rating": curr_game.headers.get("WhiteElo"),
                "Black Rating": curr_game.headers.get("BlackElo"),
                "time": game_node.clock(),
            }
            producer.produce(topic="chess-moves", key=payload.get("game_id"), value=json.dumps(payload, indent = 4))
            producer.poll(0)

            board.push(move)

        producer.flush()

    while True:
        curr_game = chess.pgn.read_game(pgn2)


        if curr_game is None:
            break

        board = curr_game.board()

        num_moves = 0
        game_node = curr_game


        for move in curr_game.mainline_moves():

            san_move = board.san(move)
            num_moves+=1

            game_node = game_node.next()

            payload = {
                "game_id" : curr_game.headers.get("White") + " vs. " + curr_game.headers.get("Black") + " Cheating",
                "move": san_move,
                "move_number": num_moves,
                "White Rating": curr_game.headers.get("WhiteElo"),
                "Black Rating": curr_game.headers.get("BlackElo"),
                "time": game_node.clock(),
            }
            producer.produce(topic="chess-moves", key=payload.get("game_id"), value=json.dumps(payload, indent = 4))
            producer.poll(0)
            #time.sleep(0.05)

            board.push(move)

        producer.flush()

