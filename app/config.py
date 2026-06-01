"""
환경변수 기반 설정 모듈.
os.getenv 직접 호출 대신 이 모듈을 통해 설정값을 가져온다.
"""
import os
from dotenv import load_dotenv

load_dotenv()  # .env 파일이 있으면 로드 (docker run -e 우선)


class Settings:
    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # 단체 모드 최소 인원 (기본: 4)
    GROUP_MIN_PLAYERS: int = int(os.getenv("GROUP_MIN_PLAYERS", "4"))

    # 자유 대화 시간(초) (기본: 120)
    FREE_CHAT_DURATION: int = int(os.getenv("FREE_CHAT_DURATION", "120"))

    # 경험 공유 1인당 타임아웃(초) (기본: 30)
    EXPERIENCE_TIMEOUT: int = int(os.getenv("EXPERIENCE_TIMEOUT", "30"))

    # 솔로 모드 AI 인원 (기본: 4)
    SOLO_AI_COUNT: int = int(os.getenv("SOLO_AI_COUNT", "4"))

    # 단체 모드 AI 인원 (기본: 1)
    GROUP_AI_COUNT: int = int(os.getenv("GROUP_AI_COUNT", "1"))


settings = Settings()
