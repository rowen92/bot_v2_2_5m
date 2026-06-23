.PHONY: up down stop restart logs status rebuild shell

COMPOSE  = docker compose
SERVICE  = binance-bot

up: ## Build image (if needed) and start the bot in background
	$(COMPOSE) up -d --build

down: ## Stop and remove containers
	$(COMPOSE) down

stop: ## Stop containers without removing them
	$(COMPOSE) stop

restart: down up ## Stop + rebuild + start

logs: ## Follow live log output (Ctrl+C to exit)
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

status: ## Show container status
	$(COMPOSE) ps $(SERVICE)

ps: ## List containers for this project
	$(COMPOSE) ps

rebuild: ## Force full image rebuild and start
	$(COMPOSE) down
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

shell: ## Open a bash shell inside the running container
	docker exec -it binance_php_bot bash
