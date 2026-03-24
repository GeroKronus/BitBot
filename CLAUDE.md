# BitBot — Bot de Grid Trading Bitcoin

## Sobre o projeto
Bot de trading automatizado para Bitcoin usando estratégia Grid Trading na Hyperliquid.
Roda no servidor AWS Lightsail do OpenClaw (52.73.39.197) como serviço systemd.

## Integração com OpenClaw (Calila)
- **Servidor:** mesmo Lightsail do OpenClaw (ubuntu@52.73.39.197)
- **SSH:** `ssh -i "D:\Claude Code\Openclaw\openclaw-key.pem" -o StrictHostKeyChecking=no ubuntu@52.73.39.197`
- **Notificações:** bot escreve em `/home/ubuntu/claude-to-calila.txt` → Calila envia via Telegram para Rogério
- **Comandos:** Rogério envia via Telegram → Calila escreve em arquivo → bot lê e executa
- **Credenciais AWS Bedrock:** `AWS_ACCESS_KEY_ID=AKIAVQNF7V3IFDMRCJEE` (mesmo IAM user openclaw-bedrock)
- **Diretório no servidor:** `/home/ubuntu/gridbot/`
- **Dashboard:** http://52.73.39.197:8099

## Estratégia
- Grid Trading: ordens de compra abaixo do preço, vendas acima
- Stop Loss + Trailing Profit para proteção
- AI Market Analyst (Claude Haiku via Bedrock) analisa mercado a cada 2h e ajusta parâmetros
- Modo REAL ativo na Hyperliquid com leverage 4x

## Stack
- Python 3 + ccxt (Hyperliquid API)
- AWS Bedrock (Claude Haiku) para AI Market Analyst
- Systemd service para rodar 24/7
- JSON Lines para log de trades
- HTTP status endpoint + dashboard na porta 8099

## Configuração Atual
- **Exchange:** Hyperliquid (perpetual futures)
- **Par:** BTC/USDC:USDC
- **Modo:** REAL
- **Capital:** ~$126 USDC
- **Leverage:** 4x
- **Grid:** 5 níveis, 0.5% espaçamento, $20/ordem
- **Wallet:** MetaMask (0xc5FF...3AFA)
- **API Wallet:** 0x6d11448Da1B4342744B7992d887Ab8A03Da5C2De

## Regras
- NUNCA aumentar leverage ou capital sem aprovação explícita do Rogério
- Notificar via Telegram (Calila) todo trade executado
- Relatório diário de P&L às 21h UTC
- AI Analyst pode ajustar parâmetros dentro de limites seguros
