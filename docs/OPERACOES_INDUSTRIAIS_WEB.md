# CENTRO DE OPERAÇÕES INTELIGENTE — USINA IPIRANGA

## Escopo implementado na fase WEB

Esta versão foi incorporada aos repositórios `ipiranga-backend` (FastAPI, MongoDB) e `ipiranga-frontend` (React). Não é um sistema SCADA homologado, e **não controla a instalação física**.

### Funcionalidades novas

- **`GET /api/ops/status`**: estado efetivo da entrada de dados e do controle industrial (bloqueado).
- **`GET /api/ops/catalog`**: áreas, equipamentos, tags e malhas reais cadastrados.
- **`GET /api/ops/overview`**: indicadores obtidos dos registros e amostras persistidas.
- **`GET /api/ops/telemetry`**: última amostra recebida por tag, qualidade e frescor.
- **`GET /api/ops/trends/{tag_id}?hours=24`**: histórico de 1 a 720 horas, até 1.500 pontos, com indicação de truncamento.
- **`GET /api/ops/alarms`**: eventos de limite de tags reais, disparados somente por qualidade GOOD.
- **`POST /api/ops/alarms/{id}/ack`**: reconhecimento por supervisor/administrador, auditado.
- **`GET /api/ops/audit`**: trilha de auditoria da engenharia.
- **`POST /api/ops/areas`**, **`/equipment`**, **`/tags`**, **`/loops`**: cadastros administrados no servidor.
- **`POST /api/ops/loops/{id}/requested-mode`**: registra um modo *pretendido* MANUAL/SUPERVISORIO/AUTOMATICO. **O estado físico permanece NÃO COMISSIONADO e NENHUM comando é enviado.**
- **`POST /api/ops/gateway/ingest`**: entrada autenticada de medições reais, sem inventar tags.
- **`/api/ops/ws`**: WebSocket autenticado (JWT no primeiro frame), com atualização WEB a cada ~5s de até 300 tags, sem comandos.
- **`POST /api/ops/ai/analyze`**: análise auxiliar de no máximo 30 medições reais, somente quando chaves e aprovação de envio externo forem explicitamente configuradas. IA não escreve setpoints.

### Configuração no servidor

Configure as variáveis definidas em `.env.example`:

- `MONGO_URL`, `DB_NAME`: banco persistente.
- `JWT_SECRET`: valor **aleatório** com 32+ caracteres, mantido fora do GitHub.
- `CORS_ORIGINS`: URLs HTTPS exatas da interface web; também autorizam as origens do WebSocket.
- `INITIAL_ADMIN_PASSWORD`: senha forte apenas na inicialização, remover depois.
- `INDUSTRIAL_GATEWAY_KEY`: segredo independente com 32+ caracteres para o gateway de dados.
- `AI_EXTERNAL_SHARING_APPROVED=false` por padrão.
- `OPENAI_API_KEY` e `OPENAI_MODEL` apenas após liberação por segurança da informação.

Os arquivos `.env` já foram versionados em commits antigos dos dois repositórios. **Remover do branch atual NÃO apaga os históricos**. Revogue e substitua quaisquer credenciais, senhas, JWTs, chaves de IA e strings de conexão expostas **ANTES** da publicação.

### Entrada real de medições

O gateway autorizado (não o navegador) envia JSON para `POST /api/ops/gateway/ingest` com cabeçalho `X-Gateway-Key`. Corpo de contrato, com placeholders (NÃO são valores de processo):

```json
{
  "readings": [{
    "tag_id": "UUID_REAL_CADASTRADO",
    "value": 0,
    "observed_at": "DATA_ISO8601_REAL_COM_TIMEZONE",
    "quality": "GOOD"
  }]
}
```

O campo numérico ilustrativo `0` acima é apenas para mostrar o formato JSON e **não deve ser enviado** sem uma amostra real. O backend rejeita timestamp sem timezone, medições não finitas, amostras com desvio de relógio e tags não cadastradas. O gateway precisa de controle de identidade, rede e autorização industrial.

### Modos de controle

- MANUAL, SUPERVISÓRIO, AUTOMÁTICO podem ser escolhidos como **intenção de engenharia**, auditada.
- Campo `actual_mode=NAO_COMISSIONADO` e `write_enabled=false` permanecem fixos.
- Não existe API de escrita de tags, acionamento de bombas, válvulas ou motores.
- A arquitetura final de controle exige limites, permissivos, intertravamentos, validação e habilitação executadas com o time OT e PLC.

### Ainda necessário para operação industrial

1. Identificar arquitetura local de rede/CLP/SCADA e estabelecer gateway homologado.
2. Mapear áreas/equipamentos/tags e limites reais com engenharia de processo.
3. Implantar TLS, autenticação forte/MFA, segmentação OT/IT, trilha de auditoria e monitoramento.
4. Reforçar RBAC por área, rate limit e segurança de gateway (ex.: mTLS, IP allowlisting).
5. Escalar historian (índices/retention/TimescaleDB), migrações, backup, restauração e testes de carga.
6. Homologar IA com indicadores verificáveis, política de dados e análise de segurança.
7. Realizar FAT/SAT e comissionamento controlado. Não permitir comandos físicos desta aplicação antes disso.

### Publicação web

A hospedagem pública exige frontend hospedado, backend API HTTPS, MongoDB seguro, URLs configuradas e autorização da usina. A integração PLC deve permanecer na rede industrial, isolada da internet pública. **GitHub não é site publicado.**
