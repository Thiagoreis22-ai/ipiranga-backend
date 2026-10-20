from dotenv import load_dotenv
import os
from pymongo import MongoClient

# Configuração carregada pela aplicação principal abaixo.
from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, UploadFile, File
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone
import jwt
import bcrypt

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT Config
JWT_SECRET = os.environ.get('JWT_SECRET', '')
if len(JWT_SECRET) < 32 or JWT_SECRET.startswith("SUBSTITUA_"):
    raise RuntimeError("Configure JWT_SECRET com pelo menos 32 caracteres no ambiente.")
JWT_ALGORITHM = "HS256"

# Emergent LLM Key
EMERGENT_LLM_KEY = os.environ.get('EMERGENT_LLM_KEY')

# Create the main app
app = FastAPI(title="IPIRANGA AI - Tratamento de Caldo")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")
security = HTTPBearer()

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============== MODELS ==============

class UserCreate(BaseModel):
    name: str
    matricula: str
    sector: str = Field(default="Tratamento de Caldo")
    function: str  # operador, supervisor, lider, admin
    password: str
    role: str = Field(default="operator")  # operator, supervisor, admin

class UserLogin(BaseModel):
    matricula: str
    password: str

class UserResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    name: str
    matricula: str
    sector: str
    function: str
    role: str
    active: bool
    created_at: str
    created_by: Optional[str] = None

class UserUpdate(BaseModel):
    name: Optional[str] = None
    function: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None

class ParameterCreate(BaseModel):
    ph: float
    brix: float
    pol: float
    turbidity: float
    temperature: float
    flow: float
    shift: str = Field(default="A")
    notes: Optional[str] = None

class ParameterResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    ph: float
    brix: float
    pol: float
    turbidity: float
    temperature: float
    flow: float
    shift: str
    notes: Optional[str]
    operator_id: str
    operator_name: str
    operator_matricula: str
    timestamp: str

class OccurrenceCreate(BaseModel):
    equipment: str
    occurrence_type: str
    urgency: str
    description: str
    photo_base64: Optional[str] = None

class OccurrenceResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    protocol: str
    equipment: str
    occurrence_type: str
    urgency: str
    description: str
    photo_url: Optional[str]
    operator_id: str
    operator_name: str
    operator_matricula: str
    status: str
    timestamp: str
    parameters_snapshot: Optional[dict]
    notifications_sent: bool = False

class ChemicalDosageCreate(BaseModel):
    chemical_type: str
    quantity: float
    unit: str
    cost_per_unit: float
    shift: str
    notes: Optional[str] = None

class ChemicalDosageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    chemical_type: str
    quantity: float
    unit: str
    cost_per_unit: float
    total_cost: float
    shift: str
    notes: Optional[str]
    operator_id: str
    operator_name: str
    operator_matricula: str
    timestamp: str

class ChatMessage(BaseModel):
    message: str
    session_id: Optional[str] = None

class ChatResponse(BaseModel):
    response: str
    risk_level: str
    probable_cause: str
    recommended_action: str
    escalate: bool
    session_id: str

class NotificationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    type: str
    title: str
    message: str
    occurrence_id: Optional[str]
    occurrence_protocol: Optional[str]
    urgency: Optional[str]
    read: bool
    created_at: str
    target_roles: List[str]

# ============== WORK ORDER (OS) MODELS ==============

class WorkOrderCreate(BaseModel):
    title: str
    description: str
    equipment: str
    priority: str  # baixa, media, alta, urgente
    assigned_to: str  # operator user_id
    due_date: Optional[str] = None

class WorkOrderResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    os_number: str
    title: str
    description: str
    equipment: str
    priority: str
    status: str
    assigned_to: str
    assigned_to_name: str
    assigned_to_matricula: str
    created_by: str
    created_by_name: str
    due_date: Optional[str]
    completed_at: Optional[str]
    completion_notes: Optional[str]
    created_at: str

