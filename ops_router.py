"""Módulos industriais WEB: dados reais, histórico, alarmes, engenharia e modos pretendidos.

Nenhum endpoint deste módulo comunica diretamente com CLPs ou escreve setpoints.
Gateway de entrada autenticado; controle físico permanentemente desativado.
"""
import os
import uuid
import hmac
import math
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field, field_validator


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def parse_time(value: str) -> datetime:
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("timezone requerida")
        return date.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Data ISO-8601 com fuso horário obrigatória")


def text_code(value: str) -> str:
    code = value.strip().upper()
    if not 2 <= len(code) <= 64 or not all(c.isascii() and (c.isalnum() or c in "_-.") for c in code):
        raise ValueError("Código deve ter 2 a 64 caracteres ASCII: letras, números, _, - ou .")
    return code


class AreaIn(BaseModel):
    code: str
    name: str = Field(min_length=2, max_length=140)
    description: str = Field(default="", max_length=800)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value):
        return text_code(value)


class EquipmentIn(BaseModel):
    code: str
    name: str = Field(min_length=2, max_length=140)
    area_id: str
    category: Literal["TANQUE", "BOMBA", "VALVULA", "DECANTADOR", "TROCADOR", "FILTRO", "TURBINA", "OUTRO"] = "OUTRO"
    description: str = Field(default="", max_length=800)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value):
        return text_code(value)


class TagIn(BaseModel):
    code: str
    name: str = Field(min_length=2, max_length=140)
    area_id: str
    equipment_id: Optional[str] = None
    kind: Literal["NIVEL", "VAZAO", "TEMPERATURA", "PH", "BRIX", "PRESSAO", "DENSIDADE", "POSICAO", "ENERGIA", "OUTRO"] = "OUTRO"
    unit: str = Field(default="", max_length=30)
    warning_low: Optional[float] = None
    warning_high: Optional[float] = None
    critical_low: Optional[float] = None
    critical_high: Optional[float] = None
    source_tag: str = Field(default="", max_length=180)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value):
        return text_code(value)

    @field_validator("warning_low", "warning_high", "critical_low", "critical_high")
    @classmethod
    def finite_bounds(cls, value):
        if value is not None and not math.isfinite(value):
            raise ValueError("Limites devem ser finitos")
        return value


class LoopIn(BaseModel):
    code: str
    name: str = Field(min_length=2, max_length=140)
    area_id: str
    pv_tag_id: str
    sp_tag_id: Optional[str] = None
    mv_tag_id: Optional[str] = None
    requested_mode: Literal["MANUAL", "SUPERVISORIO", "AUTOMATICO"] = "MANUAL"
    notes: str = Field(default="", max_length=1000)

    @field_validator("code")
    @classmethod
    def validate_code(cls, value):
        return text_code(value)


class ModeIn(BaseModel):
    requested_mode: Literal["MANUAL", "SUPERVISORIO", "AUTOMATICO"]
    justification: str = Field(min_length=10, max_length=1000)


class Reading(BaseModel):
    tag_id: str = Field(min_length=1, max_length=80)
    value: float
    observed_at: str
    quality: Literal["GOOD", "UNCERTAIN", "BAD"]

    @field_validator("value")
    @classmethod
    def finite_value(cls, value):
        if not math.isfinite(value):
            raise ValueError("Medição precisa ser finita")
        return value


class IngestIn(BaseModel):
    readings: List[Reading] = Field(min_length=1, max_length=100)


class AiRequest(BaseModel):
    tag_id: str


