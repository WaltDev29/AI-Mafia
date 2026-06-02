"""
게임 상태 관리 모듈 (싱글톤).
진행 중인 모든 게임 인스턴스와 단체 모드 대기방을 관리한다.
"""
import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .models import GameMode, GamePhase, Player, ChatMessage

logger = logging.getLogger(__name__)


@dataclass
class GameState:
    """진행 중인 게임 하나의 전체 상태"""
    game_id: str
    mode: GameMode
    players: list[Player]
    phase: GamePhase = GamePhase.WAITING
    round_number: int = 0

    # 현재 라운드 데이터
    current_prompt_word: str = ""
    # 경험 공유: player_id → 제출한 경험담
    experience_submissions: dict = field(default_factory=dict)
    # 경험 공유 순서 (생존 플레이어 id 리스트)
    experience_order: list = field(default_factory=list)
    # 현재 경험 공유 차례 인덱스
    experience_index: int = 0
    # 채팅 히스토리 (자유 대화 포함)
    chat_history: list[ChatMessage] = field(default_factory=list)

    @property
    def alive_players(self) -> list[Player]:
        return [p for p in self.players if not p.is_eliminated]

    @property
    def alive_humans(self) -> list[Player]:
        return [p for p in self.alive_players if p.is_human]

    @property
    def alive_ais(self) -> list[Player]:
        return [p for p in self.alive_players if not p.is_human]


class GameManager:
    """
    싱글톤 게임 매니저.
    동시에 여러 게임을 관리하며, 단체 모드 대기방도 여기서 처리한다.
    """
    _instance: Optional["GameManager"] = None

    def __new__(cls) -> "GameManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        # 중복 초기화 방지
        if self._initialized:
            return
        self._initialized = True

        # game_id → GameState
        self.active_games: dict[str, GameState] = {}
        # 단체 모드 대기방: 아직 게임이 배정되지 않은 인간 플레이어 목록
        self.group_waiting_room: list[Player] = []

    def create_game(self, players: list[Player], mode: GameMode) -> GameState:
        """새 게임 인스턴스를 생성하고 등록한다."""
        game_id = str(uuid.uuid4())
        game = GameState(game_id=game_id, mode=mode, players=players)
        self.active_games[game_id] = game
        logger.info(
            f"[GameManager] 게임 생성: id={game_id}, mode={mode}, "
            f"players={len(players)}명"
        )
        return game

    def remove_game(self, game_id: str) -> None:
        """게임 종료 후 메모리에서 제거한다."""
        if game_id in self.active_games:
            del self.active_games[game_id]
            logger.info(f"[GameManager] 게임 제거: id={game_id}")

    def get_game(self, game_id: str) -> Optional[GameState]:
        return self.active_games.get(game_id)

    def find_game_by_player(self, player_id: str) -> Optional[GameState]:
        """플레이어 id로 해당 플레이어가 속한 게임을 찾는다."""
        for game in self.active_games.values():
            if any(p.id == player_id for p in game.players):
                return game
        return None

    def add_to_group_waiting(self, player: Player) -> list[Player]:
        """
        단체 모드 대기방에 플레이어를 추가한다.
        4명이 모이면 해당 4명을 반환한다 (게임 시작 신호).
        미달이면 빈 리스트 반환.
        """
        from .config import settings
        self.group_waiting_room.append(player)
        logger.info(
            f"[GameManager] 단체 대기방: {player.id} 추가 "
            f"({len(self.group_waiting_room)}/{settings.GROUP_MIN_PLAYERS}명)"
        )
        if len(self.group_waiting_room) >= settings.GROUP_MIN_PLAYERS:
            ready_players = self.group_waiting_room[:settings.GROUP_MIN_PLAYERS]
            self.group_waiting_room = self.group_waiting_room[settings.GROUP_MIN_PLAYERS:]
            return ready_players
        return []

    def remove_from_waiting(self, player_id: str) -> None:
        """대기 중 연결이 끊긴 플레이어를 대기방에서 제거한다."""
        self.group_waiting_room = [
            p for p in self.group_waiting_room if p.id != player_id
        ]

    @property
    def status(self) -> dict:
        """현재 서버 상태 요약 (헬스체크용)"""
        return {
            "active_games": len(self.active_games),
            "group_waiting": len(self.group_waiting_room),
        }


# 모듈 레벨 싱글톤 인스턴스
game_manager = GameManager()
