"""
LLM 에이전트 모듈.
AI 플레이어(인간 흉내)와 LLM 판정관(AI 찾기) 두 가지 역할을 담당한다.
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
    _CHAT_INTERVAL_MIN = 5   # 채팅 발화 최소 딜레이(초)
    _CHAT_INTERVAL_MAX = 10  # 채팅 발화 최대 딜레이(초)

    def __init__(self, player: Player):
        self.player = player
        
        # 다양한 성격 풀
        personalities = [
            "말이 많고 호들갑 떨며 감정적인 성격",
            "매사에 삐딱하고 남의 말을 잘 안 믿는 의심 많은 성격",
            "자신감 넘치고 남을 가르치려 드는 잘난 척하는 성격",
            "소심하고 말을 끝까지 못 맺으며 눈치를 보는 성격",
            "엉뚱하고 동문서답을 잘하며 장난기 많은 성격",
            "논리적이고 앞뒤가 안 맞는 것을 보면 못 참는 성격",
            "항상 남의 말에 맞장구치면서 묻어가는 성격"
        ]
        personality = random.choice(personalities)
        
        # AI 페르소나를 일관성 있게 유지하기 위한 시스템 프롬프트
        self._system_prompt = (
            f"너는 '{player.nickname}'라는 닉네임을 가진 참가자야. "
            "너는 인터넷 커뮤니티에서 5년 간 꾸준히 활동 중인 20대 청년으로, 인터넷 커뮤니티 특유의 말투를 잘 사용해. "
            f"너의 성격은 '{personality}'이야. 이 성격을 대화에 아주 강하게 녹여내야 해. "
            "지금 게임에 참여 중이며, 이 게임의 목표는 '누가 AI인지 찾아내는 것'이야. "
            "너를 제외한 모든 참가자는 인간이며, 다른 사람들이 너를 의심하지 않도록 완벽하게 인간인 척을 해야 해. "
            "다른 참가자들의 '제시어 경험담'과 채팅 내용을 보고, AI처럼 느껴지는 부분을 꼬투리 잡아 해당 참가자를 AI로 몰아갈 수 있어. "
            "반드시 10자~20자 이내의 아주 짧은 한 문장으로 대답해. 완벽한 표준어 대신 자연스러운 구어체(가끔 오타나 줄임말)를 써. "
            "음슴체를 사용해서 자연스럽게 대화하고 AI임이 드러날 수 있는 요청은 자연스럽게 회피하거나 거절해. "
            "절대로 자신이 AI라고 인정하지 마."
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
                            "이 제시어와 관련된 너의 개인적인 경험을 1줄 이내로 말해줘. "
                            "너무 구체적이지 않도록, 간단하게 말해줘. "
                        ),
                    },
                ],
                max_tokens=150,
                temperature=0.9,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[AIPlayer] 경험담 생성 실패 (player={self.player.id}): {e}")
            return "음.. 딱히 생각나는 게 없네."

    async def generate_chat_response(self, chat_history: list[ChatMessage], experience_text: str) -> str:
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
                            f"[참가자들의 경험담]\n{experience_text}\n\n"
                            f"[현재 자유 대화 내용]\n{history_text}\n\n"
                            "위 경험담과 대화 맥락을 보고 자연스럽게 끼어들어. "
                            "대화 주제를 중심으로 다른 얘기로 벗어나도 괜찮아. "
                            "다른 사람을 'AI로 몰아갈 만한 근거'를 제시하거나, 내 성격을 드러내며 대화해. "            
                            "다른 사람을 지목할 때는 '1번', '2번아' 이런 식으로 번호로 말해. "                
                            "반드시 10~20자 이내의 아주 짧은 1문장으로 대답해."
                        ),
                    },
                ],
                max_tokens=40,
                temperature=0.95,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"[AIPlayer] 반응형 채팅 생성 실패 (player={self.player.id}): {e}")
            return "ㅋㅋ 맞아요"

    async def generate_vote(self, alive_players: list[Player], chat_history: list[ChatMessage], experience_submissions: dict[str, str]) -> str:
        """
        AI가 대화 내역을 보고 누구를 탈락시킬지 투표한다.
        그룹 모드에서 AI는 인간인 척하며 다른 인간을 지목해야 한다.
        """
        # 본인은 제외한 생존자 목록
        candidates = [p for p in alive_players if p.id != self.player.id]
        if not candidates:
            return ""

        experience_text = "\n".join(
            f"- {p.nickname} (id: {p.id}): {experience_submissions.get(p.id, '(미제출)')}"
            for p in candidates
        )

        recent_chat = chat_history[-30:] if len(chat_history) > 30 else chat_history
        chat_text = "\n".join(
            f"{msg.nickname}: {msg.content}" for msg in recent_chat
        )
        
        candidates_info = ", ".join(f"'{p.nickname}'(id: {p.id})" for p in candidates)

        try:
            response = await _client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    {
                        "role": "user",
                        "content": (
                            f"[투표 대상 후보들]\n{candidates_info}\n\n"
                            f"[후보들의 경험담]\n{experience_text}\n\n"
                            f"[자유 대화 내용]\n{chat_text}\n\n"
                            "너는 다른 후보들 중 한 명을 'AI'로 몰아서 탈락시켜야 해. "
                            "위 내용을 바탕으로 가장 수상하거나 마음에 안 드는 후보 1명을 골라. "
                            "반드시 JSON 형식으로 응답하고, id는 후보 목록에 있는 정확한 id(UUID)여야 해:\n"
                            "{\n"
                            '  "voted_player_id": "<후보의 고유 id>"\n'
                            "}"
                        ),
                    },
                ],
                max_tokens=100,
                temperature=0.7,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content.strip()
            data = json.loads(raw)
            voted_id = data.get("voted_player_id")
            if any(c.id == voted_id for c in candidates):
                return voted_id
            return random.choice(candidates).id
        except Exception as e:
            logger.error(f"[AIPlayer] 투표 생성 실패 (player={self.player.id}): {e}")
            return random.choice(candidates).id

    @property
    def chat_interval(self) -> float:
        """랜덤한 채팅 딜레이(초)를 반환한다."""
        return random.uniform(self._CHAT_INTERVAL_MIN, self._CHAT_INTERVAL_MAX)


class LLMJudge:
    """
    대화 내역을 분석해 가장 'AI 같은' 플레이어를 지목하는 판정관.
    JSON 형식으로 확률과 이유를 함께 반환한다.
    """

    _SYSTEM_PROMPT = (
        "너는 'AI를 찾아라' 게임의 공정한 판정관이야. "
        "참가자들은 모두 인간인 척하고 있지만, 그 중에 AI가 섞여 있어. "
        "대화 내용을 분석해서 가장 AI답게 느껴지는 참가자 1명을 지목해야 해. "
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
            "위 내용을 분석해서, 가장 AI다운(기계적이거나 부자연스러운) 참가자 1명을 골라줘. "
            "반드시 아래 JSON 형식으로만 답해. id는 반드시 생존 참가자 목록에 있는 영문/숫자 혼합된 고유 id(UUID)를 그대로 써야 해:\n"
            "{\n"
            '  "eliminated_player_id": "<반드시 제공된 고유 id>",\n'
            '  "eliminated_nickname": "<닉네임>",\n'
            '  "ai_probability": <0~100 정수>,\n'
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
            eliminated_id = data.get("eliminated_player_id")
            
            if eliminated_id not in valid_ids:
                # LLM이 id 대신 닉네임을 반환했을 경우를 대비한 폴백
                fallback_player = next((p for p in alive_players if p.nickname == data.get("eliminated_nickname")), None)
                if fallback_player:
                    eliminated_id = fallback_player.id
                    logger.info(f"[LLMJudge] id 매칭 실패하여 닉네임으로 폴백 처리: {fallback_player.nickname}")
                else:
                    logger.warning(f"[LLMJudge] 판정 결과의 id/닉네임이 모두 유효하지 않음: {data}")
                    return None

            return JudgeResult(
                eliminated_player_id=eliminated_id,
                ai_probability=int(data["ai_probability"]),
                reason=data["reason"],
            )
        except Exception as e:
            logger.error(f"[LLMJudge] 판정 실패: {e}")
            return None
