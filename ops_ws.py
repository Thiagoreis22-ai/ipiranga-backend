"""WebSocket autenticado para atualizações de telemetria, SOMENTE LEITURA.

Não conecta a rede de PLCs, não escreve comandos, não fabrica valores.
A frequência deste canal WEB não equivale a controle em tempo real de PLC.
"""
import asyncio
import os
from datetime import datetime, timezone

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect


def build_ops_ws_router(db, jwt_secret):
    router = APIRouter()

    @router.websocket("/api/ops/ws")
    async def industrial_stream(ws: WebSocket):
        origin = ws.headers.get("origin")
        approved_origins = set(
            item.strip() for item in os.environ.get(
                "CORS_ORIGINS", "http://localhost:3000"
            ).split(",") if item.strip() and item.strip() != "*"
        )
        # WebSocket não é protegido automaticamente pelo middleware CORS.
        if not origin or origin not in approved_origins:
            await ws.close(code=1008, reason="Origin não autorizada")
            return
        await ws.accept()
        try:
            login = await asyncio.wait_for(ws.receive_json(), timeout=8)
            token = login.get("token", "") if isinstance(login, dict) else ""
            try:
                claims = jwt.decode(token, jwt_secret, algorithms=["HS256"])
                user = await db.users.find_one({
                    "id": claims.get("user_id"), "active": True
                }, {"_id": 0, "id": 1})
                if not user:
                    await ws.close(code=1008, reason="Usuário inativo ou inexistente")
                    return
            except (jwt.PyJWTError, TypeError):
                await ws.close(code=1008, reason="Sessão inválida")
                return
            while True:
                # Revalidar sessão e permissão mesmo para conexões duradouras.
                try:
                    claims = jwt.decode(token, jwt_secret, algorithms=["HS256"])
                except jwt.PyJWTError:
                    await ws.close(code=1008, reason="Sessão expirada")
                    return
                if not await db.users.find_one({"id": claims["user_id"], "active": True}, {"_id": 1}):
                    await ws.close(code=1008, reason="Acesso revogado")
                    return
                tags = await db.industrial_tags.find(
                    {"enabled": True}, {"_id": 0, "id": 1}
                ).limit(300).to_list(300)
                ids = [tag["id"] for tag in tags]
                values = {}
                if ids:
                    pipeline = [
                        {"$match": {"tag_id": {"$in": ids}}},
                        {"$sort": {"observed_at": -1}},
                        {"$group": {"_id": "$tag_id", "reading": {"$first": "$$ROOT"}}}},
                        {"$project": {"_id": 0, "reading": 1}}
                    ]
                    rows = await db.industrial_samples.aggregate(pipeline).to_list(300)
                    now = datetime.now(timezone.utc)
                    for row in rows:
                        reading = row["reading"]
                        reading.pop("_id", None)
                        try:
                            dt = datetime.fromisoformat(reading["observed_at"].replace("Z", "+00:00"))
                            age = (now-dt.astimezone(timezone.utc)).total_seconds()
                        except (KeyError, ValueError, TypeError):
                            age = float("inf")
                        valid = 0 <= age <= 120 and reading.get("quality") == "GOOD"
                        reading["valid_now"] = valid
                        reading["freshness"] = "FRESH" if valid else "STALE_OR_INVALID"
                        values[reading["tag_id"]] = reading
                await ws.send_json({
                    "type": "industrial_telemetry",
                    "readings": values,
                    "observed_server_time": datetime.now(timezone.utc).isoformat(),
                    "control_enabled": False,
                    "source": "verified_gateway_database",
                    "scope_limit": 300
                })
                await asyncio.sleep(5)
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, RuntimeError):
            try:
                await ws.close()
            except RuntimeError:
                pass

    return router
