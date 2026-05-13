# TradeBot — Deploy no Fly.io

Bot de trading automatizado: recebe sinais do TradingView via webhook,
executa ordens na Binance Futures, e serve um dashboard em tempo real.

---

## Pré-requisitos

- [flyctl instalado](https://fly.io/docs/hands-on/install-flyctl/)
- Conta no Fly.io (`fly auth login`)
- (Opcional) API keys da Binance Testnet para começar em paper trading

---

## Deploy em 4 comandos

```bash
# 1. Entra na pasta do projecto
cd tradebot

# 2. Cria a app no Fly.io (só na primeira vez)
fly launch --name tradebot --region mad --no-deploy

# 3. Define as variáveis de ambiente (secrets)
fly secrets set WEBHOOK_SECRET="escolhe_um_secret_seguro"
fly secrets set BINANCE_API_KEY="a_tua_api_key"
fly secrets set BINANCE_API_SECRET="o_teu_api_secret"

# 4. Faz deploy!
fly deploy
```

O bot fica disponível em: `https://tradebot.fly.dev`

---

## Configurar o TradingView

1. Abre qualquer gráfico no TradingView
2. Cria um alerta (ícone do sino)
3. Em "Alert actions" → "Webhook URL" coloca:
   ```
   https://tradebot.fly.dev/webhook
   ```
4. Em "Message" coloca (em JSON):
   ```json
   {
     "secret": "o_teu_webhook_secret",
     "symbol": "{{ticker}}",
     "action": "BUY",
     "price": {{close}},
     "size_usd": 50,
     "strategy": "O nome da tua estratégia"
   }
   ```

Para fechar posição usa `"action": "CLOSE"`.

---

## Variáveis de ambiente

| Variável | Default | Descrição |
|---|---|---|
| `WEBHOOK_SECRET` | `muda_este_secret` | Chave secreta para autenticar alertas |
| `BINANCE_API_KEY` | — | API key da Binance |
| `BINANCE_API_SECRET` | — | Secret da Binance |
| `PAPER_TRADING` | `true` | `true` = testnet, `false` = real |
| `MAX_POSITION_USDT` | `50` | Tamanho máximo por posição em USDT |
| `MAX_DAILY_LOSS_PCT` | `5` | Stop loss diário em % do saldo |

---

## Comandos úteis

```bash
fly logs              # ver logs em tempo real
fly status            # estado da máquina
fly ssh console       # acesso SSH à máquina
fly secrets list      # listar secrets definidos
fly scale memory 512  # aumentar memória se necessário
```

---

## Estrutura do projecto

```
tradebot/
├── main.py           # servidor FastAPI (webhook + API REST)
├── static/
│   └── index.html    # dashboard
├── requirements.txt
├── Dockerfile
├── fly.toml
└── README.md
```

---

## Endpoints da API

| Endpoint | Método | Descrição |
|---|---|---|
| `/` | GET | Dashboard |
| `/webhook` | POST | Recebe sinais do TradingView |
| `/api/status` | GET | Estado geral do bot |
| `/api/positions` | GET | Posições abertas |
| `/api/signals` | GET | Últimos sinais recebidos |
| `/api/trades` | GET | Histórico de trades |
| `/api/health` | GET | Health check |

---

## Próximos passos

- [ ] Adicionar base de dados SQLite para persistência (volumes do Fly.io)
- [ ] Notificações Telegram quando uma ordem é executada
- [ ] Suporte a Bybit além de Binance
- [ ] Backtesting da estratégia antes de ir a real
