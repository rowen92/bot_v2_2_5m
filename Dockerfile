FROM php:8.2-cli-alpine

# Build args to match host user (avoids root-owned files on mounted volumes)
ARG UID=1000
ARG GID=1000

# Install essential dependencies
RUN apk add --no-cache git unzip bash curl-dev openssl-dev gmp-dev shadow

# Install required PHP extensions
RUN docker-php-ext-install curl bcmath gmp

# Install Composer
COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

# Create a non-root user matching the host UID/GID
RUN groupmod -g ${GID} www-data 2>/dev/null || groupadd -g ${GID} appuser && \
    usermod  -u ${UID} www-data 2>/dev/null || useradd  -u ${UID} -g ${GID} -s /bin/sh appuser

WORKDIR /usr/src/app

# Install PHP dependencies at build time
COPY composer.json composer.lock ./
RUN composer install --no-interaction --no-dev --prefer-dist

COPY . .

CMD ["php", "-d", "memory_limit=1024M", "./bot.php"]