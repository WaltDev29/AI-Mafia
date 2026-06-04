"""
데이터 모델 정의 모듈.
게임 상태, 플레이어, 메시지, 판정 결과 등의 핵심 구조체를 Pydantic과 Enum으로 선언한다.
"""
import uuid
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field


class GameMode(str, Enum):
    """게임 모드: 솔로(인간1 vs AI4) / 단체(인간4 vs AI1)"""
    SOLO = "solo"    # 인간 1 + AI 4
    GROUP = "group"  # 인간 4 + AI 1


class GamePhase(str, Enum):
    """게임 진행 단계"""
    WAITING = "waiting"                      # 플레이어 대기 중
    ROUND_START = "round_start"              # 라운드 시작 / 제시어 공개
    EXPERIENCE_SHARING = "experience_sharing"# 경험 공유 단계
    FREE_CHAT = "free_chat"                  # 자유 대화 단계
    JUDGING = "judging"                      # LLM 판정 단계 (솔로 모드)
    VOTING = "voting"                        # 투표 단계 (그룹 모드)
    GAME_OVER = "game_over"                  # 게임 종료


class Player(BaseModel):
    """플레이어 정보. ws 필드는 직렬화 대상이 아니므로 model_config로 arbitrary 허용."""
    model_config = {"arbitrary_types_allowed": True}

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    nickname: str = ""
    is_human: bool
    is_eliminated: bool = False
    # WebSocket 객체는 직렬화하지 않음 (게임 로직에서만 참조)
    ws: Optional[Any] = Field(default=None, exclude=True)


class ChatMessage(BaseModel):
    """채팅 메시지"""
    player_id: str
    nickname: str = ""
    content: str
    timestamp: float  # Unix timestamp


class JudgeResult(BaseModel):
    """LLM 판정 결과"""
    eliminated_player_id: str
    human_probability: int  # 사람이라고 판단한 확률 (0~100)
    reason: str


class GameResult(str, Enum):
    """게임 결과"""
    HUMAN_WIN = "human_win"
    AI_WIN = "ai_win"


# ──────────────────────────────────────────────
# WebSocket 메시지 페이로드 스키마
# 클라이언트 ↔ 서버 간 주고받는 모든 메시지는 이 형식을 따른다.
# ──────────────────────────────────────────────

class WsMessageType(str, Enum):
    """WebSocket 메시지 타입"""
    # 서버 → 클라이언트
    CONNECTED = "connected"                 # 연결 성공 및 본인 id 전달
    GAME_START = "game_start"               # 게임 시작 (플레이어 목록 전달)
    ROUND_START = "round_start"             # 라운드 시작 + 제시어
    EXPERIENCE_REQUEST = "experience_request"  # 경험 공유 요청 (현재 순서 플레이어에게)
    EXPERIENCE_SUBMITTED = "experience_submitted"  # 누군가 경험담 제출 완료
    FREE_CHAT_START = "free_chat_start"     # 자유 대화 시작
    FREE_CHAT_END = "free_chat_end"         # 자유 대화 종료
    CHAT_MESSAGE = "chat_message"           # 채팅 메시지 브로드캐스트
    JUDGING_START = "judging_start"         # 판정 시작 알림
    JUDGE_RESULT = "judge_result"           # 판정 결과
    VOTING_START = "voting_start"           # 투표 시작 알림
    VOTE_RESULT = "vote_result"             # 투표 결과
    PLAYER_ELIMINATED = "player_eliminated" # 플레이어 탈락
    GAME_OVER = "game_over"                 # 게임 종료
    WAITING_ROOM = "waiting_room"           # 대기방 상태 업데이트
    ERROR = "error"                         # 에러 메시지
    EXPERIENCE_TIMEOUT = "experience_timeout"  # 경험 공유 타임아웃

    # 클라이언트 → 서버
    SUBMIT_EXPERIENCE = "submit_experience" # 경험담 제출
    SEND_CHAT = "send_chat"                 # 채팅 전송
    TYPING_STATUS = "typing_status"         # 타이핑 상태 브로드캐스트/수신
    SUBMIT_VOTE = "submit_vote"             # 투표 제출


class WsMessage(BaseModel):
    """WebSocket 메시지 공통 포맷"""
    type: WsMessageType
    data: Optional[dict] = None
