"""
FastAPI 애플리케이션 진입점.
WebSocket 엔드포인트와 REST API를 정의한다.
"""
import asyncio
import logging
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .connection_manager import connection_manager
from .game_logic import create_ai_players, start_game
from .models import GameMode, Player, WsMessage, WsMessageType, ChatMessage
from .state_manager import game_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="AI 마피아 게임 서버", version="1.0.0")

# CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 실제 서비스 시에는 허용할 프론트엔드 도메인만 지정
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────
# REST API
# ──────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """서버 및 게임 상태 헬스체크"""
    return JSONResponse({
        "status": "ok",
        "active_games": game_manager.status["active_games"],
        "group_waiting": game_manager.status["group_waiting"],
    })


@app.get("/status")
async def server_status():
    """상세 서버 상태 (진행 중인 게임 목록)"""
    games = []
    for game_id, game in game_manager.active_games.items():
        games.append({
            "game_id": game_id,
            "mode": game.mode,
            "phase": game.phase,
            "round": game.round_number,
            "alive_players": [
                {"id": p.id, "is_human": p.is_human}
                for p in game.alive_players
            ],
        })
    return JSONResponse({
        "active_games": games,
        "group_waiting_count": game_manager.status["group_waiting"],
        "group_waiting_players": [
            {"id": p.id}
            for p in game_manager.group_waiting_room
        ],
    })


# ──────────────────────────────────────────────
# WebSocket 엔드포인트
# ──────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(
    ws: WebSocket,
    mode: str = Query(..., description="게임 모드: solo | group"),
):
    """
    클라이언트 접속 처리.
    - solo 모드: 즉시 새 게임 생성 (AI 4명 자동 투입)
    - group 모드: 대기방 배치, 4명 충족 시 게임 시작
    """
    # 모드 유효성 검사
    try:
        game_mode = GameMode(mode)
    except ValueError:
        await ws.accept()
        await ws.send_json({"type": "error", "data": {"detail": f"유효하지 않은 모드: {mode}"}})
        await ws.close()
        return

    # 플레이어 생성 및 연결
    player = Player(is_human=True, ws=ws)
    await connection_manager.connect(player.id, ws)
    await ws.send_json({"type": "connected", "data": {"player_id": player.id}})

    game = None
    game_task = None

    try:
        if game_mode == GameMode.SOLO:
            game = await _handle_solo_join(player)
            game_task = asyncio.create_task(start_game(game))

        else:  # GROUP
            game = await _handle_group_join(player)
            if game:
                # 4명 모인 경우 → 게임 시작
                game_task = asyncio.create_task(start_game(game))
            else:
                # 대기 중 → 수신 루프만 유지
                pass

        # 메시지 수신 루프
        await _message_loop(player, game)

    except WebSocketDisconnect:
        logger.info(f"[main] 연결 끊김: ({player.id})")
        await _handle_disconnect(player, game)

    finally:
        connection_manager.disconnect(player.id)
        if game_task and not game_task.done():
            # 게임이 종료되지 않은 상태에서 마지막 인간이 나갔을 경우 Task 유지
            # (다른 인간이 있다면 게임 계속)
            pass


async def _handle_solo_join(player: Player):
    """솔로 모드: AI 플레이어를 생성하고 즉시 게임을 만든다."""
    ai_players = create_ai_players(settings.SOLO_AI_COUNT)
    all_players = [player] + ai_players
    game = game_manager.create_game(all_players, GameMode.SOLO)
    logger.info(f"[main] 솔로 게임 생성: vs AI {settings.SOLO_AI_COUNT}명")
    return game


async def _handle_group_join(player: Player):
    """
    단체 모드: 대기방에 추가하고, 4명 충족 시 게임을 반환한다.
    대기 중이면 None 반환.
    """
    ready_players = game_manager.add_to_group_waiting(player)
    if not ready_players:
        # 아직 대기 중 → 대기방 상태 브로드캐스트
        waiting_count = len(game_manager.group_waiting_room)
        await connection_manager.send(
            player.id,
            WsMessage(
                type=WsMessageType.WAITING_ROOM,
                data={
                    "waiting_count": waiting_count,
                    "required": settings.GROUP_MIN_PLAYERS,
                    "message": f"대기 중... ({waiting_count}/{settings.GROUP_MIN_PLAYERS}명)",
                },
            ),
        )
        return None

    # 4명 충족 → AI 투입 + 게임 생성
    ai_players = create_ai_players(settings.GROUP_AI_COUNT)
    all_players = list(ready_players) + ai_players
    game = game_manager.create_game(all_players, GameMode.GROUP)
    logger.info(
        f"[main] 단체 게임 생성: "
        f"인간 {len(ready_players)}명 vs AI {settings.GROUP_AI_COUNT}명"
    )
    return game