# ============== AUTH HELPERS ==============

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_token(user_id: str, role: str, name: str, matricula: str) -> str:
    payload = {
        "user_id": user_id,
        "role": role,
        "name": name,
        "matricula": matricula,
        "exp": datetime.now(timezone.utc).timestamp() + 86400 * 7
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

def require_roles(allowed_roles: List[str]):
    async def role_checker(current_user: dict = Depends(get_current_user)):
        if current_user["role"] not in allowed_roles:
            raise HTTPException(
                status_code=403, 
                detail=f"Acesso restrito. Requer: {', '.join(allowed_roles)}"
            )
        return current_user
    return role_checker

# ============== NOTIFICATION HELPERS ==============

async def create_notification(
    notification_type: str,
    title: str,
    message: str,
    target_roles: List[str],
    occurrence_id: str = None,
    occurrence_protocol: str = None,
    urgency: str = None
):
    notification = {
        "id": str(uuid.uuid4()),
        "type": notification_type,
        "title": title,
        "message": message,
        "occurrence_id": occurrence_id,
        "occurrence_protocol": occurrence_protocol,
        "urgency": urgency,
        "read": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_roles": target_roles
    }
    await db.notifications.insert_one(notification)
    return notification

# ============== AUTH ROUTES ==============

@api_router.post("/auth/login")
async def login(credentials: UserLogin):
    user = await db.users.find_one({"matricula": credentials.matricula})
    if not user:
        raise HTTPException(status_code=401, detail="Matrícula não encontrada")
    if not user.get("active", True):
        raise HTTPException(status_code=401, detail="Usuário desativado. Contate o supervisor.")
    if not verify_password(credentials.password, user["password"]):
        raise HTTPException(status_code=401, detail="Senha incorreta")
    
    token = create_token(user["id"], user["role"], user["name"], user["matricula"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "name": user["name"],
            "matricula": user["matricula"],
            "sector": user["sector"],
            "function": user["function"],
            "role": user["role"]
        }
    }

@api_router.get("/auth/me", response_model=UserResponse)
async def get_me(current_user: dict = Depends(get_current_user)):
    user = await db.users.find_one({"id": current_user["user_id"]}, {"_id": 0, "password": 0})
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return UserResponse(**user)

# ============== USER MANAGEMENT ROUTES (ADMIN/SUPERVISOR ONLY) ==============

@api_router.post("/users", response_model=UserResponse)
async def create_user(
    user: UserCreate, 
    current_user: dict = Depends(require_roles(["admin", "supervisor"]))
):
    # Check if matricula already exists
    existing = await db.users.find_one({"matricula": user.matricula})
    if existing:
        raise HTTPException(status_code=400, detail="Matrícula já cadastrada no sistema")
    
    # Supervisors can only create operators
    if current_user["role"] == "supervisor" and user.role not in ["operator"]:
        raise HTTPException(status_code=403, detail="Supervisores só podem criar operadores")
    
    user_id = str(uuid.uuid4())
    user_doc = {
        "id": user_id,
        "name": user.name,
        "matricula": user.matricula,
        "sector": user.sector,
        "function": user.function,
        "password": hash_password(user.password),
        "role": user.role,
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": current_user["user_id"]
    }
    await db.users.insert_one(user_doc)
    
    return UserResponse(
        id=user_id,
        name=user.name,
        matricula=user.matricula,
        sector=user.sector,
        function=user.function,
        role=user.role,
        active=True,
        created_at=user_doc["created_at"],
        created_by=current_user["user_id"]
    )

@api_router.get("/users", response_model=List[UserResponse])
async def list_users(current_user: dict = Depends(require_roles(["admin", "supervisor"]))):
    users = await db.users.find({}, {"_id": 0, "password": 0}).to_list(1000)
    return [UserResponse(**u) for u in users]

@api_router.get("/users/{user_id}", response_model=UserResponse)
async def get_user(user_id: str, current_user: dict = Depends(require_roles(["admin", "supervisor"]))):
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password": 0})
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return UserResponse(**user)

@api_router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str, 
    update: UserUpdate, 
    current_user: dict = Depends(require_roles(["admin", "supervisor"]))
):
    update_data = {k: v for k, v in update.model_dump().items() if v is not None}
    
    if not update_data:
        raise HTTPException(status_code=400, detail="Nenhum dado para atualizar")
    
    # Supervisors cannot change roles to admin
    if current_user["role"] == "supervisor" and update_data.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Supervisores não podem promover a administrador")
    
    result = await db.users.update_one({"id": user_id}, {"$set": update_data})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    
    user = await db.users.find_one({"id": user_id}, {"_id": 0, "password": 0})
    return UserResponse(**user)

