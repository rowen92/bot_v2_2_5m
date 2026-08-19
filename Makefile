.PHONY: help _generate up down stop restart logs status ps rebuild shell tail

ENV_FILE = .env
-include $(ENV_FILE)
COMPOSE  = docker compose

_generate: ## Render docker-compose.yml from template for BOT
	@sed \
		-e "s|__PROJECT__|$(COMPOSE_PROJECT_NAME)|g" \
		-e "s|__SERVICE__|$(SERVICE)|g" \
		-e "s|__CONTAINER__|$(CONTAINER)|g" \
		-e "s|__ENV_FILE__|$(ENV_FILE)|g" \
		docker-compose.template.yml > docker-compose.yml
	@echo "→ docker-compose.yml  PROJECT=$(COMPOSE_PROJECT_NAME)  SERVICE=$(SERVICE)  CONTAINER=$(CONTAINER)  ENV=$(ENV_FILE)"

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: _generate ## Build and start
	$(COMPOSE) up -d --build

down: _generate ## Stop and remove containers
	$(COMPOSE) down

stop: _generate ## Stop containers without removing
	$(COMPOSE) stop

restart: _generate ## Stop → rebuild → start
	$(COMPOSE) down
	$(COMPOSE) up -d --build

logs: _generate ## Follow live logs
	$(COMPOSE) logs -f --tail=100

status: _generate ## Show container status
	$(COMPOSE) ps

ps: _generate ## List all containers for this project
	$(COMPOSE) ps

rebuild: _generate ## Force full image rebuild
	$(COMPOSE) down
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

shell: ## Open bash inside container
	docker exec -it $(CONTAINER) bash

tail: ## Tail trades.log on the host
	tail -f trades.log
