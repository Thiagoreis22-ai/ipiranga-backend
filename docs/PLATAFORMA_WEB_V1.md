# Usina Ipiranga — integração web (fase inicial)

## Objetivo
Integrar a plataforma industrial com o frontend React e backend FastAPI existentes, **sem apagar a aplicação anterior**. A marca e protocolos dos CLPs não foram identificados; por isso a conexão industrial fica pendente.

## O que foi efetivamente conectado
- Rota autenticada `GET /api/industrial/status` no backend.
- Rota autenticada `GET /api/industrial/tags`, lendo somente os cadastros que existirem no MongoDB.
- Interface React `/industrial` com estados consultados à API. Se o servidor ou a rede cair, a interface mostra erro em vez de valores inventados.
- Resposta falsa do antigo endpoint de IA substituída por HTTP 503; integração real depende de aprovação da usina.
- Setup inicial sem senha padrão: exigir `INITIAL_ADMIN_PASSWORD` forte no ambiente (apenas bootstrap).

## Pendências verdadeiras
- Não há leitura direta OPC UA/Modbus, integração com PLC/SCADA, telemetria de tanques ou motor de IA de controle.
- Não há escrita em PLC, nem mudança automática de setpoints.
- Tags de produção não são criadas automaticamente.
- Lógica física requer mapeamento das tags, limites, permissivos, testes FAT/SAT, revisão OT e comissionamento autorizado.

## Ambiente
Copie `.env.example` para `.env` **no servidor** e defina: `MONGO_URL`, `DB_NAME`, `JWT_SECRET` (32+ caracteres), `CORS_ORIGINS` (origens reais da interface) e, apenas no primeiro cadastro, `INITIAL_ADMIN_PASSWORD` (12+ caracteres). Mantenha segredo fora do GitHub e rotacione chaves anteriormente versionadas.

## Publicação web
O código estar no GitHub **não** significa site publicado. Para ficar online, é necessário configurar hospedagem, DNS, banco, TLS, secrets e autorização do operador da infraestrutura.

## Atenção de segurança
Os repositórios existentes já continham `.env` versionados. Removê-los do commit atual não apaga histórico; credenciais expostas devem ser revogadas/rotacionadas antes de qualquer publicação.
