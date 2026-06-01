"""
LLM 에이전트 모듈.
AI 플레이어(인간 흉내)와 LLM 판정관(사람 찾기) 두 가지 역할을 담당한다.
모델: gpt-4o-mini
"""
import json
import logging
import os
import random
from typing import Optional

from openai import AsyncOpenAI

from .models import ChatMessage, JudgeResult, Player

logger = logging.getLogger(__name__)

# gpt-4o-mini 고정 (사용자 결정 사항)
_MODEL = "gpt-4o-mini"
_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class AIPlayer:
    """
    인간인 척 연기하는 AI 플레이어.
    각 인스턴스가 독립적인 페르소나(닉네임, 성격)를 유지한다.
    """

    # 자유 대화 전략 상수
    _REACTION_DELAY_MIN = 3   # 반응형 최소 딜레이(초)
    _REACTION_DELAY_MAX = 8   # 반응형 최대 딜레이(초)
    _SPONTANEOUS_IDLE_SEC = 30  # 선제 발화 트리거: 이 초 이상 침묵

    def __init__(self, player: Player):
        self.player = player
        # AI 페르소나를 일관성 있게 유지하기 위한 시스템 프롬프트
        self._system_prompt = (
            f"너는 '{player.nickname}'라는 닉네임을 가진 사람이야. "
            "지금 'AI를 찾아라' 게임에 참여 중이야. "
            "이 게임에서 모든 참가자는 AI인 척해야 해. "
            "너는 실제로 AI지만, 다른 참가자들이 너를 AI라고 눈치채지 못하도록 "
            "최대한 인간처럼 자연스럽게 행동해야 해. "
            "짧고 구어체로 대화하고, 가끔 오타나 말줄임표를 써도 좋아. "
            "절대로 자신이 AI임을 드러내지 마."
        )

    async def generate_experience(self, prompt_word: str) -> str:
        """
        제시어에 대한 경험담을 2줄 이내로 생성한다.
        인간처럼 구체적이고 감정이 담긴 짧은 일화를 만들어낸다.
        """
        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"제시어: '{prompt_word}'\n"
                            "이 제시어와 관련된 너의 개인적인 경험을 2줄 이내로 말해줘. "
                            "구체적이고 감정이 느껴지게, 너무 완벽하지 않게 자연스럽게."
                        ),
                    },
                ],
                max_tokens=150,
                temperature=0.9,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[AIPlayer] 경험담 생성 실패 (player={self.player.id}): {e}")
            return "음.. 딱히 생각나는 게 없네요."

    async def generate_chat_response(self, chat_history: list[ChatMessage]) -> str:
        """
        최근 대화 흐름을 보고 자연스러운 반응형 채팅을 생성한다.
        최근 10개 메시지만 컨텍스트로 사용해 토큰을 절약한다.
        """
        recent = chat_history[-10:] if len(chat_history) > 10 else chat_history
        history_text = "\n".join(
            f"{msg.nickname}: {msg.content}" for msg in recent
        )
        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"현재 자유 대화 내용:\n{history_text}\n\n"
                            "위 대화에 자연스럽게 끼어들어 한 마디 해줘. "
                            "1~2문장, 구어체로. 대화 맥락에 맞게."
                        ),
                    },
                ],
                max_tokens=100,
                temperature=0.95,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[AIPlayer] 반응형 채팅 생성 실패 (player={self.player.id}): {e}")
            return "ㅋㅋ 맞아요"

    async def generate_spontaneous_message(self, chat_history: list[ChatMessage]) -> str:
        """
        대화가 멈췄을 때 선제적으로 발화할 메시지를 생성한다.
        """
        recent_context = ""
        if chat_history:
            last_few = chat_history[-5:] if len(chat_history) >= 5 else chat_history
            recent_context = "\n".join(f"{m.nickname}: {m.content}" for m in last_few)

        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"최근 대화:\n{recent_context or '(대화 없음)'}\n\n"
                            "대화가 잠시 멈췄어. 자연스럽게 먼저 말을 걸어봐. "
                            "1문장, 가볍게."
                        ),
                    },
                ],
                max_tokens=80,
                temperature=1.0,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[AIPlayer] 선제 발화 생성 실패 (player={self.player.id}): {e}")
            return "근데 다들 조용하네요?"

    @property
    def reaction_delay(self) -> float:
        """반응형 채팅 시 랜덤 딜레이(초)를 반환한다."""
        return random.uniform(self._REACTION_DELAY_MIN, self._REACTION_DELAY_MAX)


class LLMJudge:
    """
    대화 내역을 분석해 가장 '인간 같은' 플레이어를 지목하는 판정관.
    JSON 형식으로 확률과 이유를 함께 반환한다.
    """

    _SYSTEM_PROMPT = (
        "너는 'AI를 찾아라' 게임의 공정한 판정관이야. "
        "참가자들은 모두 AI인 척하고 있지만, 그 중에 진짜 사람이 섞여 있어. "
        "대화 내용을 분석해서 가장 인간답게 느껴지는 참가자 1명을 지목해야 해. "
        "감정 표현, 구체적인 경험, 자연스러운 맥락 파악, 오타/말버릇 등을 종합적으로 판단해."
    )

    async def judge(
        self,
        alive_players: list[Player],
        chat_history: list[ChatMessage],
        experience_submissions: dict[str, str],  # player_id → 경험담
    ) -> Optional[JudgeResult]:
        """
        전체 대화 + 경험담을 바탕으로 탈락자 1명을 결정한다.
        실패 시 None 반환 (호출부에서 랜덤 탈락 처리).
        """
        # 경험담 텍스트 구성
        experience_text = "\n".join(
            f"- {p.nickname}: {experience_submissions.get(p.id, '(미제출)')}"
            for p in alive_players
        )

        # 채팅 내역 구성 (최대 50개)
        recent_chat = chat_history[-50:] if len(chat_history) > 50 else chat_history
        chat_text = "\n".join(
            f"{msg.nickname}: {msg.content}" for msg in recent_chat
        )

        player_list = ", ".join(f"'{p.nickname}'(id:{p.id})" for p in alive_players)

        user_prompt = (
            f"생존 참가자 목록: {player_list}\n\n"
            f"[경험 공유]\n{experience_text}\n\n"
            f"[자유 대화]\n{chat_text}\n\n"
            "위 내용을 분석해서, 가장 인간다운 참가자 1명을 골라줘. "
            "반드시 아래 JSON 형식으로만 답해:\n"
            "{\n"
            '  "eliminated_player_id": "<id>",\n'
            '  "eliminated_nickname": "<닉네임>",\n'
            '  "human_probability": <0~100 정수>,\n'
            '  "reason": "<판단 이유 2~4문장>"\n'
            "}"
        )

        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": self._SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=300,
                temperature=0.3,  # 판정은 일관성 중시
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            data = json.loads(raw)

            # 반환된 id가 실제 생존 플레이어인지 검증
            valid_ids = {p.id for p in alive_players}
            if data.get("eliminated_player_id") not in valid_ids:
                logger.warning(f"[LLMJudge] 판정 결과의 id가 유효하지 않음: {data}")
                return None

            return JudgeResult(
                eliminated_player_id=data["eliminated_player_id"],
                eliminated_nickname=data["eliminated_nickname"],
                human_probability=int(data["human_probability"]),
                reason=data["reason"],
            )
        except Exception as e:
            logger.error(f"[LLMJudge] 판정 실패: {e}")
            return None
