import os
from pymongo import MongoClient
from dotenv import load_dotenv

print("Iniciando teste...")

load_dotenv()

mongo_url = os.getenv("MONGO_URL")

print("MONGO_URL encontrada:", "SIM" if mongo_url else "NÃO")

if not mongo_url:
    print("Erro: variável MONGO_URL não encontrada no .env")
    exit()

try:
    client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    print("🔥 Conectado ao MongoDB Atlas com sucesso!")
except Exception as e:
    print("❌ Erro na conexão:")
    print(e)