"""
게임 흐름 제어 모듈.
각 게임 인스턴스는 독립적인 asyncio.Task로 실행되며, Phase를 순서대로 전환한다.

Phase 순서:
  ROUND_START → EXPERIENCE_SHARING → FREE_CHAT → JUDGING → (반복 or GAME_OVER)
"""
import asyncio
import logging
import random
import time
from typing import Optional

from .config import settings
from .connection_manager import connection_manager
from .llm_agents import AIPlayer, LLMJudge
from .models import (
    ChatMessage,
    GameMode,
    GamePhase,
    GameResult,
    Player,
    WsMessage,
    WsMessageType,
)
from .state_manager import GameState, game_manager

logger = logging.getLogger(__name__)

# 제시어 목록 (30개)
PROMPT_WORDS = [
    "첫사랑", "군대", "야식", "졸업식", "친구와 싸운 날",
    "생일", "여행", "아르바이트", "시험 전날", "실수한 순간",
    "고향", "반려동물", "선생님", "용돈", "비 오는 날",
    "게임", "부모님", "이사", "운동", "명절",
    "학교 급식", "꿈", "버스", "여름 방학", "겨울",
    "노래방", "도서관", "취업 준비", "밤새우기", "길을 잃은 날",
]

# 솔로 모드용 AI 닉네임 풀
AI_NICKNAMES = [
    "알파", "베타", "감마", "델타", "엡실론",
    "제타", "에타", "세타", "아이오타", "카파",
]


def _build_player_list_payload(game: GameState) -> dict:
    """게임 참가자 목록을 클라이언트에 전달할 형태로 직렬화한다."""
    return {
        "game_id": game.game_id,
        "mode": game.mode,
        "players": [
            {
                "id": p.id,
                "nickname": p.nickname,
                "is_human": p.is_human,  # 프론트에서 자기 자신 확인용
                "is_eliminated": p.is_eliminated,
            }
            for p in game.players
        ],
    }


async def start_game(game: GameState) -> None:
    """
    게임의 메인 루프를 실행한다.
    각 라운드를 순차적으로 진행하고, 종료 조건에 도달하면 게임을 종료한다.
    """
    # AI 플레이어 에이전트 초기화 (게임당 독립 인스턴스)
    ai_agents: dict[str, AIPlayer] = {
        p.id: AIPlayer(p) for p in game.players if not p.is_human
    }
    judge = LLMJudge()

    # 게임 시작 알림
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.GAME_START,
            data=_build_player_list_payload(game),
        ),
    )

    used_words: set[str] = set()

    while True:
        # ── 종료 조건 체크 ──────────────────────────────────────────
        result = _check_game_result(game)
        if result is not None:
            await _handle_game_over(game, result)
            return

        # ── ROUND_START ──────────────────────────────────────────────
        game.round_number += 1
        game.phase = GamePhase.ROUND_START
        game.experience_submissions = {}
        game.chat_history = []

        # 제시어 선택 (중복 방지)
        available = [w for w in PROMPT_WORDS if w not in used_words]
        if not available:
            available = PROMPT_WORDS  # 전부 소진됐을 경우 재사용
            used_words.clear()
        prompt_word = random.choice(available)
        used_words.add(prompt_word)
        game.current_prompt_word = prompt_word

        # 생존 플레이어 순서 섞기
        alive = game.alive_players
        random.shuffle(alive)
        game.experience_order = [p.id for p in alive]
        game.experience_index = 0

        await connection_manager.broadcast_to_game(
            game,
            WsMessage(
                type=WsMessageType.ROUND_START,
                data={
                    "round": game.round_number,
                    "prompt_word": prompt_word,
                    "order": [
                        {"id": p.id, "nickname": p.nickname}
                        for p in alive
                    ],
                },
            ),
        )

        # ── EXPERIENCE_SHARING ────────────────────────────────────────
        game.phase = GamePhase.EXPERIENCE_SHARING
        await _run_experience_sharing(game, ai_agents)

        # ── FREE_CHAT ──────────────────────────────────────────────────
        game.phase = GamePhase.FREE_CHAT
        await _run_free_chat(game, ai_agents)

        # ── JUDGING ────────────────────────────────────────────────────
        game.phase = GamePhase.JUDGING
        await _run_judging(game, judge)


# ────────────────────────────────────────────────────────────────────
# Phase 구현부
# ────────────────────────────────────────────────────────────────────