@api_router.delete("/users/{user_id}")
async def deactivate_user(user_id: str, current_user: dict = Depends(require_roles(["admin"]))):
    result = await db.users.update_one({"id": user_id}, {"$set": {"active": False}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return {"message": "Usuário desativado com sucesso"}

@api_router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str, 
    new_password: str,
    current_user: dict = Depends(require_roles(["admin", "supervisor"]))
):
    result = await db.users.update_one(
        {"id": user_id}, 
        {"$set": {"password": hash_password(new_password)}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return {"message": "Senha redefinida com sucesso"}

# ============== NOTIFICATIONS ROUTES ==============

@api_router.get("/notifications", response_model=List[NotificationResponse])
async def get_notifications(
    unread_only: bool = False,
    current_user: dict = Depends(get_current_user)
):
    query = {"target_roles": current_user["role"]}
    if unread_only:
        query["read"] = False
    
    notifications = await db.notifications.find(query, {"_id": 0}).sort("created_at", -1).limit(50).to_list(50)
    return [NotificationResponse(**n) for n in notifications]

@api_router.patch("/notifications/{notification_id}/read")
async def mark_notification_read(notification_id: str, current_user: dict = Depends(get_current_user)):
    result = await db.notifications.update_one({"id": notification_id}, {"$set": {"read": True}})
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Notificação não encontrada")
    return {"message": "Notificação marcada como lida"}

@api_router.post("/notifications/mark-all-read")
async def mark_all_notifications_read(current_user: dict = Depends(get_current_user)):
    await db.notifications.update_many(
        {"target_roles": current_user["role"], "read": False},
        {"$set": {"read": True}}
    )
    return {"message": "Todas notificações marcadas como lidas"}

@api_router.get("/notifications/count")
async def get_unread_count(current_user: dict = Depends(get_current_user)):
    count = await db.notifications.count_documents({
        "target_roles": current_user["role"],
        "read": False
    })
    return {"unread_count": count}

# ============== PARAMETERS ROUTES ==============

@api_router.post("/parameters", response_model=ParameterResponse)
async def create_parameter(param: ParameterCreate, current_user: dict = Depends(get_current_user)):
    param_id = str(uuid.uuid4())
    param_doc = {
        "id": param_id,
        **param.model_dump(),
        "operator_id": current_user["user_id"],
        "operator_name": current_user["name"],
        "operator_matricula": current_user["matricula"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    await db.parameters.insert_one(param_doc)
    return ParameterResponse(**param_doc)

@api_router.get("/parameters", response_model=List[ParameterResponse])
async def get_parameters(
    limit: int = 100,
    shift: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    query = {}
    if shift:
        query["shift"] = shift
    
    params = await db.parameters.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
    return [ParameterResponse(**p) for p in params]

@api_router.get("/parameters/latest", response_model=ParameterResponse)
async def get_latest_parameter(current_user: dict = Depends(get_current_user)):
    param = await db.parameters.find_one({}, {"_id": 0}, sort=[("timestamp", -1)])
    if not param:
        raise HTTPException(status_code=404, detail="Nenhum parâmetro registrado")
    return ParameterResponse(**param)

@api_router.get("/parameters/stats")
async def get_parameters_stats(current_user: dict = Depends(get_current_user)):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    
    pipeline = [
        {"$match": {"timestamp": {"$gte": today}}},
        {"$group": {
            "_id": None,
            "avg_ph": {"$avg": "$ph"},
            "avg_brix": {"$avg": "$brix"},
            "avg_pol": {"$avg": "$pol"},
            "avg_turbidity": {"$avg": "$turbidity"},
            "avg_temperature": {"$avg": "$temperature"},
            "avg_flow": {"$avg": "$flow"},
            "count": {"$sum": 1}
        }}
    ]
    
    result = await db.parameters.aggregate(pipeline).to_list(1)
    if result:
        stats = result[0]
        stats.pop("_id", None)
        return stats
    return {"avg_ph": 0, "avg_brix": 0, "avg_pol": 0, "avg_turbidity": 0, "avg_temperature": 0, "avg_flow": 0, "count": 0}

# ============== OCCURRENCES ROUTES ==============

async def generate_protocol():
    year = datetime.now(timezone.utc).year
    count = await db.occurrences.count_documents({"protocol": {"$regex": f"IPR-OC-{year}"}})
    return f"IPR-OC-{year}-{str(count + 1).zfill(4)}"

@api_router.post("/occurrences", response_model=OccurrenceResponse)
async def create_occurrence(occ: OccurrenceCreate, current_user: dict = Depends(get_current_user)):
    occ_id = str(uuid.uuid4())
    protocol = await generate_protocol()
    
    # Get latest parameters for snapshot
    latest_params = await db.parameters.find_one({}, {"_id": 0}, sort=[("timestamp", -1)])
    
    occ_doc = {
        "id": occ_id,
        "protocol": protocol,
        "equipment": occ.equipment,
        "occurrence_type": occ.occurrence_type,
        "urgency": occ.urgency,
        "description": occ.description,
        "photo_url": occ.photo_base64 if occ.photo_base64 else None,
        "operator_id": current_user["user_id"],
        "operator_name": current_user["name"],
        "operator_matricula": current_user["matricula"],
        "status": "aberta",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "parameters_snapshot": latest_params,
        "notifications_sent": True
    }
    await db.occurrences.insert_one(occ_doc)
    
    # Create notifications for supervisors and admins
    urgency_labels = {"baixa": "Baixa", "media": "Média", "alta": "Alta", "critica": "CRÍTICA"}
    urgency_label = urgency_labels.get(occ.urgency, occ.urgency)
    
    notification_title = f"Nova Ocorrência: {protocol}"
    if occ.urgency in ["alta", "critica"]:
        notification_title = f"🚨 ALERTA: {protocol} - Urgência {urgency_label}"
    
    await create_notification(
        notification_type="occurrence_created",
        title=notification_title,
        message=f"Operador {current_user['name']} ({current_user['matricula']}) registrou ocorrência no equipamento {occ.equipment}. Tipo: {occ.occurrence_type}. Descrição: {occ.description[:100]}...",
        target_roles=["supervisor", "admin"],
        occurrence_id=occ_id,
        occurrence_protocol=protocol,
        urgency=occ.urgency
    )
    
    return OccurrenceResponse(**occ_doc)

@api_router.get("/occurrences", response_model=List[OccurrenceResponse])
async def get_occurrences(
    limit: int = 100,
    status: Optional[str] = None,
    urgency: Optional[str] = None,
    equipment: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    my_only: bool = False,
    current_user: dict = Depends(get_current_user)
):
    query = {}
    
    # Operators can only see their own occurrences unless supervisor/admin
    if current_user["role"] == "operator" or my_only:
        query["operator_id"] = current_user["user_id"]
    
    if status:
        query["status"] = status
    if urgency:
        query["urgency"] = urgency
    if equipment:
        query["equipment"] = equipment
    if start_date:
        query["timestamp"] = {"$gte": start_date}
    if end_date:
        if "timestamp" in query:
            query["timestamp"]["$lte"] = end_date
        else:
            query["timestamp"] = {"$lte": end_date}
    
    occs = await db.occurrences.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
    return [OccurrenceResponse(**o) for o in occs]

@api_router.get("/occurrences/all", response_model=List[OccurrenceResponse])
async def get_all_occurrences(
    limit: int = 100,
    status: Optional[str] = None,
    urgency: Optional[str] = None,
    current_user: dict = Depends(require_roles(["supervisor", "admin"]))
):
    """Get all occurrences - supervisors and admins only"""
    query = {}
    if status:
        query["status"] = status
    if urgency:
        query["urgency"] = urgency
    
    occs = await db.occurrences.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
    return [OccurrenceResponse(**o) for o in occs]

@api_router.get("/occurrences/{occ_id}", response_model=OccurrenceResponse)
async def get_occurrence(occ_id: str, current_user: dict = Depends(get_current_user)):
    occ = await db.occurrences.find_one({"id": occ_id}, {"_id": 0})
    if not occ:
        raise HTTPException(status_code=404, detail="Ocorrência não encontrada")
    
    # Operators can only see their own occurrences
    if current_user["role"] == "operator" and occ["operator_id"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Acesso negado a esta ocorrência")
    
    return OccurrenceResponse(**occ)

@api_router.patch("/occurrences/{occ_id}/status")
async def update_occurrence_status(
    occ_id: str, 
    new_status: str,
    current_user: dict = Depends(require_roles(["supervisor", "admin"]))
):
    valid_statuses = ["aberta", "andamento", "resolvida"]
    if new_status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Status inválido. Use: {', '.join(valid_statuses)}")
    
    occ = await db.occurrences.find_one({"id": occ_id}, {"_id": 0})
    if not occ:
        raise HTTPException(status_code=404, detail="Ocorrência não encontrada")
    
    result = await db.occurrences.update_one(
        {"id": occ_id}, 
        {"$set": {
            "status": new_status,
            "status_updated_by": current_user["user_id"],
            "status_updated_at": datetime.now(timezone.utc).isoformat()
        }}
    )
    
    # Notify the operator who created the occurrence
    await create_notification(
        notification_type="status_changed",
        title=f"Status Atualizado: {occ['protocol']}",
        message=f"O status da ocorrência {occ['protocol']} foi alterado para '{new_status}' por {current_user['name']}.",
        target_roles=["operator"],
        occurrence_id=occ_id,
        occurrence_protocol=occ["protocol"]
    )
    
    return {"message": f"Status atualizado para '{new_status}'"}

@api_router.get("/occurrences/stats/summary")
async def get_occurrences_stats(current_user: dict = Depends(get_current_user)):
    pipeline = [
        {"$group": {
            "_id": "$urgency",
            "count": {"$sum": 1}
        }}
    ]
    
    by_urgency = await db.occurrences.aggregate(pipeline).to_list(10)
    
    total = await db.occurrences.count_documents({})
    open_count = await db.occurrences.count_documents({"status": "aberta"})
    in_progress = await db.occurrences.count_documents({"status": "andamento"})
    resolved = await db.occurrences.count_documents({"status": "resolvida"})
    
    return {
        "total": total,
        "open": open_count,
        "in_progress": in_progress,
        "resolved": resolved,
        "by_urgency": {item["_id"]: item["count"] for item in by_urgency}
    }

# ============== CHEMICAL DOSAGE ROUTES ==============

@api_router.post("/dosage", response_model=ChemicalDosageResponse)
async def create_dosage(dosage: ChemicalDosageCreate, current_user: dict = Depends(get_current_user)):
    dosage_id = str(uuid.uuid4())
    total_cost = dosage.quantity * dosage.cost_per_unit
    
    dosage_doc = {
        "id": dosage_id,
        **dosage.model_dump(),
        "total_cost": total_cost,
        "operator_id": current_user["user_id"],
        "operator_name": current_user["name"],
        "operator_matricula": current_user["matricula"],
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    await db.dosages.insert_one(dosage_doc)
    return ChemicalDosageResponse(**dosage_doc)

@api_router.get("/dosage", response_model=List[ChemicalDosageResponse])
async def get_dosages(
    limit: int = 100,
    chemical_type: Optional[str] = None,
    shift: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    query = {}
    if chemical_type:
        query["chemical_type"] = chemical_type
    if shift:
        query["shift"] = shift
    
    dosages = await db.dosages.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
    return [ChemicalDosageResponse(**d) for d in dosages]

@api_router.get("/dosage/stats")
async def get_dosage_stats(current_user: dict = Depends(get_current_user)):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    
    pipeline = [
        {"$match": {"timestamp": {"$gte": today}}},
        {"$group": {
            "_id": "$chemical_type",
            "total_quantity": {"$sum": "$quantity"},
            "total_cost": {"$sum": "$total_cost"},
            "count": {"$sum": 1}
        }}
    ]
    
    result = await db.dosages.aggregate(pipeline).to_list(10)
    
    stats = {}
    for item in result:
        stats[item["_id"]] = {
            "total_quantity": item["total_quantity"],
            "total_cost": item["total_cost"],
            "count": item["count"]
        }
    
    return stats

@api_router.get("/dosage/daily")
async def get_daily_dosage(days: int = 7, current_user: dict = Depends(get_current_user)):
    pipeline = [
        {"$addFields": {
            "date": {"$substr": ["$timestamp", 0, 10]}
        }},
        {"$group": {
            "_id": {"date": "$date", "chemical_type": "$chemical_type"},
            "total_quantity": {"$sum": "$quantity"},
            "total_cost": {"$sum": "$total_cost"}
        }},
        {"$sort": {"_id.date": -1}},
        {"$limit": days * 4}
    ]
    
    result = await db.dosages.aggregate(pipeline).to_list(100)
    return result

# ============== AI CHAT ROUTES ==============

SYSTEM_PROMPT = """Você é o Assistente Operacional IPIRANGA AI, especialista em Tratamento de Caldo para usinas sucroenergéticas.

Sua função é analisar situações operacionais reportadas pelos operadores e fornecer:
1. CAUSA PROVÁVEL: Identifique a causa mais provável do problema
2. AÇÃO RECOMENDADA: Sugira ações corretivas específicas
3. NÍVEL DE RISCO: Classifique como BAIXO, MÉDIO, ALTO ou CRÍTICO
4. ESCALAR: Indique se deve acionar supervisão (true/false)

Conhecimentos técnicos:
- pH ideal do caldo: 6.8 a 7.2
- Brix ideal: 14 a 18
- Pol ideal: 12 a 16
- Turbidez máxima aceitável: 500 NTU
- Temperatura de aquecimento: 103-105°C
- Vazão nominal: conforme projeto da usina

Problemas comuns:
- pH baixo (<6.5): indica fermentação ou contaminação
- pH alto (>7.5): excesso de cal
- Turbidez alta: problemas na decantação ou floculação
- Temperatura baixa: reduz eficiência da clarificação
- Brix/Pol fora de faixa: problemas na extração

Sempre responda em português brasileiro de forma técnica e profissional."""

@api_router.post("/chat", response_model=ChatResponse)
async def chat_with_ai(message: ChatMessage, current_user: dict = Depends(get_current_user)):
    session_id = message.session_id or str(uuid.uuid4())
    
    # Nenhuma resposta artificial é apresentada como inteligência integrada.
    # Habilitar somente após implantação de um serviço real aprovado pela engenharia.
    raise HTTPException(
        status_code=503,
        detail="IA indisponível: integração com modelo real ainda não configurada."
    )

@api_router.get("/chat/history")
async def get_chat_history(session_id: Optional[str] = None, limit: int = 50, current_user: dict = Depends(get_current_user)):
    query = {"user_id": current_user["user_id"]}
    if session_id:
        query["session_id"] = session_id
    
    history = await db.chat_history.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
    return history

# ============== SUPERVISOR DASHBOARD ROUTES ==============

@api_router.get("/supervisor/dashboard")
async def get_supervisor_dashboard(current_user: dict = Depends(require_roles(["supervisor", "admin"]))):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    
    # Occurrences by shift
    shift_pipeline = [
        {"$match": {"timestamp": {"$gte": today}}},
        {"$addFields": {
            "hour": {"$toInt": {"$substr": ["$timestamp", 11, 2]}}
        }},
        {"$addFields": {
            "shift": {
                "$switch": {
                    "branches": [
                        {"case": {"$lt": ["$hour", 8]}, "then": "C"},
                        {"case": {"$lt": ["$hour", 16]}, "then": "A"},
                    ],
                    "default": "B"
                }
            }
        }},
        {"$group": {
            "_id": "$shift",
            "count": {"$sum": 1}
        }}
    ]
    
    shifts = await db.occurrences.aggregate(shift_pipeline).to_list(3)
    
    # Critical occurrences
    critical = await db.occurrences.find(
        {"urgency": {"$in": ["alta", "critica"]}, "status": {"$ne": "resolvida"}},
        {"_id": 0}
    ).limit(10).to_list(10)
    
    # Open occurrences count
    open_count = await db.occurrences.count_documents({"status": "aberta"})
    in_progress_count = await db.occurrences.count_documents({"status": "andamento"})
    
    # Parameter trends (last 24 hours)
    params = await db.parameters.find({}, {"_id": 0}).sort("timestamp", -1).limit(24).to_list(24)
    
    # Top failures
    failure_pipeline = [
        {"$group": {
            "_id": "$occurrence_type",
            "count": {"$sum": 1}
        }},
        {"$sort": {"count": -1}},
        {"$limit": 5}
    ]
    
    top_failures = await db.occurrences.aggregate(failure_pipeline).to_list(5)
    
    # Treatment efficiency
    efficiency = 0
    if params:
        total_checks = 0
        passed_checks = 0
        for p in params:
            total_checks += 6
            if 6.8 <= p.get("ph", 0) <= 7.2:
                passed_checks += 1
            if 14 <= p.get("brix", 0) <= 18:
                passed_checks += 1
            if 12 <= p.get("pol", 0) <= 16:
                passed_checks += 1
            if p.get("turbidity", 1000) <= 500:
                passed_checks += 1
            if 103 <= p.get("temperature", 0) <= 105:
                passed_checks += 1
            if p.get("flow", 0) > 0:
                passed_checks += 1
        
        efficiency = round((passed_checks / total_checks) * 100, 1) if total_checks > 0 else 0
    
    # Active operators count
    active_operators = await db.users.count_documents({"role": "operator", "active": True})
    
    return {
        "shifts": {item["_id"]: item["count"] for item in shifts},
        "critical_occurrences": critical,
        "open_occurrences": open_count,
        "in_progress_occurrences": in_progress_count,
        "parameter_trends": params,
        "top_failures": [{"type": f["_id"], "count": f["count"]} for f in top_failures],
        "efficiency": efficiency,
        "active_operators": active_operators
    }

@api_router.get("/supervisor/weekly-trends")
async def get_weekly_trends(current_user: dict = Depends(require_roles(["supervisor", "admin"]))):
    pipeline = [
        {"$addFields": {
            "date": {"$substr": ["$timestamp", 0, 10]}
        }},
        {"$group": {
            "_id": "$date",
            "avg_ph": {"$avg": "$ph"},
            "avg_turbidity": {"$avg": "$turbidity"},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": -1}},
        {"$limit": 7}
    ]
    
    trends = await db.parameters.aggregate(pipeline).to_list(7)
    return trends

# ============== HISTORY/AUDIT ROUTES ==============

@api_router.get("/audit/logs")
async def get_audit_logs(
    entity_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
    current_user: dict = Depends(get_current_user)
):
    logs = []
    
    # For operators, only show their own logs
    operator_filter = {}
    if current_user["role"] == "operator":
        operator_filter = {"operator_id": current_user["user_id"]}
    
    if not entity_type or entity_type == "occurrence":
        query = {**operator_filter}
        occs = await db.occurrences.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
        for o in occs:
            o["entity_type"] = "occurrence"
        logs.extend(occs)
    
    if not entity_type or entity_type == "parameter":
        query = {**operator_filter}
        params = await db.parameters.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
        for p in params:
            p["entity_type"] = "parameter"
        logs.extend(params)
    
    if not entity_type or entity_type == "dosage":
        query = {**operator_filter}
        dosages = await db.dosages.find(query, {"_id": 0}).sort("timestamp", -1).limit(limit).to_list(limit)
        for d in dosages:
            d["entity_type"] = "dosage"
        logs.extend(dosages)
    
    logs.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return logs[:limit]

# ============== WORK ORDER (OS) ROUTES ==============

async def generate_os_number():
    year = datetime.now(timezone.utc).year
    count = await db.work_orders.count_documents({"os_number": {"$regex": f"OS-{year}"}})
    return f"OS-{year}-{str(count + 1).zfill(4)}"

@api_router.post("/work-orders", response_model=WorkOrderResponse)
async def create_work_order(
    wo: WorkOrderCreate, 
    current_user: dict = Depends(require_roles(["supervisor", "admin"]))
):
    # Get assigned operator info
    assigned_user = await db.users.find_one({"id": wo.assigned_to}, {"_id": 0})
    if not assigned_user:
        raise HTTPException(status_code=404, detail="Operador não encontrado")
    
    wo_id = str(uuid.uuid4())
    os_number = await generate_os_number()
    
    wo_doc = {
        "id": wo_id,
        "os_number": os_number,
        "title": wo.title,
        "description": wo.description,
        "equipment": wo.equipment,
        "priority": wo.priority,
        "status": "pendente",
        "assigned_to": wo.assigned_to,
        "assigned_to_name": assigned_user["name"],
        "assigned_to_matricula": assigned_user["matricula"],
        "created_by": current_user["user_id"],
        "created_by_name": current_user["name"],
        "due_date": wo.due_date,
        "completed_at": None,
        "completion_notes": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.work_orders.insert_one(wo_doc)
    
    # Create notification for the assigned operator
    priority_labels = {"baixa": "Baixa", "media": "Média", "alta": "Alta", "urgente": "URGENTE"}
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()),
        "type": "work_order_assigned",
        "title": f"Nova OS: {os_number}",
        "message": f"Você recebeu uma nova ordem de serviço de {current_user['name']}. Equipamento: {wo.equipment}. Prioridade: {priority_labels.get(wo.priority, wo.priority)}",
        "occurrence_id": wo_id,
        "occurrence_protocol": os_number,
        "urgency": wo.priority,
        "read": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_roles": ["operator"],
        "target_user_id": wo.assigned_to
    })
    
    return WorkOrderResponse(**wo_doc)

@api_router.get("/work-orders", response_model=List[WorkOrderResponse])
async def get_work_orders(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    query = {}
    
    # Operators only see their assigned work orders
    if current_user["role"] == "operator":
        query["assigned_to"] = current_user["user_id"]
    
    if status:
        query["status"] = status
    if priority:
        query["priority"] = priority
    
    work_orders = await db.work_orders.find(query, {"_id": 0}).sort("created_at", -1).to_list(100)
    return [WorkOrderResponse(**wo) for wo in work_orders]

@api_router.get("/work-orders/{wo_id}", response_model=WorkOrderResponse)
async def get_work_order(wo_id: str, current_user: dict = Depends(get_current_user)):
    wo = await db.work_orders.find_one({"id": wo_id}, {"_id": 0})
    if not wo:
        raise HTTPException(status_code=404, detail="Ordem de serviço não encontrada")
    
    # Operators can only see their own work orders
    if current_user["role"] == "operator" and wo["assigned_to"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Acesso negado")
    
    return WorkOrderResponse(**wo)

@api_router.patch("/work-orders/{wo_id}/start")
async def start_work_order(wo_id: str, current_user: dict = Depends(get_current_user)):
    wo = await db.work_orders.find_one({"id": wo_id}, {"_id": 0})
    if not wo:
        raise HTTPException(status_code=404, detail="Ordem de serviço não encontrada")
    
    if current_user["role"] == "operator" and wo["assigned_to"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Esta OS não está atribuída a você")
    
    await db.work_orders.update_one(
        {"id": wo_id},
        {"$set": {"status": "em_andamento", "started_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"message": "OS iniciada"}

@api_router.patch("/work-orders/{wo_id}/complete")
async def complete_work_order(
    wo_id: str, 
    completion_notes: str = "",
    current_user: dict = Depends(get_current_user)
):
    wo = await db.work_orders.find_one({"id": wo_id}, {"_id": 0})
    if not wo:
        raise HTTPException(status_code=404, detail="Ordem de serviço não encontrada")
    
    if current_user["role"] == "operator" and wo["assigned_to"] != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Esta OS não está atribuída a você")
    
    await db.work_orders.update_one(
        {"id": wo_id},
        {"$set": {
            "status": "concluida",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "completion_notes": completion_notes
        }}
    )
    
    # Notify the creator
    await db.notifications.insert_one({
        "id": str(uuid.uuid4()),
        "type": "work_order_completed",
        "title": f"OS Concluída: {wo['os_number']}",
        "message": f"A ordem de serviço {wo['os_number']} foi concluída por {current_user['name']}.",
        "occurrence_id": wo_id,
        "occurrence_protocol": wo["os_number"],
        "urgency": None,
        "read": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_roles": ["supervisor", "admin"]
    })
    
    return {"message": "OS concluída"}

@api_router.get("/work-orders/stats/summary")
async def get_work_orders_stats(current_user: dict = Depends(get_current_user)):
    query = {}
    if current_user["role"] == "operator":
        query["assigned_to"] = current_user["user_id"]
    
    total = await db.work_orders.count_documents(query)
    pending = await db.work_orders.count_documents({**query, "status": "pendente"})
    in_progress = await db.work_orders.count_documents({**query, "status": "em_andamento"})
    completed = await db.work_orders.count_documents({**query, "status": "concluida"})
    
    return {
        "total": total,
        "pending": pending,
        "in_progress": in_progress,
        "completed": completed
    }

@api_router.get("/operators/list")
async def list_operators(current_user: dict = Depends(require_roles(["supervisor", "admin"]))):
    """List all active operators for work order assignment"""
    operators = await db.users.find(
        {"role": "operator", "active": True},
        {"_id": 0, "password": 0}
    ).to_list(100)
    return operators

# ============== SETUP ROUTES ==============

@api_router.post("/setup/admin")
async def setup_admin():
    """Bootstrap permitido somente com senha forte configurada fora do código."""
    user_count = await db.users.count_documents({})
    if user_count > 0:
        raise HTTPException(status_code=400, detail="Sistema já possui usuários cadastrados")
    initial_password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")
    if len(initial_password) < 12:
        raise HTTPException(
            status_code=503,
            detail="Configuração segura pendente: defina INITIAL_ADMIN_PASSWORD com pelo menos 12 caracteres no servidor."
        )
    admin_doc = {
        "id": str(uuid.uuid4()),
        "name": "Administrador",
        "matricula": "ADM001",
        "sector": "Tratamento de Caldo",
        "function": "Administrador do Sistema",
        "password": hash_password(initial_password),
        "role": "admin",
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": None
    }
    await db.users.insert_one(admin_doc)
    return {
        "message": "Administrador inicial criado. Remova INITIAL_ADMIN_PASSWORD das variáveis do servidor.",
        "matricula": "ADM001"
    }

@api_router.get("/setup/status")
async def get_setup_status():
    """Check if system needs initial setup"""
    user_count = await db.users.count_documents({})
    return {
        "needs_setup": user_count == 0,
        "user_count": user_count
    }

# ============== INTEGRACAO INDUSTRIAL (SOMENTE LEITURA) ==============

@api_router.get("/industrial/status")
async def industrial_status(current_user: dict = Depends(get_current_user)):
    """Retorna estado real da integracao. Nao fabrica telemetria."""
    return {
        "integration_status": "NOT_CONFIGURED",
        "plc_vendor": None,
        "connector_type": None,
        "last_real_reading": None,
        "telemetry_status": "NO_REAL_DATA",
        "ai_analysis_status": "NOT_CONFIGURED",
        "automatic_control_enabled": False,
        "write_commands_enabled": False,
        "message": "Aguardando conexão industrial autorizada."
    }


@api_router.get("/industrial/tags")
async def industrial_tags(current_user: dict = Depends(get_current_user)):
    """Nenhuma tag e inventada. Devolve apenas tags cadastradas de fato."""
    tags = await db.industrial_tags.find({}, {"_id": 0}).limit(500).to_list(500)
    return {"items": tags, "count": len(tags)}


# ============== BASE ROUTES ==============

@api_router.get("/")
async def root():
    return {"message": "IPIRANGA AI - Sistema de Tratamento de Caldo", "version": "2.0.0"}

@api_router.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

# Include the routers in the main app
app.include_router(api_router)
from ops_router import build_ops_router
app.include_router(build_ops_router(db, get_current_user))
from ops_ws import build_ops_ws_router
app.include_router(build_ops_ws_router(db, JWT_SECRET))

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[origin.strip() for origin in os.environ.get('CORS_ORIGINS', 'http://localhost:3000').split(',') if origin.strip() and origin.strip() != '*'],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
# Mostrar coleções existentes
# Colecoes consultadas apenas em chamadas assincronas sob demanda.