async def _message_loop(player: Player, game) -> None:
    """
    클라이언트로부터 메시지를 수신하고 처리하는 루프.
    연결이 끊기거나 게임이 종료될 때까지 실행된다.
    """
    ws = player.ws
    while True:
        try:
            data = await ws.receive_json()
        except Exception:
            raise WebSocketDisconnect()

        msg_type = data.get("type")

        # 아직 게임에 배정되지 않은 경우(단체 대기 중) → 게임 찾기
        if game is None:
            game = game_manager.find_game_by_player(player.id)

        if game is None:
            continue  # 아직 대기 중

        if msg_type == WsMessageType.SUBMIT_EXPERIENCE:
            await _handle_experience_submit(player, game, data)

        elif msg_type == WsMessageType.SEND_CHAT:
            await _handle_chat(player, game, data)

        elif msg_type == WsMessageType.TYPING_STATUS:
            is_typing = data.get("data", {}).get("is_typing", False)
            await connection_manager.broadcast_to_game(
                game,
                WsMessage(
                    type=WsMessageType.TYPING_STATUS,
                    data={
                        "player_id": player.id,
                        "is_typing": is_typing,
                    },
                ),
                exclude_player_id=player.id,
            )

        else:
            logger.warning(f"[main] 알 수 없는 메시지 타입: {msg_type}")


async def _handle_experience_submit(player: Player, game, data: dict) -> None:
    """경험담 제출 처리"""
    from .models import GamePhase

    if game.phase != GamePhase.EXPERIENCE_SHARING:
        await connection_manager.send_error(player.id, "경험 공유 단계가 아닙니다.")
        return

    # 현재 차례가 본인인지 확인
    if game.experience_index < len(game.experience_order):
        current_pid = game.experience_order[game.experience_index]
        if current_pid != player.id:
            await connection_manager.send_error(player.id, "현재 당신의 차례가 아닙니다.")
            return

    content = str(data.get("data", {}).get("content", "")).strip()
    if not content:
        await connection_manager.send_error(player.id, "내용이 비어 있습니다.")
        return

    # 2줄 제한 (클라이언트 신뢰하지 않고 서버에서도 강제)
    lines = content.split("\n")
    if len(lines) > 2:
        content = "\n".join(lines[:2])

    game.experience_submissions[player.id] = content
    logger.info(f"[main] 경험담 제출: {player.id}")

    # 제출 완료를 게임 전체에 브로드캐스트
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.EXPERIENCE_SUBMITTED,
            data={
                "player_id": player.id,
                "content": content,
            },
        ),
    )


async def _handle_chat(player: Player, game, data: dict) -> None:
    """자유 대화 채팅 처리"""
    from .models import GamePhase

    if game.phase != GamePhase.FREE_CHAT:
        await connection_manager.send_error(player.id, "자유 대화 단계가 아닙니다.")
        return

    if player.is_eliminated:
        await connection_manager.send_error(player.id, "탈락한 플레이어는 채팅할 수 없습니다.")
        return

    content = str(data.get("data", {}).get("content", "")).strip()
    if not content:
        return

    # 최대 200자 제한
    if len(content) > 200:
        content = content[:200]

    msg = ChatMessage(
        player_id=player.id,
        content=content,
        timestamp=time.time(),
    )
    game.chat_history.append(msg)

    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.CHAT_MESSAGE,
            data={
                "player_id": player.id,
                "content": content,
                "timestamp": msg.timestamp,
            },
        ),
    )


async def _handle_disconnect(player: Player, game) -> None:
    """
    연결 끊김 처리.
    - 대기 중: 대기방에서 제거
    - 게임 중: 탈락 처리 후 게임 계속
    """
    if game is None:
        game = game_manager.find_game_by_player(player.id)

    if game is None:
        # 대기방 제거
        game_manager.remove_from_waiting(player.id)
        return

    player_in_game = next(
        (p for p in game.players if p.id == player.id), None
    )
    if player_in_game and not player_in_game.is_eliminated:
        player_in_game.is_eliminated = True
        logger.info(f"[main] 연결 끊김으로 탈락: {player.id}")

        await connection_manager.broadcast_to_game(
            game,
            WsMessage(
                type=WsMessageType.PLAYER_ELIMINATED,
                data={
                    "player_id": player.id,
                    "reason": "연결 끊김",
                },
            ),
        )