async def _run_experience_sharing(
    game: GameState, ai_agents: dict[str, AIPlayer]
) -> None:
    """
    플레이어 순서대로 경험담을 제출받는다.
    AI는 자동 생성, 인간은 타임아웃 내 제출 대기.
    """
    for pid in game.experience_order:
        player = next((p for p in game.alive_players if p.id == pid), None)
        if player is None or player.is_eliminated:
            continue

        # 현재 차례 알림 (모든 인간에게 브로드캐스트)
        await connection_manager.broadcast_to_game(
            game,
            WsMessage(
                type=WsMessageType.EXPERIENCE_REQUEST,
                data={
                    "current_player_id": pid,
                    "current_nickname": player.nickname,
                    "timeout": settings.EXPERIENCE_TIMEOUT,
                },
            ),
        )

        if not player.is_human:
            # AI는 즉시 경험담 생성 (약간의 자연스러운 딜레이)
            await asyncio.sleep(random.uniform(5.0, 8.0))
            experience = await ai_agents[pid].generate_experience(
                game.current_prompt_word
            )
            game.experience_submissions[pid] = experience
            await connection_manager.broadcast_to_game(
                game,
                WsMessage(
                    type=WsMessageType.EXPERIENCE_SUBMITTED,
                    data={
                        "player_id": pid,
                        "nickname": player.nickname,
                        "content": experience,
                    },
                ),
            )
        else:
            # 인간: 타임아웃 내 submit_experience 이벤트 대기
            submitted = await _wait_for_experience(
                game, pid, settings.EXPERIENCE_TIMEOUT
            )
            if not submitted:
                # 타임아웃: 자동 패스
                game.experience_submissions[pid] = "(시간 초과)"
                await connection_manager.broadcast_to_game(
                    game,
                    WsMessage(
                        type=WsMessageType.EXPERIENCE_TIMEOUT,
                        data={"player_id": pid, "nickname": player.nickname},
                    ),
                )
        
        # 차례 넘기기
        game.experience_index += 1


