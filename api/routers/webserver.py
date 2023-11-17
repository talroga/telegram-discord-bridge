"""Bridge controller router."""
import asyncio
from typing import List

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.models import (BridgeResponse, BridgeResponseSchema, Health,
                        HealthHistory, HealtHistoryManager, HealthSchema)
from api.routers.health import HealthcheckSubscriber, WSConnectionManager
from bridge.config import Config
from bridge.enums import ProcessStateEnum
from bridge.events import EventDispatcher
from bridge.logger import Logger
from bridge.telegram_handler import check_telegram_session
from forwarder import determine_process_state, run_controller

# from typing import List



logger = Logger.get_logger(Config.get_config_instance().app.name)


class BridgeRouter:  # pylint: disable=too-few-public-methods
    """Bridge Router."""

    def __init__(self):
        """Initialize the Bridge Router."""
        self.dispatcher: EventDispatcher
        HealtHistoryManager.register('HealthHistory', HealthHistory)

        self.health_history_manager_instance = HealtHistoryManager()
        self.health_history_manager_instance.start() # pylint: disable=consider-using-with # the server must stay alive as long as we want the shared object to be accessible
        self.health_history: HealthHistory = self.health_history_manager_instance.HealthHistory() # type: ignore # pylint: disable=no-member

        self.ws_connection_manager: WSConnectionManager

        self.bridge_router = APIRouter(
            prefix="/bridge",
            tags=["bridge"],
        )

        self.bridge_router.post("/",
                         name="Start the Telegram to Discord Bridge",
                         summary="Initiate the forwarding.",
                         description="Starts the Bridge controller triggering the Telegram authentication process.",
                         response_model=BridgeResponseSchema)(self.start)

        self.bridge_router.delete("/",
                           name="Stop the Telegram to Discord Bridge",
                           summary="Removes the Bridge process.",
                           description="Suspends the Bridge forwarding messages from Telegram to Discord and stops the process.",
                           response_model=BridgeResponseSchema)(self.stop)

        self.bridge_router.get("/health",
                        name="Get the health status of the Bridge.",
                        summary="Determines the Bridge process status, the Telegram, Discord, and OpenAI connections health and returns a summary.",
                        description="Determines the Bridge process status, and the Telegram, Discord, and OpenAI connections health.",
                        response_model=HealthSchema)(self.health)

        self.bridge_router.websocket("/health/ws",
                                name="Get the health status of the Bridge.")(self.health_websocket_endpoint)
    
    async def index(self):
        

router = BridgeRouter().bridge_router
