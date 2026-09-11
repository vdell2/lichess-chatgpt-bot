import json
import logging
import os
import time

import chess
import chess.engine
import requests


LICHESS_URL = "https://lichess.org"
TOKEN = os.environ.get("LICHESS_TOKEN")
STOCKFISH_PATH = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("lichess-bot")

if not TOKEN:
    raise RuntimeError("LICHESS_TOKEN is missing.")

session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}"})


def api_get(path, stream=False):
    response = session.get(
        f"{LICHESS_URL}{path}",
        stream=stream,
        timeout=None if stream else 30,
    )
    response.raise_for_status()
    return response


def api_post(path, data=None):
    response = session.post(
        f"{LICHESS_URL}{path}",
        data=data or {},
        timeout=30,
    )
    response.raise_for_status()
    return response


def stream_json(response):
    for line in response.iter_lines():
        if line:
            yield json.loads(line.decode("utf-8"))


def accept_challenge(challenge):
    challenge_id = challenge["id"]
    variant = challenge.get("variant", {}).get("key")
    speed = challenge.get("speed")

    if variant != "standard":
        log.info("Declining unsupported variant: %s", variant)
        api_post(
            f"/api/challenge/{challenge_id}/decline",
            {"reason": "variant"},
        )
        return

    if speed in {"ultraBullet"}:
        log.info("Declining game because it is too fast.")
        api_post(
            f"/api/challenge/{challenge_id}/decline",
            {"reason": "tooFast"},
        )
        return

    api_post(f"/api/challenge/{challenge_id}/accept")
    log.info("Accepted challenge %s", challenge_id)


def build_board(initial_fen, moves):
    if initial_fen and initial_fen != "startpos":
        board = chess.Board(initial_fen)
    else:
        board = chess.Board()

    for move_text in moves.split():
        board.push_uci(move_text)

    return board


def play_game(game_id, bot_username):
    log.info("Starting game %s", game_id)
    engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)

    try:
        response = api_get(f"/api/bot/game/stream/{game_id}", stream=True)
        bot_color = None
        initial_fen = "startpos"
        last_position_played = -1

        for event in stream_json(response):
            event_type = event.get("type")

            if event_type == "gameFull":
                initial_fen = event.get("initialFen", "startpos")
                white_name = event.get("white", {}).get("name", "").lower()
                bot_color = (
                    chess.WHITE
                    if white_name == bot_username.lower()
                    else chess.BLACK
                )
                state = event.get("state", {})
            elif event_type == "gameState":
                state = event
            else:
                continue

            status = state.get("status")
            if status != "started":
                log.info("Game %s finished: %s", game_id, status)
                break

            moves = state.get("moves", "")
            board = build_board(initial_fen, moves)
            move_count = board.ply()

            if (
                bot_color is not None
                and board.turn == bot_color
                and move_count != last_position_played
                and not board.is_game_over()
            ):
                last_position_played = move_count

                result = engine.play(
                    board,
                    chess.engine.Limit(time=0.5),
                )
                move = result.move.uci()

                api_post(f"/api/bot/game/{game_id}/move/{move}")
                log.info("Game %s: played %s", game_id, move)

    finally:
        engine.quit()


def run():
    account = api_get("/api/account").json()
    bot_username = account["username"]
    log.info("Logged in as %s", bot_username)

    while True:
        try:
            log.info("Waiting for challenges...")
            response = api_get("/api/stream/event", stream=True)

            for event in stream_json(response):
                event_type = event.get("type")

                if event_type == "challenge":
                    accept_challenge(event["challenge"])

                elif event_type == "gameStart":
                    play_game(event["game"]["id"], bot_username)

        except KeyboardInterrupt:
            log.info("Bot stopped.")
            return
        except Exception:
            log.exception("Connection error. Retrying in 5 seconds.")
            time.sleep(5)


if __name__ == "__main__":
    run()