def build_ops_router(db, get_current_user):
    router = APIRouter(prefix="/api/ops", tags=["Operação Industrial"])

    async def actor(payload=Depends(get_current_user)):
        user = await db.users.find_one({"id": payload["user_id"]}, {"_id": 0, "password": 0})
        if not user or not user.get("active", False):
            raise HTTPException(status_code=401, detail="Sessão inválida ou usuário inativo")
        return user

    async def engineering(user=Depends(actor)):
        if user.get("role") not in ("admin", "supervisor"):
            raise HTTPException(status_code=403, detail="Acesso restrito à engenharia ou supervisão")
        return user

    async def admin(user=Depends(actor)):
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Apenas administrador pode alterar o cadastro industrial")
        return user

    async def audit(action: str, who: dict, entity: str, details: dict):
        await db.industrial_audit.insert_one({
            "id": str(uuid.uuid4()), "action": action, "actor_id": who.get("id"),
            "actor_name": who.get("name"), "entity": entity,
            "details": details, "timestamp": iso_now()
        })

    async def latest_values(tag_ids: list):
        if not tag_ids:
            return {}
        pipeline = [
            {"$match": {"tag_id": {"$in": tag_ids}}},
            {"$sort": {"observed_at": -1}},
            {"$group": {"_id": "$tag_id", "data": {"$first": "$$ROOT"}}},
            {"$project": {"_id": 0, "data._id": 0, "data": 1}}
        ]
        rows = await db.industrial_samples.aggregate(pipeline).to_list(len(tag_ids))
        return {row["data"]["tag_id"]: row["data"] for row in rows}

    def validate_sample(sample):
        seen = parse_time(sample["observed_at"])
        delta = (now_utc() - seen).total_seconds()
        quality = sample["quality"]
        if delta < -120 or delta > 600:
            return {**sample, "valid_now": False, "freshness": "STALE",
                    "status": "SEM DADO ATUAL"}
        return {**sample, "valid_now": quality == "GOOD",
                "freshness": "FRESH" if quality == "GOOD" else "INVALID_QUALITY",
                "status": "ATUAL" if quality == "GOOD" else "QUALIDADE INVÁLIDA"}

    @router.get("/status")
    async def status(user=Depends(actor)):
        key = os.environ.get("INDUSTRIAL_GATEWAY_KEY", "")
        last = await db.industrial_samples.find_one({}, {"_id": 0}, sort=[("observed_at", -1)])
        sample = validate_sample(last) if last else None
        ai_ready = bool(os.getenv("OPENAI_API_KEY") and os.getenv("AI_EXTERNAL_SHARING_APPROVED") == "true")
        return {
            "plant": "USINA IPIRANGA AGROINDUSTRIAL",
            "gateway_ingest_configured": len(key) >= 32,
            "last_reading_at": last["observed_at"] if last else None,
            "telemetry_state": "RECENT_REAL_DATA" if sample and sample["valid_now"] else "NO_CURRENT_DATA",
            "plc_connection_confirmed": False,
            "ai_analysis_configured": ai_ready,
            "hardware_write_enabled": False,
            "physical_control_enabled": False,
            "commissioning": "PENDENTE",
            "message": "Somente supervisão de dados. Controle físico desabilitado até comissionamento."
        }

    @router.get("/catalog")
    async def catalog(user=Depends(actor)):
        areas = await db.industrial_areas.find({}, {"_id": 0}).sort("name", 1).limit(300).to_list(300)
        equipments = await db.industrial_equipment.find({}, {"_id": 0}).sort("name", 1).limit(600).to_list(600)
        tags = await db.industrial_tags.find({}, {"_id": 0}).sort("code", 1).limit(1000).to_list(1000)
        loops = await db.industrial_loops.find({}, {"_id": 0}).sort("code", 1).limit(300).to_list(300)
        return {"areas": areas, "equipment": equipments, "tags": tags, "loops": loops,
                "limits": {"areas": 300, "equipment": 600, "tags": 1000, "loops": 300}}

    @router.get("/overview")
    async def overview(user=Depends(actor)):
        count_names = ("industrial_areas", "industrial_equipment", "industrial_tags", "industrial_loops")
        counts = {}
        for name in count_names:
            counts[name] = await db[name].count_documents({})
        alarm_counts = {"CRITICO": 0, "ATENCAO": 0}
        async for alarm in db.industrial_alarms.find({"state": "OPEN"}, {"_id": 0, "severity": 1}).limit(5000):
            severity = alarm.get("severity")
            if severity in alarm_counts:
                alarm_counts[severity] += 1
        registered_tags = await db.industrial_tags.find({}, {"_id": 0, "id": 1}).limit(1000).to_list(1000)
        recent = await latest_values([tag["id"] for tag in registered_tags])
        fresh_count = sum(1 for item in recent.values() if validate_sample(item)["valid_now"])
        quality_errors = sum(1 for item in recent.values() if not validate_sample(item)["valid_now"])
        return {"counts": counts, "open_alarms": alarm_counts,
                "tags_with_current_data": fresh_count, "tags_without_current_data": counts["industrial_tags"]-fresh_count,
                "tags_with_invalid_or_old_data": quality_errors,
                "updated_at": iso_now(), "hardware_write_enabled": False}

    @router.get("/telemetry")
    async def telemetry(user=Depends(actor)):
        tags = await db.industrial_tags.find({}, {"_id": 0, "id": 1}).limit(1000).to_list(1000)
        values = await latest_values([t["id"] for t in tags])
        result = {tag_id: validate_sample(sample) for tag_id, sample in values.items()}
        return {"readings": result, "count": len(result), "source": "authenticated_gateway"}

    @router.get("/trends/{tag_id}")
    async def trends(tag_id: str, hours: int = Query(default=24, ge=1, le=720), user=Depends(actor)):
        tag = await db.industrial_tags.find_one({"id": tag_id}, {"_id": 0})
        if not tag:
            raise HTTPException(status_code=404, detail="Tag não cadastrada")
        cutoff = (now_utc() - timedelta(hours=hours)).isoformat()
        docs = await db.industrial_samples.find(
            {"tag_id": tag_id, "observed_at": {"$gte": cutoff}},
            {"_id": 0, "id": 0, "received_at": 0}
        ).sort("observed_at", -1).limit(1501).to_list(1501)
        return {"tag": tag, "points": list(reversed(docs[:1500])),
                "truncated": len(docs) > 1500, "hours": hours}

    @router.get("/alarms")
    async def alarms(state: Optional[Literal["OPEN", "CLEARED"]] = None,
                     limit: int = Query(default=100, ge=1, le=500), user=Depends(actor)):
        query = {"state": state} if state else {}
        entries = await db.industrial_alarms.find(query, {"_id": 0}).sort("last_seen_at", -1).limit(limit).to_list(limit)
        return {"items": entries, "count": len(entries)}

    @router.post("/alarms/{alarm_id}/ack")
    async def acknowledge_alarm(alarm_id: str, user=Depends(engineering)):
        alarm = await db.industrial_alarms.find_one({"id": alarm_id}, {"_id": 0})
        if not alarm:
            raise HTTPException(status_code=404, detail="Alarme não encontrado")
        if alarm.get("acknowledged_at"):
            return {"status": "ALREADY_ACKNOWLEDGED"}
        moment = iso_now()
        await db.industrial_alarms.update_one(
            {"id": alarm_id, "acknowledged_at": None},
            {"$set": {"acknowledged_at": moment, "acknowledged_by": user["id"]}}
        )
        await audit("ALARM_ACK", user, alarm_id, {"severity": alarm.get("severity")})
        return {"status": "ACKNOWLEDGED"}

    @router.get("/audit")
    async def history(limit: int = Query(default=150, ge=1, le=500), user=Depends(engineering)):
        entries = await db.industrial_audit.find({}, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
        return {"items": entries}

    @router.post("/areas", status_code=201)
    async def create_area(payload: AreaIn, user=Depends(admin)):
        if await db.industrial_areas.find_one({"code": payload.code}):
            raise HTTPException(status_code=409, detail="Código de área já cadastrado")
        data = {"id": str(uuid.uuid4()), **payload.model_dump(), "created_at": iso_now()}
        await db.industrial_areas.insert_one(data.copy())
        await audit("AREA_CREATED", user, data["id"], {"code": payload.code})
        return data

    @router.post("/equipment", status_code=201)
    async def create_equipment(payload: EquipmentIn, user=Depends(admin)):
        if not await db.industrial_areas.find_one({"id": payload.area_id}):
            raise HTTPException(status_code=422, detail="Área inexistente")
        if await db.industrial_equipment.find_one({"code": payload.code}):
            raise HTTPException(status_code=409, detail="Código de equipamento já cadastrado")
        data = {"id": str(uuid.uuid4()), **payload.model_dump(), "created_at": iso_now()}
        await db.industrial_equipment.insert_one(data.copy())
        await audit("EQUIPMENT_CREATED", user, data["id"], {"code": payload.code})
        return data

    @router.post("/tags", status_code=201)
    async def create_tag(payload: TagIn, user=Depends(admin)):
        if not await db.industrial_areas.find_one({"id": payload.area_id}):
            raise HTTPException(status_code=422, detail="Área inexistente")
        if payload.equipment_id:
            equipment = await db.industrial_equipment.find_one({"id": payload.equipment_id})
            if not equipment or equipment["area_id"] != payload.area_id:
                raise HTTPException(status_code=422, detail="Equipamento não pertence à área")
        if payload.warning_low is not None and payload.warning_high is not None and payload.warning_low >= payload.warning_high:
            raise HTTPException(status_code=422, detail="Limites de aviso inválidos")
        if payload.critical_low is not None and payload.critical_high is not None and payload.critical_low >= payload.critical_high:
            raise HTTPException(status_code=422, detail="Limites críticos inválidos")
        if await db.industrial_tags.find_one({"code": payload.code}):
            raise HTTPException(status_code=409, detail="Tag já cadastrada")
        data = {"id": str(uuid.uuid4()), **payload.model_dump(),
                "source": "GATEWAY_PENDING", "enabled": True, "created_at": iso_now()}
        await db.industrial_tags.insert_one(data.copy())
        await audit("TAG_CREATED", user, data["id"], {"code": payload.code})
        return data

    @router.post("/loops", status_code=201)
    async def create_loop(payload: LoopIn, user=Depends(admin)):
        if not await db.industrial_areas.find_one({"id": payload.area_id}):
            raise HTTPException(status_code=422, detail="Área inexistente")
        for tag_id in (payload.pv_tag_id, payload.sp_tag_id, payload.mv_tag_id):
            if tag_id and not await db.industrial_tags.find_one({"id": tag_id, "area_id": payload.area_id}):
                raise HTTPException(status_code=422, detail="Tag da malha inválida ou fora da área")
        if await db.industrial_loops.find_one({"code": payload.code}):
            raise HTTPException(status_code=409, detail="Malha já cadastrada")
        data = {"id": str(uuid.uuid4()), **payload.model_dump(), "actual_mode": "NAO_COMISSIONADO",
                "write_enabled": False, "created_at": iso_now()}
        await db.industrial_loops.insert_one(data.copy())
        await audit("LOOP_REGISTERED", user, data["id"], {"code": payload.code})
        return data

    @router.post("/loops/{loop_id}/requested-mode")
    async def change_requested_mode(loop_id: str, payload: ModeIn, user=Depends(engineering)):
        loop = await db.industrial_loops.find_one({"id": loop_id}, {"_id": 0})
        if not loop:
            raise HTTPException(status_code=404, detail="Malha não cadastrada")
        await db.industrial_loops.update_one({"id": loop_id}, {"$set": {
            "requested_mode": payload.requested_mode, "actual_mode": "NAO_COMISSIONADO",
            "write_enabled": False, "updated_at": iso_now()
        }})
        await audit("LOOP_MODE_REQUESTED", user, loop_id, {
            "previous": loop.get("requested_mode"), "requested": payload.requested_mode,
            "justification": payload.justification, "effective": "NAO_COMISSIONADO"
        })
        return {"requested_mode": payload.requested_mode, "effective_mode": "NAO_COMISSIONADO",
                "physical_control_enabled": False,
                "message": "Modo registrado para engenharia, não aplicado ao PLC."}

    async def alarm_for_sample(tag, sample):
        """Alarmes derivados EXCLUSIVAMENTE da medição real com qualidade boa."""
        if sample["quality"] != "GOOD":
            return
        value = sample["value"]
        severity = None
        direction = None
        for label, low, high in (
            ("CRITICO", tag.get("critical_low"), tag.get("critical_high")),
            ("ATENCAO", tag.get("warning_low"), tag.get("warning_high")),
        ):
            if low is not None and value < low:
                severity, direction = label, "LOW"
                break
            if high is not None and value > high:
                severity, direction = label, "HIGH"
                break
        open_alarm = await db.industrial_alarms.find_one(
            {"tag_id": tag["id"], "state": "OPEN"}, {"_id": 0})
        if severity is None:
            if open_alarm:
                await db.industrial_alarms.update_one({"id": open_alarm["id"]},
                    {"$set": {"state": "CLEARED", "cleared_at": sample["observed_at"]}})
            return
        if open_alarm and open_alarm.get("severity") == severity and open_alarm.get("direction") == direction:
            await db.industrial_alarms.update_one({"id": open_alarm["id"]}, {"$set": {
                "last_value": value, "last_seen_at": sample["observed_at"]}})
            return
        if open_alarm:
            await db.industrial_alarms.update_one({"id": open_alarm["id"]}, {"$set": {
                "state": "CLEARED", "cleared_at": sample["observed_at"]}})
        alarm = {
            "id": str(uuid.uuid4()), "tag_id": tag["id"], "tag_code": tag["code"],
            "area_id": tag["area_id"], "severity": severity, "direction": direction,
            "state": "OPEN", "last_value": value, "unit": tag.get("unit", ""),
            "created_at": sample["observed_at"], "last_seen_at": sample["observed_at"],
            "acknowledged_at": None, "acknowledged_by": None, "cleared_at": None
        }
        await db.industrial_alarms.insert_one(alarm)

    @router.post("/gateway/ingest")
    async def ingest(payload: IngestIn, x_gateway_key: str = Header(default="")):
        key = os.environ.get("INDUSTRIAL_GATEWAY_KEY", "")
        if len(key) < 32:
            raise HTTPException(status_code=503, detail="Gateway ainda não configurado")
        if not hmac.compare_digest(x_gateway_key, key):
            raise HTTPException(status_code=401, detail="Gateway não autorizado")
        unique_ids = list({r.tag_id for r in payload.readings})
        tags = await db.industrial_tags.find({"id": {"$in": unique_ids}, "enabled": True},
                                             {"_id": 0}).to_list(100)
        catalog = {t["id"]: t for t in tags}
        if len(catalog) != len(unique_ids):
            raise HTTPException(status_code=422, detail="Existem tags não cadastradas/inativas")
        batch = []
        moment = now_utc()
        for reading in payload.readings:
            observed = parse_time(reading.observed_at)
            age = (moment - observed).total_seconds()
            if age < -120 or age > 600:
                raise HTTPException(status_code=422, detail="Leitura futura ou antiga; envio rejeitado")
            batch.append({
                "id": str(uuid.uuid4()), "tag_id": reading.tag_id,
                "value": reading.value, "quality": reading.quality,
                "observed_at": observed.isoformat(), "received_at": moment.isoformat(),
                "source": "AUTHENTICATED_GATEWAY"
            })
        await db.industrial_samples.insert_many(batch)
        for row in batch:
            await alarm_for_sample(catalog[row["tag_id"]], row)
        return {"received": len(batch), "accepted": True, "physical_command_sent": False}

    @router.get("/ai/status")
    async def ai_status(user=Depends(actor)):
        return {"available": bool(os.getenv("OPENAI_API_KEY") and
                 os.getenv("AI_EXTERNAL_SHARING_APPROVED") == "true"),
                "mode": "ANALISE_SOMENTE", "physical_control_enabled": False,
                "requires_explicit_data_sharing_approval": True}

    @router.post("/ai/analyze")
    async def ai_analyze(payload: AiRequest, user=Depends(engineering)):
        """Análise opcional com provedor REAL; envio externo precisa de aprovação."""
        if os.getenv("AI_EXTERNAL_SHARING_APPROVED") != "true" or not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(status_code=503, detail="IA real não configurada ou não autorizada pela usina")
        tag = await db.industrial_tags.find_one({"id": payload.tag_id}, {"_id": 0})
        if not tag:
            raise HTTPException(status_code=404, detail="Tag não cadastrada")
        cutoff = (now_utc() - timedelta(minutes=30)).isoformat()
        samples = await db.industrial_samples.find(
            {"tag_id": tag["id"], "observed_at": {"$gte": cutoff}, "quality": "GOOD"},
            {"_id": 0, "observed_at": 1, "value": 1}
        ).sort("observed_at", -1).limit(30).to_list(30)
        if not samples:
            raise HTTPException(status_code=422, detail="Sem medições reais recentes para analisar")
        import httpx
        prompt = {
            "tag": tag["code"], "unit": tag.get("unit", ""),
            "measurements": list(reversed(samples))
        }
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                reply = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                    json={
                        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
                        "messages": [
                            {"role": "system", "content": "Você analisa tendências industriais em português. Seja conservador: não invente dados, não emita comandos nem solicite escrita de setpoints. A amostra não prova causalidade. Retorne análise e pontos de atenção."},
                            {"role": "user", "content": str(prompt)}
                        ],
                        "temperature": 0.1
                    }
                )
                reply.raise_for_status()
                answer = reply.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError):
            raise HTTPException(status_code=502, detail="Provedor de IA indisponível")
        await audit("AI_ANALYSIS_REQUESTED", user, tag["id"],
                    {"samples_count": len(samples), "provider": "external", "write_enabled": False})
        return {"analysis": answer, "tag_code": tag["code"], "samples_count": len(samples),
                "generated_at": iso_now(), "control_command_sent": False}

    return router
