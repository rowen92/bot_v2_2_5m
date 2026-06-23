FROM php:8.2-cli-alpine

# Install essential dependencies, including the GMP development headers
RUN apk add --no-cache git unzip bash curl-dev openssl-dev gmp-dev

# Install required PHP extensions
RUN docker-php-ext-install curl bcmath gmp

# Install Composer
COPY --from=composer:latest /usr/bin/composer /usr/bin/composer

WORKDIR /usr/src/app

# Run dependency installation on runtime and then execute the script with increased memory limit
CMD composer require ccxt/ccxt --no-interaction && php -d memory_limit=1024M ./bot.php