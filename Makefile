.PHONY: help up down stop restart logs status ps rebuild shell tail

COMPOSE  = docker compose
SERVICE  = binance-bot-v2

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Build image (if needed) and start the bot in background
	$(COMPOSE) up -d --build

down: ## Stop and remove containers
	$(COMPOSE) down

stop: ## Stop containers without removing them
	$(COMPOSE) stop

restart: down up ## Stop → rebuild → start

logs: ## Follow live container stdout (Ctrl+C to exit)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

status: ## Show container status
	$(COMPOSE) ps $(SERVICE)

ps: ## List all containers for this project
	$(COMPOSE) ps

rebuild: ## Force full image rebuild (use after requirements.txt changes)
	$(COMPOSE) down
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

shell: ## Open a bash shell inside the running container
	docker exec -it binance_python_bot_v2 bash

tail: ## Tail trades.log on the host (no container needed)
	tail -f trades.log
