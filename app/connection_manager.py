"""
WebSocket 연결 관리 모듈.
플레이어별 WebSocket 연결을 추적하고 게임별 브로드캐스트를 담당한다.
"""
import json
import logging
from typing import Optional

from fastapi import WebSocket

from .models import WsMessage, WsMessageType

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    모든 WebSocket 연결을 중앙에서 관리한다.
    player_id → WebSocket 매핑을 유지하며,
    game_id 기준으로 대상을 필터링한 브로드캐스트를 지원한다.
    """

    def __init__(self):
        # player_id → WebSocket
        self._connections: dict[str, WebSocket] = {}

    async def connect(self, player_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[player_id] = ws
        logger.info(f"[ConnectionManager] 연결: player_id={player_id}")

    def disconnect(self, player_id: str) -> None:
        self._connections.pop(player_id, None)
        logger.info(f"[ConnectionManager] 해제: player_id={player_id}")

    async def send(self, player_id: str, message: WsMessage) -> None:
        """특정 플레이어에게 메시지를 전송한다."""
        ws = self._connections.get(player_id)
        if ws is None:
            return
        try:
            await ws.send_text(message.model_dump_json())
        except Exception as e:
            logger.warning(f"[ConnectionManager] 전송 실패 (player={player_id}): {e}")

    async def broadcast(self, player_ids: list[str], message: WsMessage) -> None:
        """지정된 플레이어 목록에게 동일한 메시지를 브로드캐스트한다."""
        payload = message.model_dump_json()
        for pid in player_ids:
            ws = self._connections.get(pid)
            if ws is None:
                continue
            try:
                await ws.send_text(payload)
            except Exception as e:
                logger.warning(f"[ConnectionManager] 브로드캐스트 실패 (player={pid}): {e}")

    async def broadcast_to_game(self, game, message: WsMessage) -> None:
        """
        게임에 속한 모든 인간 플레이어에게 메시지를 전송한다.
        (AI 플레이어는 WebSocket 연결이 없으므로 제외)
        """
        human_ids = [p.id for p in game.players if p.is_human]
        await self.broadcast(human_ids, message)

    async def send_error(self, player_id: str, detail: str) -> None:
        await self.send(
            player_id,
            WsMessage(type=WsMessageType.ERROR, data={"detail": detail}),
        )

    def is_connected(self, player_id: str) -> bool:
        return player_id in self._connections


# 모듈 레벨 싱글톤 인스턴스
connection_manager = ConnectionManager()
