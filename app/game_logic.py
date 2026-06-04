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

# AI 플레이어 생성을 위한 모듈 (이제 닉네임 사용 안함)


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
    try:
        # 닉네임 자동 부여 (Player 1, Player 2 ...)
        for idx, p in enumerate(game.players):
            p.nickname = f"Player {idx + 1}"

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
                            {"id": p.id}
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

            # ── JUDGING or VOTING ───────────────────────────────────────────
            if game.mode == GameMode.SOLO:
                game.phase = GamePhase.JUDGING
                await _run_judging(game, judge)
            else:
                game.phase = GamePhase.VOTING
                game.votes.clear()
                await _run_voting(game, ai_agents)

    except Exception as e:
        logger.error(f"[game_logic] 게임 강제 종료 (game_id={game.game_id}): {e}", exc_info=True)
    finally:
        # 게임 종료 또는 에러 발생 시 메모리 누수(좀비 게임) 방지
        game_manager.remove_game(game.game_id)


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
                    "timeout": settings.EXPERIENCE_TIMEOUT,
                },
            ),
        )

        if not player.is_human:
            # AI는 즉시 경험담 생성 (약간의 자연스러운 딜레이)
            await _broadcast_typing(game, pid, True)
            await asyncio.sleep(random.uniform(5.0, 8.0))
            experience = await ai_agents[pid].generate_experience(
                game.current_prompt_word
            )
            await _broadcast_typing(game, pid, False)
            game.experience_submissions[pid] = experience
            await connection_manager.broadcast_to_game(
                game,
                WsMessage(
                    type=WsMessageType.EXPERIENCE_SUBMITTED,
                    data={
                        "player_id": pid,
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
                        data={"player_id": pid},
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
        if not game.alive_humans:
            return False  # 모든 인간이 나가면 조기 탈출
        if player_id in game.experience_submissions:
            return True
        await asyncio.sleep(0.2)
    return False


async def _broadcast_typing(game: GameState, player_id: str, is_typing: bool) -> None:
    """AI 플레이어의 타이핑 상태를 브로드캐스트한다."""
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.TYPING_STATUS,
            data={
                "player_id": player_id,
                "is_typing": is_typing,
            },
        ),
    )


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

    # 자유 대화 타이머 (Fail-safe: 인간이 모두 나가면 조기 종료)
    deadline = time.monotonic() + settings.FREE_CHAT_DURATION
    while time.monotonic() < deadline:
        if not game.alive_humans:
            break
        await asyncio.sleep(1.0)

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
    타이머 기반 AI 채팅 전략 루프.
    - 반응형/선제형 구분 없이 일정 주기(5~10초)마다 무작위로 발화
    - 연속 발화 방지 (직전 메시지가 본인이면 생략)
    """
    # 경험담 정보를 문자열로 구성
    experience_lines = []
    for p in game.alive_players:
        exp = game.experience_submissions.get(p.id, "(미제출)")
        experience_lines.append(f"- {p.nickname}: {exp}")
    experience_text = "\n".join(experience_lines)

    while True:
        # 랜덤 딜레이 (5~10초)
        delay = ai.chat_interval
        await asyncio.sleep(delay)

        # 딜레이 후 게임 상태 및 생존 여부 재확인
        if game.phase != GamePhase.FREE_CHAT or ai.player.is_eliminated:
            return

        # 직전 메시지가 본인이면 발화 생략 (연속 발화 방지)
        if (
            game.chat_history
            and game.chat_history[-1].player_id == ai.player.id
        ):
            continue
            
        await _broadcast_typing(game, ai.player.id, True)
        
        # 실제 타이핑 시간 느낌을 위해 약간의 추가 대기
        await asyncio.sleep(random.uniform(1.5, 3.0))
        
        content = await ai.generate_chat_response(game.chat_history, experience_text)
        
        await _broadcast_typing(game, ai.player.id, False)
        
        # 찰나의 순간에 게임이 넘어갔을 수 있으므로 재확인
        if game.phase != GamePhase.FREE_CHAT or ai.player.is_eliminated:
            return
            
        await _broadcast_ai_chat(game, ai.player, content)


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
            f"[game_logic] 탈락: {eliminated.id} "
            f"(is_human={eliminated.is_human}, prob={result.human_probability}%)"
        )

    # 판정 결과 브로드캐스트
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.JUDGE_RESULT,
            data={
                "eliminated_player_id": result.eliminated_player_id,
                "human_probability": result.human_probability,
                "reason": result.reason,
                "was_human": eliminated.is_human if eliminated else None,
            },
        ),
    )


async def _run_voting(game: GameState, ai_agents: dict[str, AIPlayer]) -> None:
    """
    그룹 모드(인간 4 vs AI 1)에서 진행되는 플레이어 투표 단계.
    인간 플레이어는 클라이언트로부터 submit_vote를 받고,
    AI 플레이어는 LLM을 통해 투표 대상을 결정한다.
    """
    timeout = 30  # 투표 제한 시간(초)
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.VOTING_START,
            data={"timeout": timeout},
        ),
    )

    # AI 플레이어 투표 (비동기로 실행)
    ai_tasks = []
    for p in game.alive_ais:
        if p.id in ai_agents:
            # AIPlayer.generate_vote 호출
            ai_tasks.append(
                asyncio.create_task(
                    _ai_vote_task(game, ai_agents[p.id])
                )
            )

    # 인간 플레이어의 투표 대기 (timeout 또는 모두 투표 완료 시까지)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not game.alive_humans:
            break
        # 생존한 모든 플레이어가 투표를 완료했는지 확인
        all_voted = all(p.id in game.votes for p in game.alive_players)
        if all_voted:
            break
        await asyncio.sleep(0.5)

    # 투표 시간 종료 시, 투표하지 않은 플레이어는 랜덤으로 투표 처리
    alive_ids = [p.id for p in game.alive_players]
    for p in game.alive_players:
        if p.id not in game.votes:
            # 본인 제외 랜덤 투표
            others = [oid for oid in alive_ids if oid != p.id]
            if others:
                game.votes[p.id] = random.choice(others)

    # AI 투표 태스크가 아직 안 끝났다면 대기
    if ai_tasks:
        await asyncio.gather(*ai_tasks, return_exceptions=True)

    # 투표 집계
    vote_counts = {p.id: 0 for p in game.alive_players}
    for voter_id, voted_id in game.votes.items():
        if voted_id in vote_counts:
            vote_counts[voted_id] += 1

    # 가장 표를 많이 받은 사람 찾기
    max_votes = max(vote_counts.values()) if vote_counts else 0
    tied_players = [pid for pid, count in vote_counts.items() if count == max_votes]

    if not tied_players:
        # 뭔가 잘못된 경우 랜덤
        eliminated_id = random.choice(alive_ids)
    elif len(tied_players) == 1:
        eliminated_id = tied_players[0]
    else:
        # 동점일 경우 랜덤으로 한 명 탈락
        eliminated_id = random.choice(tied_players)

    # 탈락 처리
    eliminated = next(
        (p for p in game.alive_players if p.id == eliminated_id),
        None,
    )
    if eliminated:
        eliminated.is_eliminated = True
        logger.info(
            f"[game_logic] 투표 탈락: {eliminated.id} "
            f"(is_human={eliminated.is_human}, votes={max_votes})"
        )

    # 투표 결과 브로드캐스트
    await connection_manager.broadcast_to_game(
        game,
        WsMessage(
            type=WsMessageType.VOTE_RESULT,
            data={
                "eliminated_player_id": eliminated_id,
                "vote_counts": vote_counts,
                "was_human": eliminated.is_human if eliminated else None,
            },
        ),
    )


async def _ai_vote_task(game: GameState, ai: AIPlayer) -> None:
    """AI가 투표 대상을 정하고 GameState에 기록하는 헬퍼 함수"""
    voted_id = await ai.generate_vote(
        alive_players=game.alive_players,
        chat_history=game.chat_history,
        experience_submissions=game.experience_submissions
    )
    if voted_id:
        game.votes[ai.player.id] = voted_id


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
    return [
        Player(is_human=False)
        for _ in range(count)
    ]