async def _wait_for_experience(
    game: GameState, player_id: str, timeout: int
) -> bool:
    """
    해당 플레이어의 경험담 제출을 타임아웃까지 기다린다.
    제출되면 True, 타임아웃이면 False.
    game.experience_submissions에 값이 들어오는 것을 감시한다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if player_id in game.experience_submissions:
            return True
        await asyncio.sleep(0.2)
    return False


async def _run_free_chat(
    game: GameState, ai_agents: dict[str, AIPlayer]
) -> None:
    """
    2분(FREE_CHAT_DURATION) 자유 대화 시간을 관리한다.
    AI 하이브리드 채팅 전략을 백그라운드 Task로 실행한다.
    """
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.FREE_CHAT_START,
            data={"duration": settings.FREE_CHAT_DURATION},
        ),
    )

    # AI 채팅 Task를 동시에 실행
    ai_tasks = [
        asyncio.create_task(
            _ai_chat_loop(game, ai_agents[p.id], settings.FREE_CHAT_DURATION)
        )
        for p in game.alive_ais
        if p.id in ai_agents
    ]

    # 자유 대화 타이머
    await asyncio.sleep(settings.FREE_CHAT_DURATION)

    # 타이머 종료 → AI 채팅 Task 취소
    for task in ai_tasks:
        task.cancel()
    await asyncio.gather(*ai_tasks, return_exceptions=True)

    await connection_manager.broadcast_to_game(
        game,
        WsMessage(type=WsMessageType.FREE_CHAT_END, data={}),
    )


async def _ai_chat_loop(
    game: GameState, ai: AIPlayer, duration: int
) -> None:
    """
    하이브리드 AI 채팅 전략 루프.
    - 반응형: 마지막 메시지 이후 일정 딜레이 뒤 응답
    - 선제형: 30초 이상 침묵 시 선제 발화
    - 연속 발화 방지: 직전 메시지가 본인이면 선제 발화 생략
    """
    start = time.monotonic()
    last_seen_count = 0       # 마지막으로 처리한 채팅 수
    last_chat_time = start    # 마지막 메시지 수신 시각
    stop_spontaneous_at = start + duration - 30  # 종료 30초 전부터 선제 발화 금지

    # 경험담 정보를 문자열로 구성
    experience_lines = []
    for p in game.alive_players:
        exp = game.experience_submissions.get(p.id, "(미제출)")
        experience_lines.append(f"- {p.nickname}: {exp}")
    experience_text = "\n".join(experience_lines)

    while True:
        now = time.monotonic()
        current_count = len(game.chat_history)

        if current_count > last_seen_count:
            # 새 메시지 감지 → 반응형 딜레이 후 응답
            last_seen_count = current_count
            last_chat_time = now
            await asyncio.sleep(ai.reaction_delay)

            # 딜레이 중 게임 상태 재확인
            if game.phase != GamePhase.FREE_CHAT or ai.player.is_eliminated:
                return

            # 직전 메시지가 본인이면 반응 생략 (연속 발화 방지)
            if (
                game.chat_history
                and game.chat_history[-1].player_id == ai.player.id
            ):
                continue

            content = await ai.generate_chat_response(game.chat_history, experience_text)
            await _broadcast_ai_chat(game, ai.player, content)

        elif now - last_chat_time > AIPlayer._SPONTANEOUS_IDLE_SEC:
            # 30초 이상 침묵 → 선제 발화 (종료 30초 전 이후에는 생략)
            if now < stop_spontaneous_at:
                # 직전 메시지가 본인이면 생략
                if not (
                    game.chat_history
                    and game.chat_history[-1].player_id == ai.player.id
                ):
                    content = await ai.generate_spontaneous_message(game.chat_history, experience_text)
                    await _broadcast_ai_chat(game, ai.player, content)
                last_chat_time = now  # 선제 발화 후 타이머 리셋

        await asyncio.sleep(0.5)


async def _broadcast_ai_chat(
    game: GameState, player: Player, content: str
) -> None:
    """AI 채팅 메시지를 게임 채팅 히스토리에 기록하고 브로드캐스트한다."""
    msg = ChatMessage(
        player_id=player.id,
        nickname=player.nickname,
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
                "nickname": player.nickname,
                "content": content,
                "timestamp": msg.timestamp,
            },
        ),
    )


async def _run_judging(game: GameState, judge: LLMJudge) -> None:
    """LLM 판정관을 호출하고 탈락자를 처리한다."""
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(type=WsMessageType.JUDGING_START, data={}),
    )

    result = await judge.judge(
        alive_players=game.alive_players,
        chat_history=game.chat_history,
        experience_submissions=game.experience_submissions,
    )

    # LLM 판정 실패 시 랜덤 탈락 (서비스 연속성 보장)
    if result is None:
        logger.warning(f"[game_logic] 판정 실패 → 랜덤 탈락 처리 (game={game.game_id})")
        fallback_player = random.choice(game.alive_players)
        from .models import JudgeResult
        result = JudgeResult(
            eliminated_player_id=fallback_player.id,
            eliminated_nickname=fallback_player.nickname,
            human_probability=50,
            reason="판정 시스템 오류로 인해 랜덤 탈락이 적용되었습니다.",
        )

    # 탈락 처리
    eliminated = next(
        (p for p in game.alive_players if p.id == result.eliminated_player_id),
        None,
    )
    if eliminated:
        eliminated.is_eliminated = True
        logger.info(
            f"[game_logic] 탈락: {eliminated.nickname} "
            f"(is_human={eliminated.is_human}, prob={result.human_probability}%)"
        )

    # 판정 결과 브로드캐스트
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.JUDGE_RESULT,
            data={
                "eliminated_player_id": result.eliminated_player_id,
                "eliminated_nickname": result.eliminated_nickname,
                "human_probability": result.human_probability,
                "reason": result.reason,
                "was_human": eliminated.is_human if eliminated else None,
            },
        ),
    )


def _check_game_result(game: GameState) -> Optional[GameResult]:
    """
    현재 게임 상태를 보고 종료 조건을 판단한다.

    솔로 모드 (인간1 vs AI4):
      - 인간이 탈락 → AI 승리
      - AI가 모두 탈락 → 인간 승리

    단체 모드 (인간4 vs AI1):
      - AI가 탈락 → 인간 승리
      - 인간이 모두 탈락 → AI 승리
    """
    if not game.alive_humans:
        # 인간이 한 명도 없음 → AI 승리
        return GameResult.AI_WIN

    if not game.alive_ais:
        # AI가 한 명도 없음 → 인간 승리
        return GameResult.HUMAN_WIN

    return None  # 아직 종료 조건 미달


async def _handle_game_over(game: GameState, result: GameResult) -> None:
    """게임 종료를 처리하고 결과를 브로드캐스트한다."""
    game.phase = GamePhase.GAME_OVER
    logger.info(f"[game_logic] 게임 종료: game_id={game.game_id}, result={result}")

    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.GAME_OVER,
            data={
                "result": result.value,
                "message": (
                    "🎉 인간 승리! AI를 모두 찾아냈습니다."
                    if result == GameResult.HUMAN_WIN
                    else "😈 AI 승리! 인간이 모두 탈락했습니다."
                ),
                "players": [
                    {
                        "id": p.id,
                        "nickname": p.nickname,
                        "is_human": p.is_human,
                        "is_eliminated": p.is_eliminated,
                    }
                    for p in game.players
                ],
            },
        ),
    )

    # 게임 정리
    game_manager.remove_game(game.game_id)


def create_ai_players(count: int) -> list[Player]:
    """AI 플레이어 인스턴스를 생성한다."""
    nicknames = random.sample(AI_NICKNAMES, min(count, len(AI_NICKNAMES)))
    return [
        Player(nickname=nick, is_human=False)
        for nick in nicknames[:count]
    ]
